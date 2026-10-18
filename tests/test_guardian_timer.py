"""Fail-safe guardian timer: 2 minutes for her son, one 5-minute extension when he calls her."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
from transactions import (
    GUARDIAN_APPROVAL_TTL_SECONDS,
    GUARDIAN_CALL_EXTENSION_SECONDS,
    TransactionFlowError,
    analyze_transaction,
    approve_transaction,
    ask_child,
    extend_for_call,
    get_transaction_state,
    payment_for_transaction,
)


class GuardianTimerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        path = Path(self.temp_dir.name) / "timer.sqlite3"
        db.initialize_database(path)
        self.connection = db.connect_db(path)
        self.now = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
        tx = analyze_transaction(self.connection, payee_id=None, payee_name="New Clinic",
                                 amount_minor=12000, now=self.now)
        self.tx_id = tx["tx_id"]
        ask_child(self.connection, self.tx_id, now=self.now)

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def at(self, seconds):
        return self.now + timedelta(seconds=seconds)

    def approve(self, seconds):
        return approve_transaction(self.connection, self.tx_id, actor_role="GUARDIAN",
                                   authorization_validator=lambda row, role, method: True, now=self.at(seconds))

    def test_window_is_two_minutes_and_silence_stops_the_payment(self):
        self.assertEqual(GUARDIAN_APPROVAL_TTL_SECONDS, 120)
        state = get_transaction_state(self.connection, self.tx_id, now=self.at(120))
        self.assertEqual(state["transaction"]["status"], "EXPIRED")
        self.assertIsNone(payment_for_transaction(self.connection, self.tx_id))
        with self.assertRaises(TransactionFlowError):  # a late approval is refused
            self.approve(121)
        self.assertIsNone(payment_for_transaction(self.connection, self.tx_id))

    def test_son_can_still_approve_inside_the_window(self):
        self.assertTrue(self.approve(119)["executed"])

    def test_calling_mum_extends_once_then_fails_safe(self):
        extended = extend_for_call(self.connection, self.tx_id, now=self.at(100))
        self.assertTrue(extended["transaction"]["guardian_extended"])
        with self.assertRaises(TransactionFlowError):
            extend_for_call(self.connection, self.tx_id, now=self.at(110))  # only once
        self.assertEqual(get_transaction_state(self.connection, self.tx_id, now=self.at(390))["transaction"]["status"],
                         "AWAITING_GUARDIAN")
        state = get_transaction_state(self.connection, self.tx_id, now=self.at(100 + GUARDIAN_CALL_EXTENSION_SECONDS))
        self.assertEqual(state["transaction"]["status"], "EXPIRED")
        self.assertIsNone(payment_for_transaction(self.connection, self.tx_id))

    def test_extension_cannot_revive_an_expired_request(self):
        result = extend_for_call(self.connection, self.tx_id, now=self.at(121))
        self.assertEqual(result["error"], "EXPIRED")


if __name__ == "__main__":
    unittest.main()
