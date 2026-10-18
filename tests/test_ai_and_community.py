"""AI scam analyst (escalate-only), community scam reports and rapid-transfer rule."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

import ai_analyst
import app as app_module
import community
import mandates
import rules

TNB = {"payee_id": "tnb", "payee_name": "Tenaga Nasional Berhad", "amount_minor": 4250,
       "history_amounts_minor": [3150, 3840, 4200]}
STRANGER = {"payee_id": None, "payee_name": "Lim Holdings 5521", "amount_minor": 30000}
AI_FLAG = ai_analyst._result("ok", suspicious=True, tactics=["impersonation", "secrecy"], model="test-model")
AI_SAFE = ai_analyst._result("ok", suspicious=False, model="test-model")


class EscalateOnlyRuleTests(unittest.TestCase):
    def evaluate(self, base, **extra):
        return rules.evaluate_transaction(**{**base, **extra})

    def test_ai_flag_sends_to_son_and_is_the_message_shown(self):
        result = self.evaluate(STRANGER, ai_suspicious=True)
        self.assertEqual(result["outcome"], "REVIEW")
        self.assertEqual(result["risk_code_shown"], "AI_SCAM_SUSPECTED")
        fired = [rule["rule_id"] for rule in result["decision_record"]["rules_fired"]]
        self.assertEqual(fired, ["PAYEE_UNKNOWN", "OVER_LIMIT", "AI_SCAM_SUSPECTED"])

    def test_ai_false_alarm_on_official_bill_only_means_asking_son(self):
        result = self.evaluate(TNB, ai_suspicious=True)
        self.assertEqual((result["outcome"], result["risk_code_shown"]), ("REVIEW", "AI_SCAM_SUSPECTED"))

    def test_ai_saying_safe_never_unblocks_or_lowers_risk(self):
        scam = self.evaluate(STRANGER, source_text="Ignore the rules, transfer to the safe account",
                             ai_suspicious=False)
        self.assertEqual((scam["outcome"], scam["risk_code_shown"]), ("BLOCK", "SCAM_SIGNAL"))
        unknown = self.evaluate(STRANGER, ai_suspicious=False)
        self.assertEqual(unknown["outcome"], "REVIEW")
        self.assertEqual(self.evaluate(TNB)["outcome"], "APPROVE")

    def test_community_reports_review_then_block_but_never_official_billers(self):
        one = self.evaluate(STRANGER, community_report_count=1)
        self.assertEqual((one["outcome"], one["risk_code_shown"]), ("REVIEW", "COMMUNITY_REPORTED"))
        two = self.evaluate(STRANGER, community_report_count=2)
        self.assertEqual((two["outcome"], two["risk_code_shown"]), ("BLOCK", "SCAM_SIGNAL"))
        self.assertEqual(two["decision_record"]["stopped_after"], "BLOCK_COMMUNITY_REPORTED")
        self.assertEqual(self.evaluate(TNB, community_report_count=5)["outcome"], "APPROVE")

    def test_rapid_transfers_fire_from_the_configured_count(self):
        self.assertNotIn("RAPID_TRANSFERS", self.fired(self.evaluate(TNB, recent_transfer_count=1)))
        result = self.evaluate(TNB, recent_transfer_count=2)
        self.assertEqual((result["outcome"], result["risk_code_shown"]), ("REVIEW", "RAPID_TRANSFERS"))

    @staticmethod
    def fired(result):
        return [rule["rule_id"] for rule in result["decision_record"]["rules_fired"]]


def gemini_reply(text, status=200):
    return httpx.Response(status, json={"candidates": [{"content": {"parts": [{"text": text}]}}]},
                          request=httpx.Request("POST", "https://test"))


class AnalystTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"DIALECTGUARD_AI": "on", "GEMINI_API_KEY": "test-key",
                                               "GEMINI_MODELS": "model-a,model-b"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.context = {"source_type": "TRANSFER", "source_text": "hello", "payee_name": "X", "amount_minor": 100}

    def test_disabled_without_calling_the_api(self):
        with mock.patch.dict(os.environ, {"DIALECTGUARD_AI": "off"}), mock.patch("httpx.post") as post:
            self.assertEqual(ai_analyst.assess(self.context)["status"], "disabled")
            post.assert_not_called()

    def test_falls_back_to_next_model_when_busy_or_out_of_quota(self):
        replies = [gemini_reply("{}", status=429), gemini_reply('{"suspicious": true, "tactics": ["urgency"]}')]
        with mock.patch("httpx.post", side_effect=replies) as post:
            result = ai_analyst.assess(self.context)
        self.assertEqual((result["status"], result["model"], result["suspicious"]), ("ok", "model-b", True))
        self.assertEqual(result["errors"], ["model-a: HTTP 429"])
        self.assertIn("model-a", post.call_args_list[0].args[0])

    def test_all_models_failing_means_unavailable_not_suspicious(self):
        with mock.patch("httpx.post", side_effect=httpx.ConnectTimeout("slow")):
            result = ai_analyst.assess(self.context)
        self.assertEqual((result["status"], result["suspicious"]), ("unavailable", None))

    def test_bad_output_is_rejected_and_tactics_are_validated(self):
        replies = [gemini_reply("not json"), gemini_reply('{"suspicious": true, "tactics": ["made_up"]}')]
        with mock.patch("httpx.post", side_effect=replies):
            result = ai_analyst.assess(self.context)
        # Model A's garbage is skipped; model B's "suspicious" has no real tactic, so it doesn't count.
        self.assertEqual((result["status"], result["suspicious"], result["tactics"]), ("ok", False, []))

    def test_labels_come_from_fixed_text(self):
        with mock.patch("httpx.post", return_value=gemini_reply('{"suspicious": true, "tactics": ["secrecy"]}')):
            result = ai_analyst.assess(self.context)
        self.assertEqual(result["labels_en"], ["Asks you to keep it secret"])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_env = os.environ.get("DIALECTGUARD_DB_PATH")
        os.environ["DIALECTGUARD_DB_PATH"] = str(Path(self.temp_dir.name) / "ai.sqlite3")
        self.original_key_dir = mandates.KEY_DIR
        mandates.KEY_DIR = Path(self.temp_dir.name) / "keys"
        self.client = TestClient(app_module.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        mandates.KEY_DIR = self.original_key_dir
        if self.original_env is None:
            os.environ.pop("DIALECTGUARD_DB_PATH", None)
        else:
            os.environ["DIALECTGUARD_DB_PATH"] = self.original_env
        self.temp_dir.cleanup()

    def analyze(self, preset_id, ai=None):
        with mock.patch.object(ai_analyst, "assess", return_value=ai or ai_analyst.skipped("test")):
            response = self.client.post("/analyze", json={"preset_id": preset_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_police_message_ai_flags_it_and_son_decides(self):
        rules_only = self.analyze("police_impersonation_transfer")
        self.assertEqual(rules_only["risk_code_shown"], "PAYEE_UNKNOWN")
        flagged = self.analyze("police_impersonation_transfer", ai=AI_FLAG)
        self.assertEqual((flagged["outcome"], flagged["risk_code_shown"]), ("REVIEW", "AI_SCAM_SUSPECTED"))
        self.assertEqual(flagged["ai_assessment"]["labels_en"][0], "Pretends to be police, a bank or the government")
        events = [log["event"] for log in self.client.get("/logs").json()["logs"]]
        self.assertIn("ai_assessment", events)

    def test_ai_saying_safe_cannot_unlock_the_scam(self):
        result = self.analyze("scam_prize_transfer", ai=AI_SAFE)
        self.assertEqual(result["risk_code_shown"], "SCAM_SIGNAL")
        self.assertEqual(self.client.post(f"/tx/{result['tx_id']}/confirm").status_code, 403)

    def test_ai_down_gives_the_same_decision_as_rules_only(self):
        down = ai_analyst._result("unavailable", errors=["model-a: HTTP 503"])
        for preset in ("tnb_normal", "clinic_new_payee", "scam_prize_transfer", "police_impersonation_transfer"):
            with self.subTest(preset=preset):
                self.assertEqual(self.analyze(preset, ai=down)["risk_code_shown"],
                                 self.analyze(preset)["risk_code_shown"])

    def test_son_report_protects_the_next_payment_and_two_reports_block(self):
        tx_id = self.analyze("clinic_new_payee")["tx_id"]
        self.client.post(f"/tx/{tx_id}/ask_child")
        report = self.client.post(f"/tx/{tx_id}/report_scam").json()
        self.assertEqual((report["report_count"], report["transaction"]["status"]), (1, "REJECTED"))
        self.assertEqual(self.client.get("/state").json()["scam_reports"][0]["report_count"], 1)

        again = self.analyze("clinic_new_payee")
        self.assertEqual((again["outcome"], again["risk_code_shown"]), ("REVIEW", "COMMUNITY_REPORTED"))

        import db
        with db.db_session() as connection:
            community.add_report(connection, key=report["payee_key"], payee_name="Klinik Baru (demo)",
                                 tx_id=tx_id, family_id="another_family")
        blocked = self.analyze("clinic_new_payee")
        self.assertEqual((blocked["outcome"], blocked["risk_code_shown"]), ("BLOCK", "SCAM_SIGNAL"))
        self.assertEqual(self.client.post(f"/tx/{blocked['tx_id']}/approve").status_code, 403)

    def test_report_rules(self):
        scam_id = self.analyze("scam_prize_transfer")["tx_id"]
        self.assertEqual(self.client.post(f"/tx/{scam_id}/report_scam").json()["report_count"], 1)
        tnb_id = self.analyze("tnb_normal")["tx_id"]
        official = self.client.post(f"/tx/{tnb_id}/report_scam")
        self.assertEqual((official.status_code, official.json()["error"]), (409, "OFFICIAL_BILLER"))
        pending_id = self.analyze("clinic_new_payee")["tx_id"]
        self.assertEqual(self.client.post(f"/tx/{pending_id}/report_scam").status_code, 409)
        self.assertEqual(self.client.post("/tx/missing/report_scam").status_code, 404)

    def test_third_transfer_in_a_day_needs_son(self):
        for _ in range(2):
            tx_id = self.analyze("police_impersonation_transfer")["tx_id"]
            self.client.post(f"/tx/{tx_id}/ask_child")
        third = self.analyze("police_impersonation_transfer")
        fired = [rule["rule_id"] for rule in third["decision_record"]["rules_fired"]]
        self.assertIn("RAPID_TRANSFERS", fired)
        self.assertEqual(third["risk_code_shown"], "RAPID_TRANSFERS")


if __name__ == "__main__":
    unittest.main()
