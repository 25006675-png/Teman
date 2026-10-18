"""DialectGuard API (PROPOSAL §7).

Run:  uvicorn app:app --reload
Roles are fixed per endpoint (no login in the demo): Ah Por uses analyze,
confirm, cancel, ask_child and voice; her son (GUARDIAN) uses approve and reject.
The frontend only ever sends a tx_id; amount and payee come from the database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, timedelta
import os
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import audit
import bank
import budget
import db
import mandates
from rules import MESSAGES
from transactions import (
    TransactionFlowError,
    analyze_transaction,
    approve_transaction,
    ask_child,
    cancel_transaction,
    confirm_transaction,
    dispatch_voice_keyword,
    get_transaction_state,
    reject_transaction,
)

# "signed" = ECDSA signed approvals (default). "basic" = backup with no
# signatures, only for the case where signing breaks before the demo.
APPROVAL_MODE = os.environ.get("DIALECTGUARD_APPROVAL_MODE", "signed")
if APPROVAL_MODE not in ("signed", "basic"):
    raise RuntimeError("DIALECTGUARD_APPROVAL_MODE must be 'signed' or 'basic'")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.initialize_database()
    yield


app = FastAPI(title="DialectGuard", lifespan=lifespan)


@app.exception_handler(TransactionFlowError)
async def flow_error(_request: Request, error: TransactionFlowError) -> JSONResponse:
    return JSONResponse(status_code=error.http_status, content=error.as_payload())


class AnalyzeRequest(BaseModel):
    """Either a preset bill id, or extracted/typed fields (OCR output or "type it in")."""
    preset_id: str | None = None
    payee_id: str | None = None
    payee_name: str | None = None
    amount_minor: int | None = None
    due_date: str | None = None
    source_type: Literal["BILL", "TRANSFER"] = "BILL"
    source_text: str = ""


class ConfirmRequest(BaseModel):
    method: Literal["tap", "voice"] = "tap"
    acknowledge_warning: bool = False  # required for "Pay anyway" on a Warn card


class VoiceRequest(BaseModel):
    """Until Tech 1's speech.py is wired in, the client sends the matched keyword."""
    tx_id: str
    keyword: str | None = None  # "confirm", "cancel", anything else = no match
    acknowledge_warning: bool = False


def _preset(preset_id: str) -> dict[str, Any]:
    for record in db.load_config("preset_bills.json")["records"]:
        if record["id"] == preset_id:
            return record
    raise HTTPException(status_code=404, detail="Unknown preset bill")


def _validator(connection: Any, tx_id: str, role: str, method: str, payable_status: str):
    """Sign an approval for the stored transaction, then hand back its verifier."""
    if APPROVAL_MODE == "basic":
        return mandates.basic_validator
    row = db.get_transaction(connection, tx_id)
    approval = None
    if row is not None and row["status"] == payable_status:
        approval = mandates.issue_approval(connection, row, role=role, method=method)
    return mandates.signed_validator(connection, approval)


@app.post("/analyze")
def analyze(body: AnalyzeRequest) -> dict[str, Any]:
    if body.preset_id:
        bill = _preset(body.preset_id)
        fields = {
            "payee_id": bill["payee_id"], "payee_name": bill["payee_name"],
            "amount_minor": bill["amount_minor"], "due_date": bill["due_date"],
            "source_type": bill["kind"], "source_text": bill["ocr_text"],
        }
    else:
        fields = body.model_dump(exclude={"preset_id"})
    with db.db_session() as connection:
        history = []
        if fields["payee_id"]:
            history = [row["amount_minor"] for row in db.get_history_for_payee(connection, fields["payee_id"])]
        result = analyze_transaction(connection, history_amounts_minor=history, **fields)
    result["message_zh_yue"] = MESSAGES[result["template_code"]]["zh_yue_draft"]
    return result


@app.post("/tx/{tx_id}/confirm")
def confirm(tx_id: str, body: ConfirmRequest | None = None) -> dict[str, Any]:
    body = body or ConfirmRequest()
    with db.db_session() as connection:
        return confirm_transaction(
            connection, tx_id, method=body.method, actor_role="AH_POR",
            acknowledge_warning=body.acknowledge_warning,
            authorization_validator=_validator(connection, tx_id, "AH_POR", body.method, "PENDING"),
        )


@app.post("/tx/{tx_id}/cancel")
def cancel(tx_id: str) -> dict[str, Any]:
    with db.db_session() as connection:
        return cancel_transaction(connection, tx_id)


@app.post("/tx/{tx_id}/ask_child")
def request_guardian(tx_id: str) -> dict[str, Any]:
    with db.db_session() as connection:
        return ask_child(connection, tx_id)


@app.post("/tx/{tx_id}/approve")
def approve(tx_id: str) -> dict[str, Any]:
    with db.db_session() as connection:
        return approve_transaction(
            connection, tx_id, actor_role="GUARDIAN",
            authorization_validator=_validator(connection, tx_id, "GUARDIAN", "guardian", "AWAITING_GUARDIAN"),
        )


@app.post("/tx/{tx_id}/reject")
def reject(tx_id: str) -> dict[str, Any]:
    with db.db_session() as connection:
        return reject_transaction(connection, tx_id, actor_role="GUARDIAN")


@app.post("/voice")
def voice(body: VoiceRequest) -> dict[str, Any]:
    with db.db_session() as connection:
        validator = None
        if body.keyword == "confirm":
            validator = _validator(connection, body.tx_id, "AH_POR", "voice", "PENDING")
        return dispatch_voice_keyword(
            connection, body.tx_id, recognized_keyword=body.keyword, actor_role="AH_POR",
            authorization_validator=validator, acknowledge_warning=body.acknowledge_warning,
        )


@app.get("/state")
def state() -> dict[str, Any]:
    """Everything the split-screen dashboard needs; polled every second."""
    with db.db_session() as connection:
        ids = [row["id"] for row in connection.execute(
            "SELECT id FROM transactions ORDER BY created_at DESC LIMIT 20"
        )]
        # get_transaction_state also expires stale requests on read.
        transactions = [get_transaction_state(connection, tx_id)["transaction"] for tx_id in ids]
        payments = [dict(row) for row in connection.execute(
            "SELECT * FROM sandbox_payments ORDER BY executed_at DESC LIMIT 20"
        )]
        balance = bank.balance_minor(connection)
    return {
        "balance_minor": balance,
        "approval_mode": APPROVAL_MODE,
        "transactions": transactions,
        "awaiting_guardian": [t for t in transactions if t["status"] == "AWAITING_GUARDIAN"],
        "guardian_alerts": [
            t for t in transactions if t["status"] == "BLOCKED" and t["risk_code"] == "SCAM_SIGNAL"
        ],
        "payments": payments,
    }


@app.get("/logs")
def logs() -> dict[str, Any]:
    with db.db_session() as connection:
        return audit.build_logs_export(connection)


@app.get("/budget")
def get_budget() -> dict[str, Any]:
    with db.db_session() as connection:
        balance = bank.balance_minor(connection)
    return budget.calculate_budget(available_balance_minor=balance)


@app.get("/report")
def report() -> dict[str, Any]:
    """Last 7 days including today. Week start and timezone are still a team decision."""
    essentials = sum(item["amount_minor"] for item in budget.BUDGET_CONFIG["allocation_reserves"])
    with db.db_session() as connection:
        return budget.build_weekly_report(
            connection,
            week_start=date.today() - timedelta(days=6),
            available_balance_minor=bank.balance_minor(connection),
            essential_costs_next_week_minor=essentials,
        )


@app.post("/reset")
def reset() -> dict[str, Any]:
    db.reset_database()
    with db.db_session() as connection:
        balance = bank.balance_minor(connection)
    return {"ok": True, "balance_minor": balance}
