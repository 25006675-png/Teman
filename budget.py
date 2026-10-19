"""Budget calculations and weekly spending reports for Teman."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
import json
from typing import Any

from db import atomic, load_config, utc_now


CENT = 100
BUDGET_CONFIG = load_config("budget.json")
MESSAGE_TEMPLATES = load_config("messages.json")["templates"]


def _minor(value: Any, name: str, *, allow_negative: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer number of minor units")
    if value < 0 and not allow_negative:
        raise ValueError(f"{name} cannot be negative")
    return value


def format_minor_units(amount_minor: int) -> str:
    sign = "-" if amount_minor < 0 else ""
    absolute = abs(amount_minor)
    return f"{sign}{absolute // CENT}.{absolute % CENT:02d}"


def _message(code: str, **values: Any) -> str:
    template = MESSAGE_TEMPLATES.get(code, {}).get("en")
    if not isinstance(template, str):
        raise KeyError(f"Missing fixed English message template: {code}")
    return template.format(**values)


def _date(value: date | str | None, name: str) -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"{name} must be a date or ISO YYYY-MM-DD string")


def days_left_in_month(as_of: date | str | None = None) -> int:
    current = _date(as_of, "as_of")
    first_next_month = (
        date(current.year + 1, 1, 1)
        if current.month == 12
        else date(current.year, current.month + 1, 1)
    )
    return (first_next_month - current).days


def calculate_daily_limit(
    remaining_money_minor: int,
    days_left: int,
    *,
    essential_costs_minor: int = 0,
) -> dict[str, Any]:
    """Return the daily limit and exact calculation, rounded down to cents."""
    remaining = _minor(remaining_money_minor, "remaining_money_minor")
    essentials = _minor(essential_costs_minor, "essential_costs_minor")
    if not isinstance(days_left, int) or isinstance(days_left, bool) or days_left <= 0:
        raise ValueError("days_left must be a positive integer")

    after_essentials = remaining - essentials
    calculation = {
        "formula": "(remaining money - essential costs) / days left",
        "remaining_money_minor": remaining,
        "essential_costs_minor": essentials,
        "days_left": days_left,
        "rounding": "ROUND_DOWN_TO_MINOR_UNIT",
    }
    if after_essentials < 0:
        gap = -after_essentials
        calculation["calculation_text"] = (
            f"RM {format_minor_units(remaining)} - RM {format_minor_units(essentials)} "
            f"= -RM {format_minor_units(gap)}; no daily limit is set"
        )
        return {
            "status": "BUDGET_GAP",
            "daily_limit_minor": None,
            "remaining_after_essentials_minor": after_essentials,
            "gap_minor": gap,
            "calculation": calculation,
            "message": _message("NEXT_WEEK_BUDGET_GAP", gap=format_minor_units(gap)),
        }

    daily_limit = after_essentials // days_left
    calculation["available_after_essentials_minor"] = after_essentials
    calculation["daily_limit_minor"] = daily_limit
    calculation["calculation_text"] = (
        f"(RM {format_minor_units(remaining)} - RM {format_minor_units(essentials)}) "
        f"/ {days_left} days = RM {format_minor_units(after_essentials)} "
        f"/ {days_left} = RM {format_minor_units(daily_limit)} per day "
        "(rounded down to cents)"
    )
    return {
        "status": "ACTIVE",
        "daily_limit_minor": daily_limit,
        "remaining_after_essentials_minor": after_essentials,
        "gap_minor": 0,
        "calculation": calculation,
        "message": _message(
            "NEXT_WEEK_DAILY_LIMIT",
            essential_costs=format_minor_units(essentials),
            remaining=format_minor_units(after_essentials),
            days=days_left,
            daily_limit=format_minor_units(daily_limit),
        ),
    }


def calculate_budget(
    *,
    available_balance_minor: int,
    essential_costs_remaining_minor: int | None = None,
    as_of: date | str | None = None,
) -> dict[str, Any]:
    """Calculate current daily spending and clearly report fixed-cost shortfalls."""
    balance = _minor(available_balance_minor, "available_balance_minor")
    income = _minor(BUDGET_CONFIG["monthly_income_total_minor"], "monthly income")
    fixed_costs = sum(_minor(item["amount_minor"], "fixed cost") for item in BUDGET_CONFIG["fixed_costs"])
    if essential_costs_remaining_minor is None:
        essential_costs_remaining_minor = sum(
            _minor(item["amount_minor"], "essential reserve")
            for item in BUDGET_CONFIG["allocation_reserves"]
        )
    essentials = _minor(essential_costs_remaining_minor, "essential_costs_remaining_minor")
    current = _date(as_of, "as_of")
    days = days_left_in_month(current)

    if fixed_costs > income:
        gap = fixed_costs - income
        return {
            "status": "BUDGET_GAP",
            "severity": "CRITICAL",
            "income_minor": income,
            "fixed_costs_minor": fixed_costs,
            "fixed_cost_gap_minor": gap,
            "available_balance_minor": balance,
            "essential_costs_remaining_minor": essentials,
            "days_left_in_month": days,
            "daily_limit_minor": None,
            "message": _message("BUDGET_GAP", gap=format_minor_units(gap)),
            "calculation": {
                "formula": "fixed costs - monthly income",
                "fixed_costs_minor": fixed_costs,
                "income_minor": income,
                "gap_minor": gap,
                "calculation_text": (
                    f"RM {format_minor_units(fixed_costs)} fixed costs - "
                    f"RM {format_minor_units(income)} income = "
                    f"RM {format_minor_units(gap)} shortfall"
                ),
            },
        }

    daily = calculate_daily_limit(
        balance,
        days,
        essential_costs_minor=essentials,
    )
    return {
        "status": daily["status"],
        "severity": "CRITICAL" if daily["status"] == "BUDGET_GAP" else "INFO",
        "income_minor": income,
        "fixed_costs_minor": fixed_costs,
        "fixed_cost_gap_minor": 0,
        "available_balance_minor": balance,
        "essential_costs_remaining_minor": essentials,
        "days_left_in_month": days,
        "daily_limit_minor": daily["daily_limit_minor"],
        "message": daily["message"],
        "calculation": daily["calculation"],
    }


def _week_bounds(week_start: date | str) -> tuple[date, date]:
    start = _date(week_start, "week_start")
    return start, start + timedelta(days=7)


def _category_case() -> tuple[str, list[Any]]:
    payees = load_config("whitelist.json").get("payees", [])
    clauses = []
    parameters: list[Any] = []
    for payee in payees:
        if payee.get("id") and payee.get("category"):
            clauses.append("WHEN payee_id = ? THEN ?")
            parameters.extend((payee["id"], payee["category"]))
    expression = "CASE " + " ".join(clauses)
    expression += " WHEN source_type = 'TRANSFER' THEN 'transfers' ELSE 'uncategorized' END"
    return expression, parameters


def _weekly_category_summary(connection: Any, start: date, end: date) -> list[Any]:
    transaction_category, category_parameters = _category_case()
    start_bound = f"{start.isoformat()}T00:00:00"
    end_bound = f"{end.isoformat()}T00:00:00"
    query = f"""SELECT category,
                      SUM(amount_minor) AS amount_minor,
                      COUNT(*) AS transaction_count,
                      SUM(synthetic) AS synthetic_record_count
               FROM (
                   SELECT category, amount_minor, synthetic
                   FROM history
                   WHERE occurred_at >= ? AND occurred_at < ?
                   UNION ALL
                   SELECT {transaction_category} AS category,
                          amount_minor, 0 AS synthetic
                   FROM transactions
                   WHERE status = 'DONE'
                     AND COALESCE(completed_at, updated_at, created_at) >= ?
                     AND COALESCE(completed_at, updated_at, created_at) < ?
                     AND NOT EXISTS (
                         SELECT 1 FROM history WHERE history.id = transactions.id
                     )
               ) AS weekly_spend
               GROUP BY category
               ORDER BY category"""
    parameters = [start_bound, end_bound, *category_parameters, start_bound, end_bound]
    return connection.execute(query, parameters).fetchall()


def _category_budgets(supplied: Mapping[str, int] | None) -> dict[str, int]:
    source = supplied if supplied is not None else BUDGET_CONFIG.get("weekly_category_budgets_minor", {})
    if not isinstance(source, Mapping):
        raise ValueError("category budgets must map categories to minor-unit limits")
    return {
        str(category): _minor(amount, f"budget for {category}")
        for category, amount in source.items()
    }


def _save_next_week_limit(
    connection: Any,
    *,
    next_start: date,
    daily: Mapping[str, Any],
    essential_costs_minor: int,
    reason: Mapping[str, Any],
) -> None:
    next_end = next_start + timedelta(days=7)
    with atomic(connection):
        connection.execute(
            """INSERT INTO budget_limits
               (period_start, period_end, daily_limit_minor, remaining_minor,
                essential_costs_minor, status, reason_json, calculated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(period_start) DO UPDATE SET
                   period_end = excluded.period_end,
                   daily_limit_minor = excluded.daily_limit_minor,
                   remaining_minor = excluded.remaining_minor,
                   essential_costs_minor = excluded.essential_costs_minor,
                   status = excluded.status,
                   reason_json = excluded.reason_json,
                   calculated_at = excluded.calculated_at""",
            (
                next_start.isoformat(),
                next_end.isoformat(),
                daily["daily_limit_minor"],
                daily["remaining_after_essentials_minor"],
                essential_costs_minor,
                daily["status"],
                json.dumps(reason, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                utc_now(),
            ),
        )


def build_weekly_report(
    connection: Any,
    *,
    week_start: date | str,
    available_balance_minor: int,
    essential_costs_next_week_minor: int,
    category_budgets_minor: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Aggregate a seven-day report and persist its recalculated next-week limit.

    The available balance must come from the authoritative sandbox ledger.
    High spending is flagged only against an explicitly configured weekly cap.
    """
    balance = _minor(available_balance_minor, "available_balance_minor")
    essentials = _minor(essential_costs_next_week_minor, "essential_costs_next_week_minor")
    start, end = _week_bounds(week_start)
    caps = _category_budgets(category_budgets_minor)
    rows = _weekly_category_summary(connection, start, end)

    categories = []
    high_categories = []
    total_spent = 0
    synthetic_records = 0
    for row in rows:
        category = str(row["category"])
        amount = int(row["amount_minor"])
        count = int(row["transaction_count"])
        synthetic_count = int(row["synthetic_record_count"] or 0)
        total_spent += amount
        synthetic_records += synthetic_count
        cap = caps.get(category)
        high = cap is not None and amount > cap
        item = {
            "category": category,
            "amount_minor": amount,
            "transaction_count": count,
            "synthetic_record_count": synthetic_count,
            "category_budget_minor": cap,
            "comparison_status": (
                "OVER_BUDGET" if high else "WITHIN_BUDGET"
            ) if cap is not None else "NO_CATEGORY_BUDGET_CONFIGURED",
            "high_spending": high,
            "message": _message(
                "WEEKLY_REPORT_CATEGORY",
                category=category,
                amount=format_minor_units(amount),
                transaction_count=count,
            ),
        }
        if high:
            over_by = amount - cap
            item["over_by_minor"] = over_by
            item["suggestion"] = _message(
                "CATEGORY_SPENDING_HIGH",
                category=category,
                over_by=format_minor_units(over_by),
            )
            high_categories.append(category)
        categories.append(item)

    income = _minor(BUDGET_CONFIG["monthly_income_total_minor"], "monthly income")
    fixed_costs = sum(_minor(item["amount_minor"], "fixed cost") for item in BUDGET_CONFIG["fixed_costs"])
    fixed_gap = max(fixed_costs - income, 0)
    if fixed_gap:
        daily = {
            "status": "BUDGET_GAP",
            "daily_limit_minor": None,
            "remaining_after_essentials_minor": balance - essentials,
            "gap_minor": fixed_gap,
            "calculation": {
                "formula": "fixed costs - monthly income",
                "fixed_costs_minor": fixed_costs,
                "income_minor": income,
                "gap_minor": fixed_gap,
                "calculation_text": (
                    f"RM {format_minor_units(fixed_costs)} - "
                    f"RM {format_minor_units(income)} = "
                    f"RM {format_minor_units(fixed_gap)} shortfall"
                ),
            },
            "message": _message("BUDGET_GAP", gap=format_minor_units(fixed_gap)),
        }
    else:
        daily = calculate_daily_limit(balance, 7, essential_costs_minor=essentials)

    reason = {
        "reason_codes": ["CATEGORY_OVER_BUDGET"] if high_categories else ["WEEKLY_RECALCULATION"],
        "high_categories": high_categories,
        "available_balance_minor": balance,
        "essential_costs_minor": essentials,
        "fixed_cost_gap_minor": fixed_gap,
        "calculation": daily["calculation"],
    }
    next_start = end
    _save_next_week_limit(
        connection,
        next_start=next_start,
        daily=daily,
        essential_costs_minor=essentials,
        reason=reason,
    )

    next_end = next_start + timedelta(days=7)
    return {
        "period": {"start": start.isoformat(), "end_exclusive": end.isoformat()},
        "total_spent_minor": total_spent,
        "total_spent": format_minor_units(total_spent),
        "category_count": len(categories),
        "synthetic_record_count": synthetic_records,
        "data_notice": "This report includes synthetic demo records." if synthetic_records else None,
        "summary_message": _message(
            "WEEKLY_REPORT_SUMMARY",
            total_spent=format_minor_units(total_spent),
            category_count=len(categories),
        ),
        "categories": categories,
        "high_spending_categories": high_categories,
        "category_comparison_status": "CONFIGURED" if caps else "NO_CATEGORY_BUDGETS_CONFIGURED",
        "next_week_limit": {
            "period": {"start": next_start.isoformat(), "end_exclusive": next_end.isoformat()},
            "status": daily["status"],
            "daily_limit_minor": daily["daily_limit_minor"],
            "daily_limit": (
                format_minor_units(daily["daily_limit_minor"])
                if daily["daily_limit_minor"] is not None else None
            ),
            "available_balance_minor": balance,
            "essential_costs_minor": essentials,
            "calculation": daily["calculation"],
            "message": daily["message"],
            "recalculated_after_high_spending": bool(high_categories),
            "persisted": True,
        },
    }


def get_daily_limit_for_date(connection: Any, on_date: date | str) -> Any | None:
    target = _date(on_date, "on_date").isoformat()
    return connection.execute(
        """SELECT * FROM budget_limits
           WHERE period_start <= ? AND period_end > ?
           ORDER BY period_start DESC LIMIT 1""",
        (target, target),
    ).fetchone()