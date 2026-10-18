"""Service-boundary tests for the future Technical Lead mandates verifier.

These use a verifier stand-in to test Tech 2's fail-closed contract. They do not
claim to exercise ECDSA signatures or the not-yet-present mandates.py module.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
from transactions import (
    TransactionFlowError,
    analyze_transaction,
    ask_child,
    approve_transaction,
    payment_for_transaction,
)


class StageTwoApprovalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "stage2.sqlite3"
        db.initialize_database(self.database_path)
        self.connection = db.connect_db(self.database_path)
        self.now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def make_review_transaction(self):
        result = analyze_transaction(
            self.connection,
            payee_id=None,
            payee_name="Unknown Clinic",
            amount_minor=12000,
            history_amounts_minor=(),
            now=self.now,
        )
        tx_id = result["tx_id"]
        ask_child(self.connection, tx_id, now=self.now + timedelta(seconds=1))
        return tx_id

    def test_stage2_tampered_amount_is_rejected_by_verifier_boundary(self):
        tx_id = self.make_review_transaction()
        stored = db.get_transaction(self.connection, tx_id)
        tampered_approval = {
            "tx_id": tx_id,
            "payee_id": stored["payee_id"],
            "amount_minor": stored["amount_minor"] + 1,
            "decision": "REVIEW",
            "role": "GUARDIAN",
            "method": "guardian",
            "nonce": "stage2-tampered-amount-nonce",
            "signature": "tampered-test-signature",
        }

        def verifier(transaction, role, method):
            return (
                tampered_approval["tx_id"] == transaction["id"]
                and tampered_approval["payee_id"] == transaction["payee_id"]
                and tampered_approval["amount_minor"] == transaction["amount_minor"]
                and role == tampered_approval["role"]
                and method == tampered_approval["method"]
            )

        with self.assertRaises(TransactionFlowError) as error:
            approve_transaction(
                self.connection,
                tx_id,
                actor_role="GUARDIAN",
                authorization_validator=verifier,
                now=self.now + timedelta(seconds=2),
            )
        self.assertEqual(error.exception.code, "AUTHORIZATION_REJECTED")
        self.assertEqual(db.get_transaction(self.connection, tx_id)["status"], "AWAITING_GUARDIAN")
        self.assertIsNone(payment_for_transaction(self.connection, tx_id))

    def test_stage2_replayed_nonce_is_rejected_for_second_transaction(self):
        first_tx_id = self.make_review_transaction()
        second_tx_id = self.make_review_transaction()
        nonce = "stage2-single-use-test-nonce"
        used_nonces = set()

        def verifier(transaction, role, method):
            if nonce in used_nonces:
                return False
            if role != "GUARDIAN" or method != "guardian":
                return False
            used_nonces.add(nonce)
            return True

        first = approve_transaction(
            self.connection,
            first_tx_id,
            actor_role="GUARDIAN",
            authorization_validator=verifier,
            now=self.now + timedelta(seconds=2),
        )
        self.assertTrue(first["executed"])
        self.assertEqual(first["transaction"]["status"], "DONE")

        with self.assertRaises(TransactionFlowError) as error:
            approve_transaction(
                self.connection,
                second_tx_id,
                actor_role="GUARDIAN",
                authorization_validator=verifier,
                now=self.now + timedelta(seconds=3),
            )
        self.assertEqual(error.exception.code, "AUTHORIZATION_REJECTED")
        self.assertEqual(db.get_transaction(self.connection, second_tx_id)["status"], "AWAITING_GUARDIAN")
        self.assertIsNone(payment_for_transaction(self.connection, second_tx_id))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM sandbox_payments").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
