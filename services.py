"""Shared payment analysis, used by both the API and the assistant agent.

Every payment request, whether from a button or from the agent, goes through
analyze(): community reports, rapid-transfer count, the AI scam check, the
rules, then the shadow model score. There is no other way to create one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import ai_analyst
import bank
import community
import db
import risk_model
from rules import MESSAGES, is_official_biller
from transactions import analyze_transaction

MALAYSIA_TIME = timezone(timedelta(hours=8))


def preset(preset_id: str) -> dict[str, Any] | None:
    return next((r for r in db.load_config("preset_bills.json")["records"] if r["id"] == preset_id), None)


def preset_fields(bill: dict[str, Any]) -> dict[str, Any]:
    return {
        "payee_id": bill["payee_id"], "payee_name": bill["payee_name"],
        "amount_minor": bill["amount_minor"], "due_date": bill["due_date"],
        "source_type": bill["kind"], "source_text": bill["ocr_text"],
    }


def analyze(connection: Any, fields: dict[str, Any]) -> dict[str, Any]:
    history = []
    if fields["payee_id"]:
        history = [row["amount_minor"] for row in db.get_history_for_payee(connection, fields["payee_id"])]
    reports = community.report_count(connection, community.payee_key(fields["payee_id"], fields["payee_name"]))
    recent_transfers = community.recent_transfer_count(connection) if fields["source_type"] == "TRANSFER" else 0
    ai = _ai_assessment(connection, fields, history, recent_transfers)
    result = analyze_transaction(
        connection, history_amounts_minor=history, community_report_count=reports,
        recent_transfer_count=recent_transfers, ai_suspicious=ai["suspicious"] is True, **fields,
    )
    result["ai_assessment"] = ai
    if result["tx_id"]:
        db.append_log(connection, event="ai_assessment", tx_id=result["tx_id"], status=ai["status"].upper(),
                      details={key: ai[key] for key in ("suspicious", "tactics", "model", "errors")})
        result["shadow_model"] = _shadow_score(connection, result["tx_id"], fields)
    result["message_zh_yue"] = MESSAGES[result["template_code"]]["zh_yue_draft"]
    return result


def bills_inbox(connection: Any) -> list[dict[str, Any]]:
    """Bills waiting for Ah Por (demo: preset bills marked in_inbox), with paid status."""
    bills = []
    for record in db.load_config("preset_bills.json")["records"]:
        if not record.get("in_inbox"):
            continue
        paid = connection.execute(
            """SELECT 1 FROM transactions WHERE status = 'DONE' AND payee_id IS ? AND amount_minor = ?
               AND due_date IS ? LIMIT 1""",
            (record["payee_id"], record["amount_minor"], record["due_date"]),
        ).fetchone()
        bills.append({
            "bill_id": record["id"], "payee_id": record["payee_id"], "payee_name": record["payee_name"],
            "amount_minor": record["amount_minor"], "due_date": record["due_date"],
            "status": "paid" if paid else "unpaid",
        })
    return bills


PHOTO_AMOUNT_MISMATCH = "amount on the photo differs from the bill the biller issued"


def check_photo_against_biller(connection: Any, fields: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """A photo of a recognised biller's bill must match the bill that biller issued.

    In production the biller's e-bill (bank bill presentment) gives the amount; here the bills inbox
    stands in for it. A tilted or blurred photo can misread "19.20" as "49.20": rather than
    trust it, the amount is dropped so the rules ask her to check or retake (never a guess).
    """
    if not fields.get("payee_id") or fields.get("amount_minor") is None:
        return fields, None
    issued = [bill["amount_minor"] for bill in bills_inbox(connection) if bill["payee_id"] == fields["payee_id"]]
    if issued and fields["amount_minor"] not in issued:
        return {**fields, "amount_minor": None}, PHOTO_AMOUNT_MISMATCH
    return fields, None


def _ai_assessment(connection: Any, fields: dict[str, Any], history: list[int], recent_transfers: int) -> dict[str, Any]:
    """Ask the AI analyst; skipped when key details are missing (the rules ask again anyway)."""
    if not (fields["payee_id"] or fields["payee_name"]) or not fields["amount_minor"]:
        return ai_analyst.skipped("missing payee or amount")
    balance = bank.balance_minor(connection)
    return ai_analyst.assess({
        "source_type": fields["source_type"],
        "source_text": fields["source_text"],
        "payee_name": fields["payee_name"] or fields["payee_id"],
        "amount_minor": fields["amount_minor"],
        "official_biller": is_official_biller(fields["payee_id"], fields["payee_name"]),
        "paid_before": bool(history),
        "share_of_balance": fields["amount_minor"] / balance if balance > 0 else 1.0,
        "recent_transfers": recent_transfers,
    })


def _shadow_score(connection: Any, tx_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    """Log the shadow model's score. It runs after the decision and can never change it."""
    try:
        shadow = risk_model.score(
            source_type=fields["source_type"], amount_minor=fields["amount_minor"],
            balance_minor=bank.balance_minor(connection), hour=datetime.now(MALAYSIA_TIME).hour,
        )
        db.append_log(connection, event="shadow_risk_score", tx_id=tx_id, status="SHADOW", details=shadow)
        return shadow
    except Exception as error:  # a shadow model must never break the real flow
        db.append_log(connection, event="shadow_model_error", tx_id=tx_id, status="SHADOW",
                      details={"error": type(error).__name__})
        return None
