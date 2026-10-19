"""Ah Ming's safeguards: a PIN to approve, and never approving money to his own account."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app as app_module
import mandates

DEMO_PIN = "2580"  # test value from data/guardians.json (stored there only as a hash)


class GuardianSafeguardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        env = mock.patch.dict(os.environ, {"DIALECTGUARD_DB_PATH": str(Path(self.temp_dir.name) / "g.sqlite3")})
        env.start()
        self.addCleanup(env.stop)
        self.original_key_dir = mandates.KEY_DIR
        mandates.KEY_DIR = Path(self.temp_dir.name) / "keys"
        self.client = TestClient(app_module.app)
        self.client.__enter__()
        self.client.post("/reset")
        self.opening = self.client.get("/state").json()["balance_minor"]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        mandates.KEY_DIR = self.original_key_dir
        self.temp_dir.cleanup()

    def send_to_son(self, payee, amount_minor):
        analyzed = self.client.post("/analyze", json={
            "payee_name": payee, "amount_minor": amount_minor, "source_type": "TRANSFER",
            "source_text": "Mum, please transfer to my account today."}).json()
        self.assertEqual(analyzed["outcome"], "REVIEW")
        self.assertEqual(self.client.post(f"/tx/{analyzed['tx_id']}/ask_child").status_code, 200)
        return analyzed["tx_id"]

    def balance(self):
        return self.client.get("/state").json()["balance_minor"]

    def test_son_cannot_approve_money_to_his_own_account(self):
        tx_id = self.send_to_son("tan ah ming  (MAYBANK 5512)", 40000)  # any spacing or case
        tx = next(t for t in self.client.get("/state").json()["transactions"] if t["tx_id"] == tx_id)
        self.assertTrue(tx["pays_guardian"])
        response = self.client.post(f"/tx/{tx_id}/approve", json={"pin": DEMO_PIN})
        self.assertEqual((response.status_code, response.json()["error"]), (403, "SELF_APPROVAL"))
        self.assertEqual(self.balance(), self.opening)
        # he can still say no
        self.assertEqual(self.client.post(f"/tx/{tx_id}/reject").status_code, 200)
        self.assertEqual(self.balance(), self.opening)

    def test_other_payees_still_approve_with_the_right_pin(self):
        tx_id = self.send_to_son("Klinik Baru (demo)", 12000)
        tx = next(t for t in self.client.get("/state").json()["transactions"] if t["tx_id"] == tx_id)
        self.assertFalse(tx["pays_guardian"])
        response = self.client.post(f"/tx/{tx_id}/approve", json={"pin": DEMO_PIN}).json()
        self.assertTrue(response["executed"])
        self.assertEqual(self.balance(), self.opening - 12000)

    def test_missing_or_wrong_pin_pays_nothing(self):
        tx_id = self.send_to_son("Klinik Baru (demo)", 12000)
        missing = self.client.post(f"/tx/{tx_id}/approve")
        self.assertEqual((missing.status_code, missing.json()["error"], missing.json()["attempts_left"]), (403, "WRONG_PIN", 2))
        wrong = self.client.post(f"/tx/{tx_id}/approve", json={"pin": "0000"}).json()
        self.assertEqual((wrong["error"], wrong["attempts_left"]), ("WRONG_PIN", 1))
        self.assertEqual(self.balance(), self.opening)

    def test_three_wrong_pins_stop_the_payment(self):
        tx_id = self.send_to_son("Klinik Baru (demo)", 12000)
        for _ in range(2):
            self.client.post(f"/tx/{tx_id}/approve", json={"pin": "1111"})
        locked = self.client.post(f"/tx/{tx_id}/approve", json={"pin": "1111"}).json()
        self.assertEqual(locked["error"], "PIN_LOCKED")
        tx = next(t for t in self.client.get("/state").json()["transactions"] if t["tx_id"] == tx_id)
        self.assertEqual(tx["status"], "REJECTED")
        # the right PIN is too late now
        self.assertNotEqual(self.client.post(f"/tx/{tx_id}/approve", json={"pin": DEMO_PIN}).status_code, 200)
        self.assertEqual(self.balance(), self.opening)
        events = [log["event"] for log in self.client.get("/logs").json()["logs"]]
        self.assertEqual(events.count("guardian_pin_wrong"), 3)

    def test_reset_clears_wrong_pin_counts(self):
        tx_id = self.send_to_son("Klinik Baru (demo)", 12000)
        self.client.post(f"/tx/{tx_id}/approve", json={"pin": "1111"})
        self.client.post("/reset")
        self.assertEqual(app_module._wrong_pins, {})


if __name__ == "__main__":
    unittest.main()
