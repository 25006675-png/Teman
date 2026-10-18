import tempfile
import threading
import unittest
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
from transactions import (
    GUARDIAN_APPROVAL_TTL_SECONDS,
    USER_CONFIRMATION_TTL_SECONDS,
    TransactionFlowError,
    analyze_transaction,
    ask_child,
    approve_transaction,
    cancel_transaction,
    confirm_transaction,
    get_transaction_state,
    payment_for_transaction,
    reject_transaction,
)


class TransactionFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "transactions.sqlite3"
        db.initialize_database(self.database_path)
        self.connection = db.connect_db(self.database_path)
        self.now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
        self.valid_signature_check = lambda row, role, method: True

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def create_tnb(self, amount_minor=4250, **kwargs):
        history = kwargs.pop("history_amounts_minor", [3150, 3840, 4200])
        return analyze_transaction(
            self.connection,
            payee_id="tnb",
            payee_name="Tenaga Nasional Berhad",
            amount_minor=amount_minor,
            history_amounts_minor=history,
            now=self.now,
            **kwargs,
        )

    def create_unknown(self, amount_minor=12000):
        return analyze_transaction(
            self.connection,
            payee_id=None,
            payee_name="Unknown Clinic",
            amount_minor=amount_minor,
            history_amounts_minor=(),
            now=self.now,
        )

    def test_payment_uses_stored_amount_and_only_executes_once(self):
        analyzed = self.create_tnb()
        tx_id = analyzed["tx_id"]
        result = confirm_transaction(
            self.connection,
            tx_id,
            method="tap",
            actor_role="AH_POR",
            authorization_validator=self.valid_signature_check,
            now=self.now + timedelta(seconds=1),
        )
        self.assertTrue(result["executed"])
        self.assertEqual(result["payment"]["amount_minor"], 4250)
        self.assertEqual(result["transaction"]["status"], "DONE")

        repeated = confirm_transaction(
            self.connection,
            tx_id,
            method="tap",
            actor_role="AH_POR",
            authorization_validator=self.valid_signature_check,
            now=self.now + timedelta(seconds=2),
        )
        self.assertFalse(repeated["executed"])
        self.assertTrue(repeated["already_final"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM sandbox_payments").fetchone()[0], 1)
        payment = payment_for_transaction(self.connection, tx_id)
        self.assertEqual(payment["amount_minor"], 4250)
        self.assertEqual(payment["payee_id"], "tnb")

    def test_concurrent_confirmations_create_only_one_payment(self):
        tx_id = self.create_tnb()["tx_id"]
        start_together = threading.Barrier(2)

        def confirm_on_own_connection():
            connection = db.connect_db(self.database_path)
            try:
                start_together.wait(timeout=5)
                return confirm_transaction(
                    connection,
                    tx_id,
                    method="tap",
                    actor_role="AH_POR",
                    authorization_validator=self.valid_signature_check,
                    now=self.now + timedelta(seconds=1),
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: confirm_on_own_connection(), range(2)))

        self.assertEqual(sum(result["executed"] for result in results), 1)
        self.assertEqual(sum(result.get("already_final", False) for result in results), 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM sandbox_payments").fetchone()[0], 1)

    def test_missing_signature_verifier_fails_closed_without_mutation(self):
        tx_id = self.create_tnb()["tx_id"]
        with self.assertRaises(TransactionFlowError) as error:
            confirm_transaction(
                self.connection,
                tx_id,
                method="tap",
                actor_role="AH_POR",
                authorization_validator=None,
                now=self.now + timedelta(seconds=1),
            )
        self.assertEqual(error.exception.code, "AUTHORIZATION_UNAVAILABLE")
        self.assertEqual(db.get_transaction(self.connection, tx_id)["status"], "PENDING")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM sandbox_payments").fetchone()[0], 0)

    def test_only_review_can_be_sent_to_child_or_approved(self):
        tx_id = self.create_unknown()["tx_id"]
        requested = ask_child(self.connection, tx_id, now=self.now + timedelta(seconds=1))
        self.assertEqual(requested["transaction"]["status"], "AWAITING_GUARDIAN")
        self.assertEqual(
            requested["transaction"]["expires_at"],
            (self.now + timedelta(seconds=1 + GUARDIAN_APPROVAL_TTL_SECONDS)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        )
        result = approve_transaction(
            self.connection,
            tx_id,
            actor_role="GUARDIAN",
            authorization_validator=self.valid_signature_check,
            now=self.now + timedelta(seconds=2),
        )
        self.assertEqual(result["transaction"]["status"], "DONE")
        self.assertEqual(result["payment"]["amount_minor"], 12000)

    def test_non_review_and_non_guardian_cannot_be_approved(self):
        tx_id = self.create_tnb()["tx_id"]
        with self.assertRaises(TransactionFlowError) as error:
            approve_transaction(
                self.connection,
                tx_id,
                actor_role="GUARDIAN",
                authorization_validator=self.valid_signature_check,
                now=self.now + timedelta(seconds=1),
            )
        self.assertEqual(error.exception.code, "NOT_REVIEW")
        with self.assertRaises(TransactionFlowError) as error:
            approve_transaction(
                self.connection,
                tx_id,
                actor_role="AH_POR",
                authorization_validator=self.valid_signature_check,
                now=self.now + timedelta(seconds=1),
            )
        self.assertEqual(error.exception.code, "AUTHORIZATION_REJECTED")

    def test_scam_blacklist_and_over_cap_are_blocked_and_never_approvable(self):
        cases = [
            {"payee_id": "tnb", "payee_name": "TNB", "amount_minor": 1000, "source_text": "safe account"},
            {"payee_id": "demo_scam_account_9910", "payee_name": "Acct #9910-SCAM", "amount_minor": 1000},
            {"payee_id": "tnb", "payee_name": "TNB", "amount_minor": 50001},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                analyzed = analyze_transaction(
                    self.connection,
                    history_amounts_minor=[3150, 3840, 4200],
                    now=self.now,
                    **fields,
                )
                tx_id = analyzed["tx_id"]
                self.assertEqual(analyzed["transaction"]["status"], "BLOCKED")
                self.assertIsNone(analyzed["transaction"]["expires_at"])
                with self.assertRaises(TransactionFlowError) as error:
                    ask_child(self.connection, tx_id, now=self.now + timedelta(seconds=1))
                self.assertEqual(error.exception.code, "BLOCKED")
                with self.assertRaises(TransactionFlowError) as error:
                    approve_transaction(
                        self.connection,
                        tx_id,
                        actor_role="GUARDIAN",
                        authorization_validator=self.valid_signature_check,
                        now=self.now + timedelta(seconds=1),
                    )
                self.assertEqual(error.exception.code, "BLOCKED")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM sandbox_payments").fetchone()[0], 0)

    def test_unrepresentable_amount_is_blocked_without_database_overflow(self):
        analyzed = analyze_transaction(
            self.connection,
            payee_id="tnb",
            payee_name="TNB",
            amount_minor=1 << 80,
            history_amounts_minor=[3150, 3840, 4200],
            now=self.now,
        )
        self.assertEqual(analyzed["outcome"], "BLOCK")
        self.assertEqual(analyzed["risk_code_shown"], "OVER_HARD_CAP")
        self.assertIsNone(analyzed["tx_id"])
        self.assertIsNone(analyzed["transaction"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)
        overflow_log = self.connection.execute(
            "SELECT details_json FROM logs WHERE event=? AND status=?",
            ("decision_recorded", "BLOCKED"),
        ).fetchone()
        self.assertIsNotNone(overflow_log)
        self.assertEqual(
            json.loads(overflow_log["details_json"])["reason"],
            "AMOUNT_EXCEEDS_SQLITE_INTEGER_RANGE",
        )

    def test_invalid_extracted_fields_return_validation_error(self):
        with self.assertRaises(TransactionFlowError) as error:
            analyze_transaction(
                self.connection,
                payee_id=17,
                payee_name="TNB",
                amount_minor=1000,
                now=self.now,
            )
        self.assertEqual(error.exception.code, "INVALID_INPUT")
        with self.assertRaises(TransactionFlowError) as error:
            analyze_transaction(
                self.connection,
                payee_id="tnb",
                payee_name="TNB",
                amount_minor=1000,
                due_date="not-a-date",
                now=self.now,
            )
        self.assertEqual(error.exception.code, "INVALID_INPUT")

    def test_warning_requires_explicit_acknowledgement(self):
        analyzed = self.create_tnb(
            amount_minor=3000,
            history_amounts_minor=[1000, 2000, 3000],
        )
        self.assertEqual(analyzed["outcome_band"], "WARN")
        with self.assertRaises(TransactionFlowError) as error:
            confirm_transaction(
                self.connection,
                analyzed["tx_id"],
                method="voice",
                actor_role="AH_POR",
                authorization_validator=self.valid_signature_check,
                now=self.now + timedelta(seconds=1),
            )
        self.assertEqual(error.exception.code, "WARNING_ACK_REQUIRED")
        result = confirm_transaction(
            self.connection,
            analyzed["tx_id"],
            method="voice",
            actor_role="AH_POR",
            authorization_validator=self.valid_signature_check,
            acknowledge_warning=True,
            now=self.now + timedelta(seconds=2),
        )
        self.assertTrue(result["executed"])

    def test_request_expires_at_fixed_600_second_boundary(self):
        tx_id = self.create_tnb()["tx_id"]
        self.assertEqual(USER_CONFIRMATION_TTL_SECONDS, 600)
        before = get_transaction_state(
            self.connection,
            tx_id,
            now=self.now + timedelta(seconds=USER_CONFIRMATION_TTL_SECONDS - 1),
        )
        self.assertEqual(before["transaction"]["status"], "PENDING")
        expired = get_transaction_state(
            self.connection,
            tx_id,
            now=self.now + timedelta(seconds=USER_CONFIRMATION_TTL_SECONDS),
        )
        self.assertEqual(expired["transaction"]["status"], "EXPIRED")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM sandbox_payments").fetchone()[0], 0)
        self.assertEqual(
            self.connection.execute("SELECT status FROM logs WHERE tx_id=? ORDER BY id DESC LIMIT 1", (tx_id,)).fetchone()[0],
            "EXPIRED",
        )

    def test_guardian_request_expires_at_fixed_boundary(self):
        tx_id = self.create_unknown()["tx_id"]
        ask_child(self.connection, tx_id, now=self.now)
        expired = get_transaction_state(
            self.connection,
            tx_id,
            now=self.now + timedelta(seconds=GUARDIAN_APPROVAL_TTL_SECONDS),
        )
        self.assertEqual(expired["transaction"]["status"], "EXPIRED")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM sandbox_payments").fetchone()[0], 0)

    def test_malformed_expiry_fails_closed_and_cannot_be_approved(self):
        tx_id = self.create_unknown()["tx_id"]
        ask_child(self.connection, tx_id, now=self.now)
        self.connection.execute("UPDATE transactions SET approval_expires_at=? WHERE id=?", ("not-a-time", tx_id))
        result = get_transaction_state(self.connection, tx_id, now=self.now + timedelta(seconds=1))
        self.assertEqual(result["transaction"]["status"], "EXPIRED")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM sandbox_payments").fetchone()[0], 0)

    def test_stored_expiry_cannot_extend_the_hard_ttl(self):
        tx_id = self.create_tnb()["tx_id"]
        extended = self.now + timedelta(days=365)
        self.connection.execute(
            "UPDATE transactions SET approval_expires_at = ? WHERE id = ?",
            (extended.isoformat(timespec="milliseconds").replace("+00:00", "Z"), tx_id),
        )
        result = get_transaction_state(
            self.connection,
            tx_id,
            now=self.now + timedelta(seconds=USER_CONFIRMATION_TTL_SECONDS + 1),
        )
        self.assertEqual(result["transaction"]["status"], "EXPIRED")
        self.assertEqual(
            result["transaction"]["expires_at"],
            (self.now + timedelta(seconds=USER_CONFIRMATION_TTL_SECONDS)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        )

    def test_guardian_reject_and_cancel_do_not_execute_payment(self):
        review_id = self.create_unknown()["tx_id"]
        ask_child(self.connection, review_id, now=self.now)
        rejected = reject_transaction(
            self.connection, review_id, actor_role="GUARDIAN", now=self.now + timedelta(seconds=1)
        )
        self.assertEqual(rejected["transaction"]["status"], "REJECTED")

        safe_id = self.create_tnb()["tx_id"]
        cancelled = cancel_transaction(self.connection, safe_id, now=self.now + timedelta(seconds=1))
        self.assertEqual(cancelled["transaction"]["status"], "CANCELLED")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM sandbox_payments").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
