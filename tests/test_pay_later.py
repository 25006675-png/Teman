"""Pay later: a saved bill is paid automatically before it's due, once, within the family's limit."""

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app as app_module
import db
import mandates
import scheduler

TODAY = date(2026, 10, 19)  # TNB's demo bill is due 28 Oct


class PayLaterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        env = mock.patch.dict(os.environ, {"DIALECTGUARD_DB_PATH": str(Path(self.temp_dir.name) / "p.sqlite3")})
        env.start()
        self.addCleanup(env.stop)
        self.original_key_dir = mandates.KEY_DIR
        mandates.KEY_DIR = Path(self.temp_dir.name) / "keys"
        self.client = TestClient(app_module.app)
        self.client.__enter__()
        self.client.post("/reset")
        self.opening = self.balance()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        mandates.KEY_DIR = self.original_key_dir
        self.temp_dir.cleanup()

    def balance(self):
        return self.client.get("/state").json()["balance_minor"]

    def tnb_bill(self):
        return self.client.post("/analyze", json={"preset_id": "tnb_normal"}).json()["tx_id"]

    def save(self, tx_id):
        with db.db_session() as connection:
            return scheduler.schedule(connection, tx_id, today=TODAY)["scheduled"]

    def run_on(self, day):
        with db.db_session() as connection:
            return scheduler.run_due(connection, today=day)

    def test_saved_bill_is_paid_two_days_before_it_is_due_and_only_once(self):
        tx_id = self.tnb_bill()
        saved = self.save(tx_id)
        self.assertEqual((saved["pay_on"], saved["status"]), ("2026-10-26", "SCHEDULED"))
        state = self.client.get("/state").json()
        self.assertEqual(next(t for t in state["transactions"] if t["tx_id"] == tx_id)["status"], "CANCELLED")
        self.assertEqual(len(state["scheduled_bills"]), 1)
        self.assertEqual(self.run_on(date(2026, 10, 25)), [])          # not yet
        self.assertEqual(self.balance(), self.opening)
        paid = self.run_on(date(2026, 10, 26))
        self.assertEqual(paid[0]["status"], "PAID")
        self.assertEqual(self.balance(), self.opening - 4250)
        payment = self.client.get("/state").json()["payments"][0]
        self.assertEqual((payment["method"], payment["amount_minor"]), ("mandate", 4250))
        self.assertEqual(self.run_on(date(2026, 10, 27)), [])          # never twice
        self.assertEqual(self.balance(), self.opening - 4250)

    def test_cancelled_bill_is_never_paid(self):
        saved = self.save(self.tnb_bill())
        self.assertEqual(self.client.post(f"/scheduled/{saved['id']}/cancel").status_code, 200)
        self.assertEqual(self.run_on(date(2026, 10, 28)), [])
        self.assertEqual(self.balance(), self.opening)
        self.assertEqual(self.client.post(f"/scheduled/{saved['id']}/cancel").status_code, 409)

    def test_rules_run_again_on_the_day(self):
        self.save(self.tnb_bill())
        blocked = {"outcome": "BLOCK", "tx_id": None}  # e.g. the payee was blacklisted meanwhile
        with mock.patch.object(scheduler.services, "analyze", return_value=blocked):
            result = self.run_on(date(2026, 10, 26))
        self.assertEqual((result[0]["status"], result[0]["note"]), ("FAILED", "rules said BLOCK"))
        self.assertEqual(self.balance(), self.opening)

    def test_family_limit_still_allows_only_one_payment_a_month(self):
        self.save(self.tnb_bill())
        self.save(self.tnb_bill())
        results = self.run_on(date(2026, 10, 26))
        self.assertEqual([r["status"] for r in results], ["PAID", "FAILED"])
        self.assertEqual(self.balance(), self.opening - 4250)

    def test_only_normal_recognised_bills_can_be_saved(self):
        clinic = self.client.post("/analyze", json={"preset_id": "clinic_new_payee"}).json()["tx_id"]
        response = self.client.post(f"/tx/{clinic}/pay_later")
        self.assertEqual((response.status_code, response.json()["error"]), (409, "NOT_ELIGIBLE"))
        scam = self.client.post("/analyze", json={"preset_id": "scam_prize_transfer"}).json()["tx_id"]
        self.assertEqual(self.client.post(f"/tx/{scam}/pay_later").status_code, 409)
        self.assertEqual(self.client.post("/tx/nope/pay_later").json()["error"], "NOT_FOUND")

        class FixedDate(date):  # the endpoint uses today's date; pin it before the demo bill's due date
            @classmethod
            def today(cls):
                return TODAY
        with mock.patch.object(scheduler, "date", FixedDate):
            ok = self.client.post(f"/tx/{self.tnb_bill()}/pay_later")
        self.assertEqual((ok.status_code, ok.json()["scheduled"]["pay_on"]), (200, "2026-10-26"))

    def test_reset_clears_saved_bills(self):
        self.save(self.tnb_bill())
        self.client.post("/reset")
        self.assertEqual(self.client.get("/state").json()["scheduled_bills"], [])


if __name__ == "__main__":
    unittest.main()
