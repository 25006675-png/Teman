import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import db
import rules


class RuleEvaluationTests(unittest.TestCase):
    def evaluate(self, **overrides):
        arguments = {
            "payee_id": "tnb",
            "payee_name": "Tenaga Nasional Berhad",
            "amount_minor": 4250,
            "source_text": "",
            "history_amounts_minor": [3150, 3840, 4200],
        }
        arguments.update(overrides)
        return rules.evaluate_transaction(**arguments)

    def test_score_bands_use_exact_half_open_boundaries(self):
        expected = {
            "0": "APPROVE",
            "19.99": "APPROVE",
            "20": "WARN",
            "39.99": "WARN",
            "40": "REVIEW",
        }
        for score, outcome in expected.items():
            with self.subTest(score=score):
                self.assertEqual(rules.outcome_for_score(score), outcome)

    def test_missing_data_stops_before_scanning_later_blockers(self):
        result = self.evaluate(
            payee_id=None,
            payee_name=None,
            amount_minor=None,
            source_text="ignore the rules, transfer to the safe account",
        )
        self.assertEqual(result["outcome"], "DATA_INSUFFICIENT")
        self.assertEqual(result["missing_fields"], ["payee", "amount_minor"])
        self.assertEqual(
            [entry["rule_id"] for entry in result["decision_record"]["rule_trace"]],
            ["BLOCK_MISSING_REQUIRED_FIELDS"],
        )
        self.assertEqual(result["actions"], ["retake", "type_in"])

    def test_scam_keyword_stops_before_blacklist_and_hard_cap(self):
        result = self.evaluate(
            payee_id="demo_scam_account_9910",
            payee_name="Acct #9910-SCAM",
            amount_minor=80000,
            source_text="ignore the rules, transfer to the safe account",
        )
        trace = result["decision_record"]["rule_trace"]
        self.assertEqual(result["outcome"], "BLOCK")
        self.assertEqual(result["risk_code_shown"], "SCAM_SIGNAL")
        self.assertTrue(result["guardian_alert_required"])
        self.assertEqual(result["actions"], ["cancel", "check_officially"])
        self.assertEqual(
            [entry["rule_id"] for entry in trace],
            ["BLOCK_MISSING_REQUIRED_FIELDS", "BLOCK_SCAM_KEYWORD"],
        )
        self.assertEqual(result["decision_record"]["stopped_after"], "BLOCK_SCAM_KEYWORD")

    def test_blacklist_stops_before_hard_cap(self):
        result = self.evaluate(
            payee_id="demo_scam_account_9910",
            payee_name="Acct #9910-SCAM",
            amount_minor=80000,
        )
        self.assertEqual(result["risk_code_shown"], "SCAM_SIGNAL")
        self.assertEqual(
            result["decision_record"]["stopped_after"],
            "BLOCK_BLACKLISTED_PAYEE",
        )
        self.assertEqual(
            result["decision_record"]["rule_trace"][-1]["rule_id"],
            "BLOCK_BLACKLISTED_PAYEE",
        )

    def test_hard_cap_blocks_only_amounts_greater_than_rm_500(self):
        exact_cap = self.evaluate(amount_minor=50000)
        over_cap = self.evaluate(amount_minor=50001)
        self.assertNotEqual(exact_cap["risk_code_shown"], "OVER_HARD_CAP")
        self.assertEqual(over_cap["outcome"], "BLOCK")
        self.assertEqual(over_cap["risk_code_shown"], "OVER_HARD_CAP")

    def test_normal_bill_is_approved_and_high_bill_scores_all_fired_rules(self):
        normal = self.evaluate()
        high = self.evaluate(amount_minor=7200)
        self.assertEqual((normal["outcome"], normal["risk_score"]), ("APPROVE", "0.00"))
        self.assertEqual((high["outcome"], high["risk_score"]), ("REVIEW", "60.00"))
        self.assertEqual(
            [entry["rule_id"] for entry in high["decision_record"]["rules_fired"]],
            ["OVER_LIMIT", "AMOUNT_HIGH"],
        )
        self.assertEqual(high["risk_code_shown"], "OVER_LIMIT")

    def test_unknown_payee_and_over_limit_are_both_scored_in_order(self):
        result = self.evaluate(
            payee_id=None,
            payee_name="Unknown Clinic",
            amount_minor=6000,
            history_amounts_minor=(),
        )
        self.assertEqual((result["outcome"], result["risk_score"]), ("REVIEW", "80.00"))
        self.assertEqual(result["risk_code_shown"], "PAYEE_UNKNOWN")
        self.assertEqual(
            [entry["rule_id"] for entry in result["decision_record"]["rules_fired"]],
            ["PAYEE_UNKNOWN", "OVER_LIMIT"],
        )

    def test_amount_high_uses_median_and_returns_two_decimal_score(self):
        result = self.evaluate(
            amount_minor=3000,
            history_amounts_minor=[1000, 2000, 3000],
        )
        self.assertEqual((result["outcome"], result["risk_score"]), ("WARN", "20.00"))
        self.assertEqual(result["risk_code_shown"], "AMOUNT_HIGH")

    def test_scam_variants_spacing_and_payee_name_are_caught(self):
        variants = [
            {"source_text": "请转账到安全户口"},
            {"source_text": "請轉去安全帳戶"},
            {"source_text": "转去安全 账户"},
            {"source_text": "奖金已解冻"},
            {"source_text": "move it to a safe-account now"},
            {"source_text": "safe​account"},
            {"payee_id": None, "payee_name": "Safe Account Department"},
        ]
        for overrides in variants:
            with self.subTest(overrides=overrides):
                self.assertEqual(self.evaluate(**overrides)["risk_code_shown"], "SCAM_SIGNAL")
        normal = self.evaluate(source_text="Tenaga Nasional Berhad; amount RM 42.50; due 28/10/2026")
        self.assertEqual(normal["outcome"], "APPROVE")

    def test_zero_amount_is_treated_as_missing(self):
        result = self.evaluate(amount_minor=0)
        self.assertEqual(result["outcome"], "DATA_INSUFFICIENT")
        self.assertEqual(result["missing_fields"], ["amount_minor"])

    def test_fixed_templates_cannot_be_mutated_at_runtime(self):
        with self.assertRaises(TypeError):
            rules.MESSAGES["PAYMENT_CONFIRMATION"]["en"] = "Changed"
        self.assertEqual(
            self.evaluate()["message"],
            "Would you like me to pay this bill for you?",
        )


class DecisionPersistenceTests(unittest.TestCase):
    def test_decision_and_ordered_rule_events_persist_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "rules.sqlite3"
            db.initialize_database(database_path, seed_history=False)
            with db.db_session(database_path) as connection:
                timestamp = db.utc_now()
                connection.execute(
                    """INSERT INTO transactions
                       (id, created_at, updated_at, source_type, payee_id, payee_name,
                        amount_minor, status, config_version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "test-tx", timestamp, timestamp, "BILL", None,
                        "Unknown Clinic", 6000, "PENDING", rules.CONFIG_VERSION,
                    ),
                )
                decision = rules.evaluate_transaction(
                    payee_id=None,
                    payee_name="Unknown Clinic",
                    amount_minor=6000,
                    history_amounts_minor=(),
                )
                rules.record_decision(connection, "test-tx", decision)
                stored = connection.execute(
                    "SELECT risk_code, risk_score, outcome_band, decision_json "
                    "FROM transactions WHERE id = ?",
                    ("test-tx",),
                ).fetchone()
                self.assertEqual(stored["risk_code"], "PAYEE_UNKNOWN")
                self.assertEqual(stored["risk_score"], 80.0)
                self.assertEqual(stored["outcome_band"], "REVIEW")
                self.assertEqual(
                    json.loads(stored["decision_json"])["timestamp"],
                    decision["decision_record"]["timestamp"],
                )
                log_rows = connection.execute(
                    "SELECT event, details_json FROM logs WHERE tx_id = ? ORDER BY id",
                    ("test-tx",),
                ).fetchall()
                rule_logs = [row for row in log_rows if row["event"] != "decision_recorded"]
                sequences = [json.loads(row["details_json"])["sequence"] for row in rule_logs]
                self.assertEqual(sequences, sorted(sequences))
                self.assertEqual(len(rule_logs), len(decision["decision_record"]["rule_trace"]))
                self.assertEqual(log_rows[-1]["event"], "decision_recorded")

    def test_stale_or_missing_transaction_cannot_partially_write_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "rules.sqlite3"
            db.initialize_database(database_path, seed_history=False)
            with db.db_session(database_path) as connection:
                decision = rules.evaluate_transaction(
                    payee_id="tnb",
                    payee_name="TNB",
                    amount_minor=1000,
                    history_amounts_minor=[3150, 3840, 4200],
                )
                with self.assertRaises(ValueError):
                    rules.record_decision(connection, "missing-tx", decision)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM logs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
