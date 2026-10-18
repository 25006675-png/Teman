"""Assistant agent and family-signed spending limits: it can act, but only inside guardrails."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

import agent
import ai_analyst
import app as app_module
import db
import mandates
from transactions import TransactionFlowError, analyze_transaction, pay_with_family_mandate

TNB = {"payee_id": "tnb", "payee_name": "Tenaga Nasional Berhad", "amount_minor": 4250,
       "history_amounts_minor": [3150, 3840, 4200]}


class FamilyMandateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_key_dir = mandates.KEY_DIR
        mandates.KEY_DIR = Path(self.temp_dir.name) / "keys"
        path = Path(self.temp_dir.name) / "mandates.sqlite3"
        db.initialize_database(path)
        self.connection = db.connect_db(path)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.connection.close()
        mandates.KEY_DIR = self.original_key_dir
        self.temp_dir.cleanup()

    def tnb_tx(self, **overrides):
        result = analyze_transaction(self.connection, **{**TNB, **overrides})
        return result["tx_id"], result["outcome_band"]

    def pay(self, tx_id, now=None):
        return pay_with_family_mandate(
            self.connection, tx_id, authorization_validator=mandates.delegated_validator(self.connection, now=now))

    def assert_refused(self, tx_id, code="AUTHORIZATION_REJECTED", now=None):
        with self.assertRaises(TransactionFlowError) as error:
            self.pay(tx_id, now=now)
        self.assertEqual(error.exception.code, code)

    def test_pays_inside_limit_once_per_month(self):
        mandates.issue_delegated(self.connection, payee_id="tnb", limit_per_bill_minor=8000,
                                 max_payments_per_month=1, valid_days=365)
        tx_id, band = self.tnb_tx()
        self.assertEqual(band, "APPROVE")
        self.assertEqual(self.pay(tx_id)["payment"]["amount_minor"], 4250)
        second, _ = self.tnb_tx()
        self.assert_refused(second)  # monthly allowance used up

    def test_over_limit_tampered_or_expired_limits_refuse(self):
        mandates.issue_delegated(self.connection, payee_id="tnb", limit_per_bill_minor=4000,
                                 max_payments_per_month=1, valid_days=30)
        tx_id, _ = self.tnb_tx()
        self.assert_refused(tx_id)  # RM 42.50 is above the RM 40 limit
        self.assert_refused(tx_id, now=self.now + timedelta(days=31))  # expired

        # Raising the limit in the database breaks the family's signature.
        row = self.connection.execute("SELECT record_json FROM mandates WHERE mandate_type = 'DELEGATED'").fetchone()
        record = json.loads(row["record_json"])
        record["limit_per_bill_minor"] = 999999
        self.connection.execute("UPDATE mandates SET amount_minor = 999999, record_json = ?", (json.dumps(record),))
        self.assertFalse(mandates.family_mandates(self.connection)[0]["signature_valid"])
        self.assert_refused(tx_id)

    def test_only_fully_safe_bills_qualify(self):
        mandates.seed_family_mandates(self.connection)
        warn_id, band = self.tnb_tx(amount_minor=4700)
        self.assertEqual(band, "WARN")
        self.assert_refused(warn_id, code="INVALID_STATE")
        stranger = analyze_transaction(self.connection, payee_id=None, payee_name="Lim Holdings",
                                       amount_minor=3000, history_amounts_minor=())
        self.assert_refused(stranger["tx_id"], code="INVALID_STATE")  # Review, not Approve
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM sandbox_payments").fetchone()[0], 0)


def call(name, args=None, call_id="c1"):
    part = {"functionCall": {"name": name, "args": args or {}, "id": call_id}, "thoughtSignature": "sig"}
    return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [part]}}]},
                          request=httpx.Request("POST", "https://test"))


def text(reply):
    return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": reply}]}}]},
                          request=httpx.Request("POST", "https://test"))


def busy():
    return httpx.Response(503, json={}, request=httpx.Request("POST", "https://test"))


class AgentApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        env = mock.patch.dict(os.environ, {
            "DIALECTGUARD_DB_PATH": str(Path(self.temp_dir.name) / "agent.sqlite3"),
            "DIALECTGUARD_AI": "on", "GEMINI_API_KEY": "test-key", "GEMINI_MODELS": "model-a,model-b",
        })
        env.start()
        self.addCleanup(env.stop)
        # The scam analyst is tested elsewhere; here it stays quiet unless a test says otherwise.
        analyst = mock.patch.object(ai_analyst, "assess", return_value=ai_analyst.skipped("test"))
        self.assess = analyst.start()
        self.addCleanup(analyst.stop)
        self.original_key_dir = mandates.KEY_DIR
        mandates.KEY_DIR = Path(self.temp_dir.name) / "keys"
        self.client = TestClient(app_module.app)
        self.client.__enter__()
        self.opening = self.state()["balance_minor"]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        mandates.KEY_DIR = self.original_key_dir
        self.temp_dir.cleanup()

    def state(self):
        return self.client.get("/state").json()

    def chat(self, replies, message="幫我交電費", path="/agent/chat"):
        with mock.patch("httpx.post", side_effect=replies) as post:
            body = {"message": message} if path == "/agent/chat" else None
            response = self.client.post(path, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), post

    def test_tool_set_has_no_way_to_approve_or_change_limits(self):
        self.assertEqual(set(agent.TOOLS), {"get_budget", "list_bills", "pay_bill", "check_money_request",
                                            "ask_family", "cancel_request", "get_spending_report"})
        self.assertEqual({d["name"] for d in agent.TOOL_DECLARATIONS}, set(agent.TOOLS))

    def test_pays_a_bill_inside_the_family_limit(self):
        result, _ = self.chat([call("list_bills"), call("pay_bill", {"bill_id": "tnb_normal"}),
                               text("幫你交咗電費 RM 42.50 ✅\nEN: Paid your electricity bill, RM 42.50.")])
        self.assertEqual([s["status"] for s in result["steps"]], [None, "paid_automatically"])
        self.assertEqual(result["guardrail"]["fact_check"], "passed")
        self.assertEqual(result["reply_en"], "Paid your electricity bill, RM 42.50.")
        self.assertTrue(result["cards"][0]["paid_by_agent"])
        state = self.state()
        self.assertEqual(state["balance_minor"], self.opening - 4250)
        self.assertEqual(state["payments"][0]["method"], "mandate")
        self.assertEqual(state["family_mandates"][0]["used_this_month"], 1)

    def test_morning_check_pays_both_bills_and_a_second_try_does_not_pay_again(self):
        script = [call("list_bills"), call("pay_bill", {"bill_id": "tnb_normal"}),
                  call("pay_bill", {"bill_id": "air_selangor_normal"}), text("交咗兩張單。\nEN: Paid both bills.")]
        result, _ = self.chat(script, path="/agent/morning_check")
        self.assertEqual(self.state()["balance_minor"], self.opening - 4250 - 1920)
        self.assertEqual({b["status"] for b in self.state()["bills_inbox"]}, {"paid"})
        again, _ = self.chat([call("pay_bill", {"bill_id": "tnb_normal"}), text("已經交咗。\nEN: Already paid.")])
        self.assertEqual(again["steps"][0]["status"], "already_paid")
        self.assertEqual(self.state()["balance_minor"], self.opening - 4250 - 1920)

    def test_scam_transfer_is_never_paid_and_the_checker_sees_her_own_words(self):
        message = "Inspector Lim said transfer RM 300 to Lim Holdings today and tell no one"
        result, _ = self.chat([
            call("check_money_request", {"payee_name": "Lim Holdings", "amount_rm": "300", "reason": "family help"}),
            call("approve_transaction", {"tx_id": "x"}, call_id="c2"),  # a tool that doesn't exist
            text("要問過仔女先。\nEN: This needs your son."),
        ], message=message)
        self.assertEqual([s["status"] for s in result["steps"]], ["needs_family_approval", "error"])
        self.assertEqual(self.state()["balance_minor"], self.opening)
        self.assertIn(message, self.assess.call_args.args[0]["source_text"])

    def test_fact_check_replaces_made_up_amounts_and_payment_claims(self):
        made_up, _ = self.chat([call("list_bills"), text("幫你交咗 RM 99.99\nEN: Paid RM 99.99")])
        self.assertEqual(made_up["guardrail"]["fact_check"], "replaced")
        self.assertIn("99.99", made_up["guardrail"]["reason"])
        false_claim, _ = self.chat([call("list_bills"), text("已經交咗！\nEN: All paid!")])
        self.assertEqual(false_claim["guardrail"]["fact_check"], "replaced")
        honest, _ = self.chat([call("list_bills"), text("電費仲未交，RM 42.50。\nEN: Not paid yet, RM 42.50.")])
        self.assertEqual(honest["guardrail"]["fact_check"], "passed")
        self.assertEqual(self.state()["balance_minor"], self.opening)

    def test_claiming_family_was_told_needs_ask_family_to_have_run(self):
        request = call("check_money_request", {"payee_name": "Lim Holdings", "amount_rm": "300"})
        false_claim, _ = self.chat([request, text("我已經通知咗屋企人。\nEN: I have notified your family.")],
                                   message="send RM 300 to Lim Holdings")
        self.assertEqual(false_claim["guardrail"]["fact_check"], "replaced")
        self.assertEqual(self.state()["awaiting_guardian"], [])
        tx_id = false_claim["cards"][0]["tx_id"]
        asked, _ = self.chat([call("ask_family", {"tx_id": tx_id}),
                              text("已經問緊你個仔。\nEN: I have asked your son.")], message="ask my son")
        self.assertEqual(asked["guardrail"]["fact_check"], "passed")
        self.assertEqual(len(self.state()["awaiting_guardian"]), 1)

    def test_budget_quoted_from_before_a_payment_is_replaced(self):
        first, _ = self.chat([call("get_budget"), text("ok\nEN: ok")])
        old_today = first["steps"][0]["summary"]["today"]
        result, _ = self.chat([call("get_budget"), call("pay_bill", {"bill_id": "tnb_normal"}, call_id="c2"),
                               text(f"交咗電費 RM 42.50，今日仲可以使 {old_today}\nEN: Paid. {old_today} today.")])
        self.assertEqual(result["guardrail"]["fact_check"], "replaced")
        new_today = result["steps"][1]["summary"]["today_after_payment"]
        self.assertNotEqual(new_today, old_today)
        self.assertIn(new_today, result["reply_yue"])
        self.assertNotIn(old_today, result["reply_yue"])

    def test_step_limit_ai_down_and_busy_model_fallbacks(self):
        looping, _ = self.chat([call("get_budget")] * agent.MAX_STEPS)
        self.assertEqual(looping["guardrail"]["fact_check"], "fallback")
        self.assertIn("今日仲可以使", looping["reply_yue"])
        with mock.patch("httpx.post", side_effect=httpx.ConnectTimeout("down")):
            down = self.client.post("/agent/chat", json={"message": "hi"}).json()
        self.assertEqual(down["guardrail"]["fact_check"], "fallback")
        switched, post = self.chat([busy(), busy(), call("get_budget"), text("今日仲可以使 RM 25.00\nEN: RM 25.00 today")])
        self.assertEqual(switched["model"], "model-b")
        self.assertIn("model-b", post.call_args_list[2].args[0])

    def test_son_can_pause_the_assistant(self):
        self.assertEqual(self.client.post("/agent/pause", json={"paused": True}).json(), {"paused": True})
        self.assertTrue(self.state()["agent_paused"])
        with mock.patch("httpx.post") as post:
            paused = self.client.post("/agent/chat", json={"message": "幫我交電費"}).json()
        post.assert_not_called()
        self.assertTrue(paused["paused"])
        self.client.post("/agent/pause", json={"paused": False})
        self.assertFalse(self.state()["agent_paused"])

    def test_family_limits_are_signed_and_shown(self):
        limits = {m["payee_id"]: m for m in self.state()["family_mandates"]}
        self.assertEqual(limits["tnb"]["limit_per_bill_minor"], 8000)
        self.assertTrue(all(m["signature_valid"] for m in limits.values()))


if __name__ == "__main__":
    unittest.main()
