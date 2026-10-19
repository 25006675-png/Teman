"""Pay later: bills Ah Por saves are paid automatically shortly before they're due.

Only a recognised biller's bill (APPROVE, with a due date) within the family's signed
limit can be saved, so the automatic payment is one the family already allowed.
On the payment day every rule runs again on the saved details, and the payment goes
through the family-limit (signed mandate) path, exactly like any other automatic payment.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import db
import mandates
import services
from rules import is_official_biller
from transactions import TransactionFlowError, cancel_transaction, pay_with_family_mandate

PAY_DAYS_BEFORE_DUE = 2


class SchedulingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _limit_for(connection: Any, payee_id: str) -> dict[str, Any] | None:
    return next((m for m in mandates.family_mandates(connection)
                 if m["payee_id"] == payee_id and m["signature_valid"] is not False), None)


def schedule(connection: Any, tx_id: str, *, today: date | None = None) -> dict[str, Any]:
    """Save a pending bill to be paid automatically before its due date."""
    today = today or date.today()
    row = db.get_transaction(connection, tx_id)
    if row is None:
        raise SchedulingError("NOT_FOUND", "Transaction not found.")
    if row["status"] != "PENDING" or row["outcome_band"] != "APPROVE" or row["source_type"] != "BILL":
        raise SchedulingError("NOT_ELIGIBLE", "Only a normal bill waiting for her can be paid later.")
    if not is_official_biller(row["payee_id"], row["payee_name"]):
        raise SchedulingError("NOT_ELIGIBLE", "Only bills from recognised billers can be paid later.")
    if not row["due_date"]:
        raise SchedulingError("NO_DUE_DATE", "This bill has no due date, so it can't be paid later.")
    due = date.fromisoformat(row["due_date"])
    if due < today:
        raise SchedulingError("OVERDUE", "This bill is already overdue. Please pay it now.")
    limit = _limit_for(connection, row["payee_id"])
    if limit is None or row["amount_minor"] > limit["limit_per_bill_minor"]:
        raise SchedulingError("OVER_FAMILY_LIMIT", "This amount is above what the family allowed to be paid automatically.")
    pay_on = max(today, due - timedelta(days=PAY_DAYS_BEFORE_DUE))
    with db.atomic(connection):
        cursor = connection.execute(
            """INSERT INTO scheduled_bills (created_at, updated_at, payee_id, payee_name, amount_minor,
                                            due_date, pay_on, source_tx_id, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'SCHEDULED')""",
            (_now(), _now(), row["payee_id"], row["payee_name"], row["amount_minor"],
             row["due_date"], pay_on.isoformat(), tx_id),
        )
        schedule_id = cursor.lastrowid
        db.append_log(connection, event="bill_scheduled", tx_id=tx_id, method="tap", status="SCHEDULED",
                      details={"schedule_id": schedule_id, "pay_on": pay_on.isoformat(), "due_date": row["due_date"]})
    cancel_transaction(connection, tx_id)  # the request she saw is closed; a fresh one is made on the day
    return {"ok": True, "scheduled": get(connection, schedule_id)}


def get(connection: Any, schedule_id: int) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM scheduled_bills WHERE id = ?", (schedule_id,)).fetchone()
    return dict(row) if row else None


def list_scheduled(connection: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute("SELECT * FROM scheduled_bills ORDER BY pay_on, id")]


def cancel(connection: Any, schedule_id: int) -> dict[str, Any]:
    """She changed her mind ("唔使交"): nothing will be paid."""
    with db.atomic(connection):
        cursor = connection.execute(
            "UPDATE scheduled_bills SET status = 'CANCELLED', updated_at = ? WHERE id = ? AND status = 'SCHEDULED'",
            (_now(), schedule_id),
        )
        if cursor.rowcount != 1:
            raise SchedulingError("NOT_SCHEDULED", "This bill isn't waiting to be paid.")
        db.append_log(connection, event="scheduled_bill_cancelled", method="tap", status="CANCELLED",
                      details={"schedule_id": schedule_id})
    return {"ok": True, "scheduled": get(connection, schedule_id)}


def _finish(connection: Any, schedule_id: int, status: str, *, tx_id: str | None, note: str | None) -> None:
    with db.atomic(connection):
        connection.execute(
            "UPDATE scheduled_bills SET status = ?, paid_tx_id = ?, note = ?, updated_at = ? WHERE id = ?",
            (status, tx_id, note, _now(), schedule_id),
        )
        db.append_log(connection, event="scheduled_bill_paid" if status == "PAID" else "scheduled_bill_failed",
                      tx_id=tx_id, method="mandate", status=status, details={"schedule_id": schedule_id, "note": note})


def run_due(connection: Any, *, today: date | None = None) -> list[dict[str, Any]]:
    """Pay every saved bill whose day has come. Every rule runs again first."""
    today = today or date.today()
    done = []
    for bill in connection.execute(
        "SELECT * FROM scheduled_bills WHERE status = 'SCHEDULED' AND pay_on <= ? ORDER BY pay_on, id",
        (today.isoformat(),),
    ).fetchall():
        fields = {"payee_id": bill["payee_id"], "payee_name": bill["payee_name"], "amount_minor": bill["amount_minor"],
                  "due_date": bill["due_date"], "source_type": "BILL",
                  "source_text": f"Saved bill, paid automatically before its due date ({bill['due_date']})"}
        result = services.analyze(connection, fields)
        tx_id = result.get("tx_id")
        if result["outcome"] != "APPROVE":
            if tx_id:
                try:
                    cancel_transaction(connection, tx_id)
                except TransactionFlowError:
                    pass
            _finish(connection, bill["id"], "FAILED", tx_id=tx_id, note=f"rules said {result['outcome']}")
        else:
            try:
                pay_with_family_mandate(connection, tx_id, authorization_validator=mandates.delegated_validator(connection))
                _finish(connection, bill["id"], "PAID", tx_id=tx_id, note=None)
            except TransactionFlowError as error:
                try:
                    cancel_transaction(connection, tx_id)  # e.g. this month's family limit already used
                except TransactionFlowError:
                    pass
                _finish(connection, bill["id"], "FAILED", tx_id=tx_id, note=error.code)
        done.append(get(connection, bill["id"]))
    return done
