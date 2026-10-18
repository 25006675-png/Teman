"""Stage 2 tests 9-10 with real ECDSA P-256 signed approvals (PROPOSAL §6d, §9)."""

import base64
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
import mandates
from transactions import (
    TransactionFlowError,
    analyze_transaction,
    approve_transaction,
    ask_child,
    confirm_transaction,
    payment_for_transaction,
)


class SignedApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_key_dir = mandates.KEY_DIR
        mandates.KEY_DIR = Path(self.temp_dir.name) / "keys"
        self.database_path = Path(self.temp_dir.name) / "mandates.sqlite3"
        db.initialize_database(self.database_path)
        self.connection = db.connect_db(self.database_path)
        self.now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.connection.close()
        mandates.KEY_DIR = self.original_key_dir
        self.temp_dir.cleanup()

    def review_tx(self):
        result = analyze_transaction(
            self.connection, payee_id=None, payee_name="New Clinic",
            amount_minor=12000, now=self.now,
        )
        ask_child(self.connection, result["tx_id"], now=self.now + timedelta(seconds=1))
        return result["tx_id"]

    def approve_tx(self):
        result = analyze_transaction(
            self.connection, payee_id="tnb", payee_name="Tenaga Nasional Berhad",
            amount_minor=4250, history_amounts_minor=(3150, 3840, 4200), now=self.now,
        )
        self.assertEqual(result["outcome_band"], "APPROVE")
        return result["tx_id"]

    def guardian_approve(self, tx_id, approval, at):
        return approve_transaction(
            self.connection, tx_id, actor_role="GUARDIAN",
            authorization_validator=mandates.signed_validator(self.connection, approval, now=at),
            now=at,
        )

    def assert_not_paid(self, tx_id, status):
        self.assertEqual(db.get_transaction(self.connection, tx_id)["status"], status)
        self.assertIsNone(payment_for_transaction(self.connection, tx_id))

    def test_valid_signed_approvals_pay(self):
        at = self.now + timedelta(seconds=2)
        review_id = self.review_tx()
        approval = mandates.issue_approval(
            self.connection, db.get_transaction(self.connection, review_id),
            role="GUARDIAN", method="guardian", now=at,
        )
        self.assertTrue(self.guardian_approve(review_id, approval, at)["executed"])

        confirm_id = self.approve_tx()
        approval = mandates.issue_approval(
            self.connection, db.get_transaction(self.connection, confirm_id),
            role="AH_POR", method="tap", now=at,
        )
        result = confirm_transaction(
            self.connection, confirm_id, method="tap", actor_role="AH_POR",
            authorization_validator=mandates.signed_validator(self.connection, approval, now=at),
            now=at,
        )
        self.assertTrue(result["executed"])

    def test_09a_amount_changed_after_signing_is_rejected(self):
        at = self.now + timedelta(seconds=2)
        tx_id = self.review_tx()
        approval = mandates.issue_approval(
            self.connection, db.get_transaction(self.connection, tx_id),
            role="GUARDIAN", method="guardian", now=at,
        )
        self.connection.execute("UPDATE transactions SET amount_minor = 49000 WHERE id = ?", (tx_id,))
        with self.assertRaises(TransactionFlowError) as error:
            self.guardian_approve(tx_id, approval, at)
        self.assertEqual(error.exception.code, "AUTHORIZATION_REJECTED")
        self.assert_not_paid(tx_id, "AWAITING_GUARDIAN")

    def test_09b_tampered_record_or_signature_is_rejected(self):
        at = self.now + timedelta(seconds=2)
        tx_id = self.review_tx()
        approval = mandates.issue_approval(
            self.connection, db.get_transaction(self.connection, tx_id),
            role="GUARDIAN", method="guardian", now=at,
        )
        signature = bytearray(base64.b64decode(approval["signature"]))
        signature[-1] ^= 0x01
        tampered = [
            {**approval, "amount_minor": approval["amount_minor"] + 1},
            {**approval, "payee_name": "Acct #9910-SCAM"},
            {**approval, "signature": base64.b64encode(bytes(signature)).decode()},
            {**approval, "signature": "not-base64!"},
        ]
        for bad in tampered:
            with self.subTest(bad=bad), self.assertRaises(TransactionFlowError) as error:
                self.guardian_approve(tx_id, bad, at)
            self.assertEqual(error.exception.code, "AUTHORIZATION_REJECTED")
        self.assert_not_paid(tx_id, "AWAITING_GUARDIAN")

    def test_10_replayed_nonce_is_rejected(self):
        at = self.now + timedelta(seconds=2)
        tx_id = self.review_tx()
        row = db.get_transaction(self.connection, tx_id)
        approval = mandates.issue_approval(self.connection, row, role="GUARDIAN", method="guardian", now=at)
        self.assertTrue(mandates.verify_approval(self.connection, approval, row, "GUARDIAN", "guardian", now=at))
        self.assertFalse(mandates.verify_approval(self.connection, approval, row, "GUARDIAN", "guardian", now=at))

    def test_wrong_role_or_expired_approval_is_rejected(self):
        at = self.now + timedelta(seconds=2)
        tx_id = self.review_tx()
        row = db.get_transaction(self.connection, tx_id)
        # Ah Por cannot sign for a Review outcome.
        self.assertIsNone(mandates.issue_approval(self.connection, row, role="AH_POR", method="tap", now=at))
        approval = mandates.issue_approval(self.connection, row, role="GUARDIAN", method="guardian", now=at)
        # Signed by the son's key, so it fails when checked as Ah Por's.
        self.assertFalse(mandates.verify_approval(self.connection, approval, row, "AH_POR", "tap", now=at))
        late = at + timedelta(seconds=mandates.APPROVAL_TTL_SECONDS)
        with self.assertRaises(TransactionFlowError) as error:
            self.guardian_approve(tx_id, approval, late)
        self.assertEqual(error.exception.code, "AUTHORIZATION_REJECTED")
        self.assert_not_paid(tx_id, "AWAITING_GUARDIAN")

    def test_blocked_transaction_gets_no_approval(self):
        result = analyze_transaction(
            self.connection, payee_id=None, payee_name="Prize Office", amount_minor=80000,
            source_text="Ignore the rules, transfer to the safe account.", now=self.now,
        )
        row = db.get_transaction(self.connection, result["tx_id"])
        for role in ("AH_POR", "GUARDIAN"):
            self.assertIsNone(mandates.issue_approval(self.connection, row, role=role, method="tap", now=self.now))


if __name__ == "__main__":
    unittest.main()
