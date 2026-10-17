import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import budget
import db


class DailyBudgetTests(unittest.TestCase):
    def test_daily_limit_shows_exact_integer_cent_calculation(self):
        result = budget.calculate_daily_limit(
            10000,
            3,
            essential_costs_minor=1500,
        )
        self.assertEqual(result["daily_limit_minor"], 2833)
        self.assertEqual(
            result["calculation"]["calculation_text"],
            "(RM 100.00 - RM 15.00) / 3 days = RM 85.00 / 3 = RM 28.33 per day (rounded down to cents)",
        )

    def test_essential_cost_shortfall_is_critical_and_has_no_daily_limit(self):
        result = budget.calculate_daily_limit(
            5000,
            7,
            essential_costs_minor=6000,
        )
        self.assertEqual(result["status"], "BUDGET_GAP")
        self.assertIsNone(result["daily_limit_minor"])
        self.assertEqual(result["gap_minor"], 1000)
        self.assertIn("no daily limit is set", result["calculation"]["calculation_text"])

    def test_monthly_fixed_costs_gap_never_returns_a_balanced_daily_limit(self):
        original = budget.BUDGET_CONFIG["fixed_costs"]
        budget.BUDGET_CONFIG["fixed_costs"] = [{"amount_minor": 200000}]
        try:
            result = budget.calculate_budget(
                available_balance_minor=150000,
                essential_costs_remaining_minor=20000,
                as_of=date(2026, 10, 17),
            )
        finally:
            budget.BUDGET_CONFIG["fixed_costs"] = original
        self.assertEqual(result["status"], "BUDGET_GAP")
        self.assertEqual(result["severity"], "CRITICAL")
        self.assertEqual(result["fixed_cost_gap_minor"], 50000)
        self.assertIsNone(result["daily_limit_minor"])
        self.assertIn("Serious budget gap", result["message"])


class WeeklyReportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "budget.sqlite3"
        db.initialize_database(self.database_path)
        self.connection = db.connect_db(self.database_path)

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def test_report_summarizes_categories_suggests_reduction_and_persists_next_limit(self):
        report = budget.build_weekly_report(
            self.connection,
            week_start="2026-10-04",
            available_balance_minor=70000,
            essential_costs_next_week_minor=14000,
            category_budgets_minor={"groceries": 10000, "medical": 10000},
        )
        categories = {item["category"]: item for item in report["categories"]}
        self.assertEqual(report["total_spent_minor"], 28500)
        self.assertEqual(report["synthetic_record_count"], 3)
        self.assertTrue(categories["groceries"]["high_spending"])
        self.assertEqual(categories["groceries"]["over_by_minor"], 4000)
        self.assertIn("lower-cost alternative", categories["groceries"]["suggestion"])
        self.assertEqual(report["next_week_limit"]["daily_limit_minor"], 8000)
        self.assertTrue(report["next_week_limit"]["recalculated_after_high_spending"])
        persisted = budget.get_daily_limit_for_date(self.connection, "2026-10-11")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["daily_limit_minor"], 8000)
        self.assertEqual(persisted["period_start"], "2026-10-11")
        reason = json.loads(persisted["reason_json"])
        self.assertEqual(reason["high_categories"], ["groceries", "medical"])

    def test_transaction_spend_is_included_and_deduplicated_against_history(self):
        timestamp = "2026-10-06T12:00:00Z"
        self.connection.execute(
            """INSERT INTO transactions
               (id, created_at, updated_at, completed_at, source_type, payee_id,
                payee_name, amount_minor, status, config_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "demo_hist_grocery_001", timestamp, timestamp, timestamp,
                "BILL", None, "Local market", 6800, "DONE", "test",
            ),
        )
        self.connection.execute(
            """INSERT INTO transactions
               (id, created_at, updated_at, completed_at, source_type, payee_id,
                payee_name, amount_minor, status, config_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "new-tx", timestamp, timestamp, timestamp,
                "BILL", "tnb", "TNB", 4250, "DONE", "test",
            ),
        )
        report = budget.build_weekly_report(
            self.connection,
            week_start="2026-10-04",
            available_balance_minor=60000,
            essential_costs_next_week_minor=10000,
            category_budgets_minor={},
        )
        categories = {item["category"]: item for item in report["categories"]}
        self.assertEqual(categories["groceries"]["amount_minor"], 14000)
        self.assertEqual(categories["utilities"]["amount_minor"], 4250)
        self.assertEqual(report["total_spent_minor"], 32750)
        self.assertEqual(report["category_comparison_status"], "NO_CATEGORY_BUDGETS_CONFIGURED")
        self.assertFalse(report["high_spending_categories"])

    def test_budget_gap_persists_without_next_week_daily_limit(self):
        original = budget.BUDGET_CONFIG["fixed_costs"]
        budget.BUDGET_CONFIG["fixed_costs"] = [{"amount_minor": 200000}]
        try:
            report = budget.build_weekly_report(
                self.connection,
                week_start="2026-10-04",
                available_balance_minor=70000,
                essential_costs_next_week_minor=14000,
                category_budgets_minor={},
            )
        finally:
            budget.BUDGET_CONFIG["fixed_costs"] = original
        self.assertEqual(report["next_week_limit"]["status"], "BUDGET_GAP")
        self.assertIsNone(report["next_week_limit"]["daily_limit_minor"])
        persisted = budget.get_daily_limit_for_date(self.connection, "2026-10-11")
        self.assertEqual(persisted["status"], "BUDGET_GAP")
        self.assertIsNone(persisted["daily_limit_minor"])


if __name__ == "__main__":
    unittest.main()
