"""Teman API (PROPOSAL §7).

Run:  uvicorn app:app --reload
Roles are fixed per endpoint (no login in the demo): Ah Por uses analyze,
confirm, cancel, ask_child and voice; her son (GUARDIAN) uses approve and reject.
The frontend only ever sends a tx_id; amount and payee come from the database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, timedelta
import hashlib
import hmac
import io
import os
import threading
import time
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent
import audit
import bank
import budget
import community
import db
import mandates
import ocr
import services
import scheduler
import speech
import tts
from rules import is_official_biller
from transactions import (
    TransactionFlowError,
    approve_transaction,
    ask_child,
    cancel_transaction,
    confirm_transaction,
    dispatch_voice_keyword,
    extend_for_call,
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
    _setup_demo()
    _preload_whisper()
    _start_pay_later_loop()
    yield


def _start_pay_later_loop() -> None:
    """Pay saved bills when their day comes: once at startup, then every few minutes."""
    if os.environ.get("DIALECTGUARD_SCHEDULER", "on") == "off":
        return

    def loop() -> None:
        while True:
            try:
                with db.db_session() as connection:
                    scheduler.run_due(connection)
            except Exception as error:  # never crash the server; try again next round
                print("pay-later run failed:", error)
            time.sleep(300)

    threading.Thread(target=loop, daemon=True).start()


def _preload_whisper() -> None:
    """Load the speech model in the background at startup, so her first spoken
    question doesn't wait ~20 s. Never downloads; skipped when the model isn't on disk."""
    if os.environ.get("DIALECTGUARD_WHISPER_PRELOAD", "on") == "off" or not _whisper_available():
        return
    threading.Thread(target=speech.get_model, args=(WHISPER_MODEL,), daemon=True).start()


def _setup_demo() -> None:
    """Create the database and sign the family's demo spending limits."""
    db.initialize_database()
    with db.db_session() as connection:
        mandates.seed_family_mandates(connection)


app = FastAPI(title="Teman", lifespan=lifespan)
STATIC_DIR = db.ROOT / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/presets")
def presets() -> list[dict[str, Any]]:
    """Demo bills for the "Snap bill" picker (stand-ins until OCR is wired in)."""
    return [
        {key: record[key] for key in ("id", "kind", "payee_name", "amount_minor", "due_date", "ocr_text")}
        for record in db.load_config("preset_bills.json")["records"]
    ]


@app.get("/officials")
def officials() -> list[dict[str, Any]]:
    """Static "Check with official channel" card; only published, sourced numbers."""
    return db.load_config("officials.json")["contacts"]


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
        bill = services.preset(body.preset_id)
        if bill is None:
            raise HTTPException(status_code=404, detail="Unknown preset bill")
        fields = services.preset_fields(bill)
    else:
        fields = body.model_dump(exclude={"preset_id"})
    with db.db_session() as connection:
        return services.analyze(connection, fields)


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# Tech 1's default Whisper model is large-v3; WHISPER_MODEL can pick a smaller, faster one.
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3")
_whisper_ready: set[str] = set()


def _read_upload(upload: UploadFile) -> bytes:
    data = upload.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    return data


@app.post("/analyze/photo")
def analyze_photo(photo: UploadFile = File(...)) -> dict[str, Any]:
    """Bill photo -> Tech 1's OCR -> the same checks as every other payment request."""
    fields = ocr.extract(_read_upload(photo))
    with db.db_session() as connection:
        checked, mismatch = services.check_photo_against_biller(connection, fields)
        result = services.analyze(connection, checked)
    result["read_from_photo"] = {key: fields[key] for key in ("payee_id", "payee_name", "amount_minor", "due_date")}
    if mismatch:
        result["photo_check"] = mismatch
    return result


def _whisper_available() -> bool:
    """True once the model is on disk; never downloads during a request."""
    if WHISPER_MODEL in _whisper_ready:
        return True
    try:
        from faster_whisper.utils import download_model
        download_model(WHISPER_MODEL, local_files_only=True)
    except Exception:
        return False
    _whisper_ready.add(WHISPER_MODEL)
    return True


@app.post("/voice/audio")
def voice_audio(tx_id: str = Form(...), audio: UploadFile = File(...),
                acknowledge_warning: bool = Form(False)) -> dict[str, Any]:
    """Her recording -> Tech 1's keyword matcher (audio stays in memory, never saved) -> confirm/cancel/no match."""
    data = _read_upload(audio)
    voice_status = "ok"
    keyword = None
    if not _whisper_available():
        voice_status = "model_not_ready"
    else:
        keyword = speech.match_keyword(data, model_size=WHISPER_MODEL)
    with db.db_session() as connection:
        validator = None
        if keyword == "confirm":
            validator = _validator(connection, tx_id, "AH_POR", "voice", "PENDING")
        result = dispatch_voice_keyword(
            connection, tx_id, recognized_keyword=keyword, actor_role="AH_POR",
            authorization_validator=validator, acknowledge_warning=acknowledge_warning,
        )
    return {**result, "heard": keyword, "voice_status": voice_status, "whisper_model": WHISPER_MODEL}


@app.post("/agent/transcribe")
def agent_transcribe(audio: UploadFile = File(...)) -> dict[str, Any]:
    """Her spoken question -> Cantonese text for the chat box. Returns text only:
    it never pays or calls the assistant. She checks the words, then taps Send.
    Audio stays in memory and is never saved."""
    data = _read_upload(audio)
    if not _whisper_available():
        return {"text": "", "status": "error", "voice_status": "model_not_ready", "whisper_model": WHISPER_MODEL}
    heard = speech.transcribe_audio(io.BytesIO(data), model_size=WHISPER_MODEL)
    return {"text": heard["text"], "status": heard["status"], "voice_status": "ok", "whisper_model": WHISPER_MODEL}


class SpeakRequest(BaseModel):
    text: str


SPEAK_MAX_CHARS = 400


@app.post("/speak", response_model=None)
async def speak_text(body: SpeakRequest) -> FileResponse | JSONResponse:
    """Her reply as Cantonese speech (edge-tts). Audio is only a helper: on any
    failure the page keeps the text on screen and uses the browser's voice.
    Data note: the text is sent to Microsoft's online voice service."""
    text = body.text.strip()
    if not text or len(text) > SPEAK_MAX_CHARS:
        raise HTTPException(status_code=422, detail=f"Text must be 1 to {SPEAK_MAX_CHARS} characters.")
    result = await tts.make_speech_async(text, output_dir=tts.OUTPUT_DIR)
    if result["status"] != "ok":
        return JSONResponse({"status": result["status"], "problems": result["problems"]}, status_code=503)
    return FileResponse(result["audio_path"], media_type="audio/mpeg")


@app.post("/tx/{tx_id}/confirm")
def confirm(tx_id: str, body: ConfirmRequest | None = None) -> dict[str, Any]:
    body = body or ConfirmRequest()
    with db.db_session() as connection:
        return confirm_transaction(
            connection, tx_id, method=body.method, actor_role="AH_POR",
            acknowledge_warning=body.acknowledge_warning,
            authorization_validator=_validator(connection, tx_id, "AH_POR", body.method, "PENDING"),
        )


@app.post("/tx/{tx_id}/pay_later", response_model=None)
def pay_later(tx_id: str) -> dict[str, Any] | JSONResponse:
    """Save a normal bill to be paid automatically shortly before it's due, within the family's limit."""
    with db.db_session() as connection:
        try:
            return scheduler.schedule(connection, tx_id)
        except scheduler.SchedulingError as error:
            return JSONResponse(status_code=409, content={"error": error.code, "message": error.message})


@app.post("/scheduled/{schedule_id}/cancel", response_model=None)
def cancel_scheduled(schedule_id: int) -> dict[str, Any] | JSONResponse:
    with db.db_session() as connection:
        try:
            return scheduler.cancel(connection, schedule_id)
        except scheduler.SchedulingError as error:
            return JSONResponse(status_code=409, content={"error": error.code, "message": error.message})


@app.post("/tx/{tx_id}/cancel")
def cancel(tx_id: str) -> dict[str, Any]:
    with db.db_session() as connection:
        return cancel_transaction(connection, tx_id)


@app.post("/tx/{tx_id}/ask_child")
def request_guardian(tx_id: str) -> dict[str, Any]:
    with db.db_session() as connection:
        return ask_child(connection, tx_id)


class ApproveRequest(BaseModel):
    pin: str = ""


GUARDIANS = db.load_config("guardians.json")
MAX_WRONG_PINS = GUARDIANS["max_wrong_pins"]
_wrong_pins: dict[str, int] = {}  # tx_id -> wrong PIN attempts (demo: kept in memory)
_wrong_pins_lock = threading.Lock()


def _guardian_pin_ok(pin: str) -> bool:
    """Compare against the salted hash in data/guardians.json (the PIN itself is never stored)."""
    stored = GUARDIANS["guardians"][0]["pin"]
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(stored["salt"]), stored["iterations"])
    return hmac.compare_digest(digest.hex(), stored["hash"])


def _wrong_pin(tx_id: str) -> JSONResponse:
    """Count a wrong PIN; after the last allowed try the request is rejected (fail safe)."""
    with db.db_session() as connection:
        if db.get_transaction(connection, tx_id) is None:
            raise TransactionFlowError("NOT_FOUND", http_status=404)
    with _wrong_pins_lock:
        _wrong_pins[tx_id] = _wrong_pins.get(tx_id, 0) + 1
        left = max(0, MAX_WRONG_PINS - _wrong_pins[tx_id])
    with db.db_session() as connection:
        db.append_log(connection, event="guardian_pin_wrong", tx_id=tx_id, method="guardian",
                      status="LOCKED" if left == 0 else "WRONG_PIN", details={"attempts_left": left})
        if left == 0:
            try:
                reject_transaction(connection, tx_id, actor_role="GUARDIAN")
            except TransactionFlowError:
                pass  # already final, expired or not his to decide: nothing to stop
    if left == 0:
        return JSONResponse(status_code=403, content={"error": "PIN_LOCKED", "attempts_left": 0,
                            "message": "Too many wrong PINs. The payment was stopped; nothing was paid."})
    return JSONResponse(status_code=403, content={"error": "WRONG_PIN", "attempts_left": left,
                        "message": f"Wrong PIN. {left} {'try' if left == 1 else 'tries'} left."})


@app.post("/tx/{tx_id}/approve", response_model=None)
def approve(tx_id: str, body: ApproveRequest | None = None) -> dict[str, Any] | JSONResponse:
    """Ah Ming approves with his PIN. He can never approve money going to his own account."""
    if not _guardian_pin_ok((body or ApproveRequest()).pin):
        return _wrong_pin(tx_id)
    with db.db_session() as connection:
        return approve_transaction(
            connection, tx_id, actor_role="GUARDIAN",
            authorization_validator=_validator(connection, tx_id, "GUARDIAN", "guardian", "AWAITING_GUARDIAN"),
        )


@app.post("/tx/{tx_id}/call_extension")
def call_extension(tx_id: str) -> dict[str, Any]:
    """Son taps "Call Mum": 5 more minutes to decide while they talk (once)."""
    with db.db_session() as connection:
        return extend_for_call(connection, tx_id)


@app.post("/tx/{tx_id}/reject")
def reject(tx_id: str) -> dict[str, Any]:
    with db.db_session() as connection:
        return reject_transaction(connection, tx_id, actor_role="GUARDIAN")


@app.post("/tx/{tx_id}/report_scam")
def report_scam(tx_id: str) -> Any:
    """Son marks a request as a scam: it's rejected, and the account is flagged for other families."""
    with db.db_session() as connection:
        row = db.get_transaction(connection, tx_id)
        if row is None:
            raise TransactionFlowError("NOT_FOUND", http_status=404)
        if is_official_biller(row["payee_id"], row["payee_name"]):
            return JSONResponse(status_code=409, content={
                "error": "OFFICIAL_BILLER", "message": "Official billers can't be reported as scams.",
            })
        if row["status"] == "AWAITING_GUARDIAN":
            reject_transaction(connection, tx_id, actor_role="GUARDIAN")
        elif row["status"] not in ("BLOCKED", "REJECTED"):
            raise TransactionFlowError("INVALID_STATE")
        key = community.payee_key(row["payee_id"], row["payee_name"])
        count = community.add_report(connection, key=key, payee_name=row["payee_name"],
                                     tx_id=tx_id, family_id=community.DEMO_FAMILY_ID)
        return {"ok": True, "payee_key": key, "report_count": count,
                "transaction": get_transaction_state(connection, tx_id)["transaction"]}


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
        reports = community.list_reports(connection)
        paused = agent.is_paused(connection)
        limits = mandates.family_mandates(connection)
        inbox = services.bills_inbox(connection)
        saved = scheduler.list_scheduled(connection)
    names = {p["id"]: p["display_name"] for p in db.load_config("whitelist.json")["payees"]}
    return {
        "balance_minor": balance,
        "scam_reports": reports,
        "agent_paused": paused,
        "family_mandates": [{**limit, "payee_name": names.get(limit["payee_id"], limit["payee_id"])} for limit in limits],
        "bills_inbox": inbox,
        "approval_mode": APPROVAL_MODE,
        "transactions": transactions,
        "awaiting_guardian": [t for t in transactions if t["status"] == "AWAITING_GUARDIAN"],
        "guardian_alerts": [
            t for t in transactions if t["status"] == "BLOCKED" and t["risk_code"] == "SCAM_SIGNAL"
        ],
        "payments": payments,
        "scheduled_bills": saved,
    }


class AgentChatRequest(BaseModel):
    message: str
    history: list[dict[str, str]] = []  # recent turns: {"role": "user"|"agent", "text": ...}


class PauseRequest(BaseModel):
    paused: bool


@app.post("/agent/chat")
def agent_chat(body: AgentChatRequest) -> dict[str, Any]:
    """Ah Por talks to the assistant agent (its guardrails are in agent.py)."""
    with db.db_session() as connection:
        return agent.run(connection, body.message[:2000], body.history)


@app.post("/agent/morning_check")
def agent_morning_check() -> dict[str, Any]:
    """The daily check-in: pays bills inside family limits and reports the budget."""
    with db.db_session() as connection:
        return agent.morning_check(connection)


@app.post("/agent/pause")
def agent_pause(body: PauseRequest) -> dict[str, Any]:
    """Ah Ming's switch. When paused, Ah Por uses the normal buttons."""
    with db.db_session() as connection:
        agent.set_paused(connection, body.paused)
        return {"paused": agent.is_paused(connection)}


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
    _setup_demo()
    with _wrong_pins_lock:
        _wrong_pins.clear()
    with db.db_session() as connection:
        balance = bank.balance_minor(connection)
    return {"ok": True, "balance_minor": balance}
