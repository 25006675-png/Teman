"""End-to-end API tests: the 5 demo scenarios through the real HTTP endpoints."""

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module
import mandates


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_env = os.environ.get("DIALECTGUARD_DB_PATH")
        os.environ["DIALECTGUARD_DB_PATH"] = str(Path(self.temp_dir.name) / "api.sqlite3")
        self.original_key_dir = mandates.KEY_DIR
        mandates.KEY_DIR = Path(self.temp_dir.name) / "keys"
        self.client = TestClient(app_module.app)
        self.client.__enter__()
        self.opening = self.balance()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        app_module.APPROVAL_MODE = "signed"
        mandates.KEY_DIR = self.original_key_dir
        if self.original_env is None:
            os.environ.pop("DIALECTGUARD_DB_PATH", None)
        else:
            os.environ["DIALECTGUARD_DB_PATH"] = self.original_env
        self.temp_dir.cleanup()

    def balance(self):
        return self.client.get("/state").json()["balance_minor"]

    def analyze(self, preset_id):
        response = self.client.post("/analyze", json={"preset_id": preset_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_scenario_1_budget_and_report(self):
        budget = self.client.get("/budget").json()
        self.assertEqual(budget["status"], "ACTIVE")
        self.assertGreater(budget["daily_limit_minor"], 0)
        report = self.client.get("/report")
        self.assertEqual(report.status_code, 200, report.text)

    def test_scenario_2_normal_bill_paid_with_signed_approval(self):
        result = self.analyze("tnb_normal")
        self.assertEqual(result["outcome_band"], "APPROVE")
        self.assertTrue(result["message_zh_yue"])
        paid = self.client.post(f"/tx/{result['tx_id']}/confirm", json={"method": "tap"})
        self.assertEqual(paid.status_code, 200, paid.text)
        self.assertTrue(paid.json()["executed"])
        self.assertEqual(self.balance(), self.opening - 4250)
        events = [log["event"] for log in self.client.get("/logs").json()["logs"]]
        self.assertIn("approval_signed", events)
        self.assertIn("payment_executed", events)

    def test_scenario_3_unknown_payee_son_approves(self):
        result = self.analyze("clinic_new_payee")
        self.assertEqual((result["outcome_band"], result["risk_code_shown"]), ("REVIEW", "PAYEE_UNKNOWN"))
        tx_id = result["tx_id"]
        # Ah Por can't pay a Review item herself, and the son can't approve before ask_child.
        self.assertEqual(self.client.post(f"/tx/{tx_id}/confirm").status_code, 409)
        self.assertEqual(self.client.post(f"/tx/{tx_id}/approve").status_code, 409)
        self.assertEqual(self.client.post(f"/tx/{tx_id}/ask_child").status_code, 200)
        queue = self.client.get("/state").json()["awaiting_guardian"]
        self.assertEqual([t["tx_id"] for t in queue], [tx_id])
        approved = self.client.post(f"/tx/{tx_id}/approve")
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["transaction"]["risk_code"], "PAYEE_UNKNOWN")
        self.assertEqual(self.balance(), self.opening - 12000)

    def test_scenario_4_scam_has_no_way_to_pay(self):
        result = self.analyze("scam_prize_transfer")
        self.assertEqual(result["risk_code_shown"], "SCAM_SIGNAL")
        self.assertNotIn("confirm", result["actions"])
        tx_id = result["tx_id"]
        for path in ("confirm", "ask_child", "approve", "reject"):
            self.assertEqual(self.client.post(f"/tx/{tx_id}/{path}").status_code, 403, path)
        voice = self.client.post("/voice", json={"tx_id": tx_id, "keyword": "confirm"})
        self.assertEqual(voice.status_code, 403)
        state = self.client.get("/state").json()
        self.assertEqual([t["tx_id"] for t in state["guardian_alerts"]], [tx_id])
        self.assertEqual(state["balance_minor"], self.opening)

    def test_scenario_5_double_confirm_charges_once(self):
        tx_id = self.analyze("tnb_normal")["tx_id"]
        first = self.client.post(f"/tx/{tx_id}/confirm").json()
        second = self.client.post(f"/tx/{tx_id}/confirm").json()
        self.assertTrue(first["executed"])
        self.assertFalse(second["executed"])
        self.assertEqual(len(self.client.get("/state").json()["payments"]), 1)
        self.assertEqual(self.balance(), self.opening - 4250)

    def test_core_02_request_amount_is_ignored(self):
        tx_id = self.analyze("tnb_normal")["tx_id"]
        paid = self.client.post(f"/tx/{tx_id}/confirm", json={"amount_minor": 1, "payee_name": "Acct #9910-SCAM"})
        self.assertEqual(paid.json()["payment"]["amount_minor"], 4250)
        self.assertEqual(paid.json()["payment"]["payee_id"], "tnb")

    def test_core_06_timeout_expires_and_balance_unchanged(self):
        tx_id = self.analyze("clinic_new_payee")["tx_id"]
        self.client.post(f"/tx/{tx_id}/ask_child")
        import db
        with db.db_session() as connection:
            connection.execute(
                "UPDATE transactions SET updated_at = '2026-01-01T00:00:00.000Z' WHERE id = ?", (tx_id,)
            )
        response = self.client.post(f"/tx/{tx_id}/approve").json()
        self.assertEqual(response["error"], "EXPIRED")
        self.assertEqual(self.balance(), self.opening)

    def test_warn_needs_explicit_pay_anyway(self):
        response = self.client.post("/analyze", json={
            "payee_id": "tnb", "payee_name": "Tenaga Nasional Berhad", "amount_minor": 4700,
        }).json()
        self.assertEqual(response["outcome_band"], "WARN")
        tx_id = response["tx_id"]
        refused = self.client.post(f"/tx/{tx_id}/confirm")
        self.assertEqual(refused.json()["error"], "WARNING_ACK_REQUIRED")
        paid = self.client.post(f"/tx/{tx_id}/confirm", json={"acknowledge_warning": True})
        self.assertTrue(paid.json()["executed"])

    def test_core_08_voice_without_keyword_changes_nothing(self):
        tx_id = self.analyze("tnb_normal")["tx_id"]
        response = self.client.post("/voice", json={"tx_id": tx_id, "keyword": None}).json()
        self.assertEqual(response["code"], "VOICE_UNRECOGNIZED")
        self.assertEqual(response["transaction"]["status"], "PENDING")
        voice_paid = self.client.post("/voice", json={"tx_id": tx_id, "keyword": "confirm"}).json()
        self.assertTrue(voice_paid["executed"])

    def test_basic_mode_backup_still_pays_and_still_blocks(self):
        app_module.APPROVAL_MODE = "basic"
        tx_id = self.analyze("tnb_normal")["tx_id"]
        self.assertTrue(self.client.post(f"/tx/{tx_id}/confirm").json()["executed"])
        scam_id = self.analyze("scam_prize_transfer")["tx_id"]
        self.assertEqual(self.client.post(f"/tx/{scam_id}/confirm").status_code, 403)

    def test_reset_restores_demo_state(self):
        tx_id = self.analyze("tnb_normal")["tx_id"]
        self.client.post(f"/tx/{tx_id}/confirm")
        self.assertEqual(self.client.post("/reset").json()["balance_minor"], self.opening)
        state = self.client.get("/state").json()
        self.assertEqual((state["transactions"], state["payments"]), ([], []))
        # Only the setup events remain: the family's spending limits being signed again.
        events = {log["event"] for log in self.client.get("/logs").json()["logs"]}
        self.assertEqual(events, {"family_mandate_signed"})
        self.assertEqual(self.analyze("tnb_normal")["outcome_band"], "APPROVE")

    def test_unknown_preset_and_unknown_tx(self):
        self.assertEqual(self.client.post("/analyze", json={"preset_id": "nope"}).status_code, 404)
        self.assertEqual(self.client.post("/tx/missing/confirm").status_code, 404)


if __name__ == "__main__":
    unittest.main()
