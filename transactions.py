"""Atomic transaction state and sandbox-payment operations for Tech 2."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
import json
import uuid
from datetime import date
from typing import Any

from db import atomic, append_log, get_transaction, load_config, utc_now
from rules import CONFIG_VERSION, MESSAGES, evaluate_transaction, record_decision


_TTL_CONFIG = load_config("limits.json")["approval_expiry"]
USER_CONFIRMATION_TTL_SECONDS = 600
# Fail-safe guardian timer: her son has 2 minutes to decide, or the payment is
# stopped. "Call Mum" extends it once by 5 minutes while they talk.
GUARDIAN_APPROVAL_TTL_SECONDS = 120
GUARDIAN_CALL_EXTENSION_SECONDS = 300
if (
    _TTL_CONFIG.get("transaction_ttl_seconds") != USER_CONFIRMATION_TTL_SECONDS
    or _TTL_CONFIG.get("guardian_ttl_seconds") != GUARDIAN_APPROVAL_TTL_SECONDS
    or _TTL_CONFIG.get("guardian_call_extension_seconds") != GUARDIAN_CALL_EXTENSION_SECONDS
):
    raise RuntimeError("Approval TTL config must remain fixed at 600, 120 and 300 seconds")

BLOCKING_RISK_CODES = frozenset({"SCAM_SIGNAL", "OVER_HARD_CAP"})
BLOCKING_RULE_IDS = frozenset({
    "BLOCK_SCAM_KEYWORD",
    "BLOCK_BLACKLISTED_PAYEE",
    "BLOCK_OVER_HARD_CAP",
})
ERROR_MESSAGES = {
    "NOT_FOUND": "Transaction not found.",
    "EXPIRED": "This request expired. No payment was made.",
    "ALREADY_FINAL": "This transaction is already final and cannot be changed.",
    "NOT_REVIEW": "Guardian approval is allowed only for a Review outcome.",
    "BLOCKED": "This transaction is blocked and cannot be approved.",
    "INVALID_STATE": "This action is not allowed for the transaction's current state.",
    "WARNING_ACK_REQUIRED": "Confirm that you want to pay despite the warning.",
    "AUTHORIZATION_UNAVAILABLE": "Payment is held until signed-approval verification is available.",
    "AUTHORIZATION_REJECTED": "The approval is invalid, expired, or not permitted for this outcome.",
    "INVALID_INPUT": "Transaction details are invalid.",
}
AuthorizationValidator = Callable[[Mapping[str, Any], str, str], bool]
SQLITE_MAX_INTEGER = (1 << 63) - 1


class TransactionFlowError(Exception):
    def __init__(self, code: str, *, http_status: int = 409) -> None:
        self.code = code
        self.http_status = http_status
        self.message = ERROR_MESSAGES[code]
        super().__init__(self.message)

    def as_payload(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message}


def _as_utc(value: datetime | None) -> datetime:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return moment.astimezone(timezone.utc)


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _ttl_for(status: str, extended: bool = False) -> int:
    if status == "AWAITING_GUARDIAN":
        return GUARDIAN_APPROVAL_TTL_SECONDS + (GUARDIAN_CALL_EXTENSION_SECONDS if extended else 0)
    return USER_CONFIRMATION_TTL_SECONDS


def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tx_id": row["id"],
        "status": row["status"],
        "payee_id": row["payee_id"],
        "payee_name": row["payee_name"],
        "amount_minor": row["amount_minor"],
        "currency": row["currency"],
        "risk_code": row["risk_code"],
        "risk_score": row["risk_score"],
        "outcome_band": row["outcome_band"],
        "decision_record": json.loads(row["decision_json"] or "{}"),
        "expires_at": row["approval_expires_at"],
        "guardian_extended": bool(row["guardian_extended"]),
    }


def _trace_logs(connection: Any, tx_id: str | None, decision: Mapping[str, Any]) -> None:
    for rule in decision["decision_record"]["rule_trace"]:
        append_log(
            connection,
            event="rule_fired" if rule["matched"] else "rule_checked",
            tx_id=tx_id,
            risk_code=rule["risk_code"] if rule["matched"] else None,
            status="FIRED" if rule["matched"] else "NOT_FIRED",
            details={
                "sequence": rule["sequence"],
                "rule_id": rule["rule_id"],
                "phase": rule["phase"],
                "matched": rule["matched"],
                "weight": rule.get("weight"),
            },
        )


def _expire_if_stale(
    connection: Any,
    row: Mapping[str, Any],
    now: datetime,
) -> tuple[Mapping[str, Any], bool]:
    status = row["status"]
    if status not in ("PENDING", "AWAITING_GUARDIAN"):
        return row, False

    invalid_expiry = False
    anchor_field = "updated_at" if status == "AWAITING_GUARDIAN" else "created_at"
    try:
        hard_deadline = _parse_timestamp(row[anchor_field]) + timedelta(
            seconds=_ttl_for(status, bool(row["guardian_extended"])))
    except (TypeError, ValueError):
        invalid_expiry = True
        hard_deadline = now
    if row["approval_expires_at"]:
        try:
            stored_expiry = _parse_timestamp(row["approval_expires_at"])
        except (TypeError, ValueError):
            invalid_expiry = True
            stored_expiry = now
    else:
        stored_expiry = hard_deadline
    expiry_was_clamped = stored_expiry > hard_deadline
    expires_at = min(stored_expiry, hard_deadline)

    expiry_text = _timestamp(expires_at)
    if (not row["approval_expires_at"] or expiry_was_clamped) and not invalid_expiry:
        connection.execute(
            "UPDATE transactions SET approval_expires_at = ? WHERE id = ? AND status = ?",
            (expiry_text, row["id"], status),
        )
        if expiry_was_clamped:
            append_log(
                connection,
                event="expiry_clamped_to_configured_ttl",
                tx_id=row["id"],
                risk_code=row["risk_code"],
                status=status,
                details={"configured_deadline": expiry_text},
            )

    if invalid_expiry or expires_at <= now:
        cursor = connection.execute(
            """UPDATE transactions
               SET status = 'EXPIRED', updated_at = ?, approval_expires_at = ?
               WHERE id = ? AND status = ?""",
            (_timestamp(now), expiry_text, row["id"], status),
        )
        if cursor.rowcount == 1:
            append_log(
                connection,
                event="transaction_expired",
                tx_id=row["id"],
                risk_code=row["risk_code"],
                status="EXPIRED",
                details={
                    "reason": "INVALID_EXPIRY_TIMESTAMP" if invalid_expiry else "TTL_ELAPSED",
                    "expired_at": expiry_text,
                },
            )
        refreshed = get_transaction(connection, row["id"])
        if refreshed is None:
            raise TransactionFlowError("NOT_FOUND", http_status=404)
        return refreshed, refreshed["status"] == "EXPIRED"

    return get_transaction(connection, row["id"]) or row, False


def _get_live(connection: Any, tx_id: str, now: datetime) -> tuple[Mapping[str, Any], bool]:
    row = get_transaction(connection, tx_id)
    if row is None:
        raise TransactionFlowError("NOT_FOUND", http_status=404)
    return _expire_if_stale(connection, row, now)


def _decision(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row["decision_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        raise TransactionFlowError("BLOCKED", http_status=403) from None
    if not isinstance(value, dict):
        raise TransactionFlowError("BLOCKED", http_status=403)
    return value


def _is_blocked(row: Mapping[str, Any], decision: Mapping[str, Any]) -> bool:
    return (
        row["status"] == "BLOCKED"
        or decision.get("outcome") == "BLOCK"
        or row["risk_code"] in BLOCKING_RISK_CODES
        or any(rule.get("rule_id") in BLOCKING_RULE_IDS for rule in decision.get("rules_fired", ()))
    )


def _require_guardian(actor_role: str) -> None:
    if actor_role != "GUARDIAN":
        raise TransactionFlowError("AUTHORIZATION_REJECTED", http_status=403)


def _verify(
    validator: AuthorizationValidator | None,
    row: Mapping[str, Any],
    role: str,
    method: str,
) -> None:
    if validator is None:
        raise TransactionFlowError("AUTHORIZATION_UNAVAILABLE", http_status=503)
    if not validator(row, role, method):
        raise TransactionFlowError("AUTHORIZATION_REJECTED", http_status=403)


def _execute(
    connection: Any,
    row: Mapping[str, Any],
    *,
    expected_status: str,
    role: str,
    method: str,
    now: datetime,
) -> dict[str, Any]:
    decision = _decision(row)
    if _is_blocked(row, decision):
        raise TransactionFlowError("BLOCKED", http_status=403)
    allowed_bands = ("REVIEW",) if role == "GUARDIAN" else ("APPROVE", "WARN")
    if row["outcome_band"] not in allowed_bands:
        raise TransactionFlowError("NOT_REVIEW" if role == "GUARDIAN" else "INVALID_STATE")

    completed_at = _timestamp(now)
    cursor = connection.execute(
        """UPDATE transactions
           SET status = 'DONE', completed_at = ?, updated_at = ?
           WHERE id = ? AND status = ? AND outcome_band = ?
                         AND (risk_code IS NULL OR risk_code NOT IN ('SCAM_SIGNAL', 'OVER_HARD_CAP'))""",
        (completed_at, completed_at, row["id"], expected_status, row["outcome_band"]),
    )
    if cursor.rowcount != 1:
        raise TransactionFlowError("ALREADY_FINAL")

    connection.execute(
        """INSERT INTO sandbox_payments
           (tx_id, payee_id, payee_name, amount_minor, currency, executed_at, method)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (row["id"], row["payee_id"], row["payee_name"], row["amount_minor"], row["currency"], completed_at, method),
    )
    append_log(
        connection,
        event="payment_executed",
        tx_id=row["id"],
        risk_code=row["risk_code"],
        method=method,
        status="DONE",
        details={
            "amount_minor": row["amount_minor"],
            "payee_id": row["payee_id"],
            "payee_name": row["payee_name"],
            "role": role,
        },
    )
    updated = get_transaction(connection, row["id"])
    return {
        "ok": True,
        "executed": True,
        "transaction": _payload(updated),
        "payment": {
            "tx_id": row["id"],
            "payee_id": row["payee_id"],
            "payee_name": row["payee_name"],
            "amount_minor": row["amount_minor"],
            "currency": row["currency"],
            "executed_at": completed_at,
        },
    }


def analyze_transaction(
    connection: Any,
    *,
    payee_id: str | None,
    payee_name: str | None,
    amount_minor: int | None,
    due_date: str | None = None,
    source_type: str = "BILL",
    source_text: str = "",
    history_amounts_minor: Iterable[int] = (),
    community_report_count: int = 0,
    recent_transfer_count: int = 0,
    ai_suspicious: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate extracted fields and persist only server-provided transaction values."""
    moment = _as_utc(now)
    if source_type not in ("BILL", "TRANSFER"):
        raise TransactionFlowError("INVALID_INPUT", http_status=422)
    if payee_id is not None and not isinstance(payee_id, str):
        raise TransactionFlowError("INVALID_INPUT", http_status=422)
    if payee_name is not None and not isinstance(payee_name, str):
        raise TransactionFlowError("INVALID_INPUT", http_status=422)
    if not isinstance(source_text, str):
        raise TransactionFlowError("INVALID_INPUT", http_status=422)
    if due_date is not None:
        if not isinstance(due_date, str):
            raise TransactionFlowError("INVALID_INPUT", http_status=422)
        try:
            date.fromisoformat(due_date)
        except ValueError:
            raise TransactionFlowError("INVALID_INPUT", http_status=422) from None
    if amount_minor is not None and (
        not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor < 0
    ):
        raise TransactionFlowError("INVALID_INPUT", http_status=422)

    decision = evaluate_transaction(
        payee_id=payee_id,
        payee_name=payee_name,
        amount_minor=amount_minor,
        source_text=source_text,
        history_amounts_minor=history_amounts_minor,
        community_report_count=community_report_count,
        recent_transfer_count=recent_transfer_count,
        ai_suspicious=ai_suspicious,
    )
    if decision["outcome"] == "DATA_INSUFFICIENT":
        with atomic(connection):
            _trace_logs(connection, None, decision)
            append_log(
                connection,
                event="decision_recorded",
                risk_code="DATA_INSUFFICIENT",
                status="ASK_AGAIN",
                details={
                    "missing_fields": decision.get("missing_fields", []),
                    "decision_record": decision["decision_record"],
                },
            )
        return {"tx_id": None, **decision}

    if amount_minor is not None and amount_minor > SQLITE_MAX_INTEGER:
        with atomic(connection):
            _trace_logs(connection, None, decision)
            append_log(
                connection,
                event="decision_recorded",
                risk_code="OVER_HARD_CAP",
                status="BLOCKED",
                details={
                    "reason": "AMOUNT_EXCEEDS_SQLITE_INTEGER_RANGE",
                    "decision_record": decision["decision_record"],
                },
            )
        return {"tx_id": None, **decision, "transaction": None}

    transaction_id = uuid.uuid4().hex
    created_at = _timestamp(moment)
    expiry = None
    if decision["outcome"] != "BLOCK":
        expiry = _timestamp(moment + timedelta(seconds=USER_CONFIRMATION_TTL_SECONDS))

    with atomic(connection):
        connection.execute(
            """INSERT INTO transactions
               (id, created_at, updated_at, source_type, payee_id, payee_name,
                amount_minor, currency, due_date, status, outcome_band, risk_code,
                risk_score, decision_json, fields_json, config_version, approval_expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'MYR', ?, 'PENDING', NULL, NULL, 0, '{}', '{}', ?, ?)""",
            (transaction_id, created_at, created_at, source_type, payee_id, payee_name,
             amount_minor, due_date, CONFIG_VERSION, expiry),
        )
        record_decision(connection, transaction_id, decision)
        if decision["outcome"] == "BLOCK":
            connection.execute(
                "UPDATE transactions SET status = 'BLOCKED', approval_expires_at = NULL WHERE id = ? AND status = 'PENDING'",
                (transaction_id,),
            )
            append_log(
                connection,
                event="transaction_blocked",
                tx_id=transaction_id,
                risk_code=decision["risk_code_shown"],
                status="BLOCKED",
                details={"guardian_alert_required": decision["guardian_alert_required"]},
            )
    row = get_transaction(connection, transaction_id)
    return {"tx_id": transaction_id, **decision, "transaction": _payload(row)}


def get_transaction_state(
    connection: Any,
    tx_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    with atomic(connection):
        row, expired = _get_live(connection, tx_id, _as_utc(now))
        return {
            "ok": not expired,
            "error": "EXPIRED" if expired else None,
            "message": ERROR_MESSAGES["EXPIRED"] if expired else None,
            "transaction": _payload(row),
        }


def ask_child(connection: Any, tx_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    moment = _as_utc(now)
    with atomic(connection):
        row, expired = _get_live(connection, tx_id, moment)
        if expired:
            return {"ok": False, "error": "EXPIRED", "transaction": _payload(row)}
        decision = _decision(row)
        if _is_blocked(row, decision):
            raise TransactionFlowError("BLOCKED", http_status=403)
        if row["status"] != "PENDING":
            raise TransactionFlowError("INVALID_STATE")
        if row["outcome_band"] != "REVIEW" or decision.get("outcome_band") != "REVIEW":
            raise TransactionFlowError("NOT_REVIEW")
        expiry = _timestamp(moment + timedelta(seconds=GUARDIAN_APPROVAL_TTL_SECONDS))
        cursor = connection.execute(
            """UPDATE transactions SET status = 'AWAITING_GUARDIAN',
                   approval_expires_at = ?, updated_at = ?
               WHERE id = ? AND status = 'PENDING' AND outcome_band = 'REVIEW'
                 AND risk_code NOT IN ('SCAM_SIGNAL', 'OVER_HARD_CAP')""",
            (expiry, _timestamp(moment), tx_id),
        )
        if cursor.rowcount != 1:
            raise TransactionFlowError("ALREADY_FINAL")
        append_log(
            connection,
            event="guardian_approval_requested",
            tx_id=tx_id,
            risk_code=row["risk_code"],
            status="AWAITING_GUARDIAN",
            details={"expires_at": expiry},
        )
        return {"ok": True, "transaction": _payload(get_transaction(connection, tx_id))}


def confirm_transaction(
    connection: Any,
    tx_id: str,
    *,
    method: str,
    actor_role: str,
    authorization_validator: AuthorizationValidator | None,
    acknowledge_warning: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = _as_utc(now)
    if actor_role != "AH_POR" or method not in ("tap", "voice"):
        raise TransactionFlowError("AUTHORIZATION_REJECTED", http_status=403)
    with atomic(connection):
        row, expired = _get_live(connection, tx_id, moment)
        if expired:
            return {"ok": False, "error": "EXPIRED", "transaction": _payload(row)}
        decision = _decision(row)
        if _is_blocked(row, decision):
            raise TransactionFlowError("BLOCKED", http_status=403)
        if row["status"] == "DONE":
            payment = connection.execute("SELECT * FROM sandbox_payments WHERE tx_id = ?", (tx_id,)).fetchone()
            return {"ok": True, "executed": False, "already_final": True, "transaction": _payload(row), "payment": dict(payment) if payment else None}
        if row["status"] != "PENDING":
            raise TransactionFlowError("ALREADY_FINAL")
        if row["outcome_band"] == "WARN" and not acknowledge_warning:
            raise TransactionFlowError("WARNING_ACK_REQUIRED")
        if row["outcome_band"] not in ("APPROVE", "WARN"):
            raise TransactionFlowError("INVALID_STATE")
        _verify(authorization_validator, row, actor_role, method)
        return _execute(
            connection, row, expected_status="PENDING", role=actor_role, method=method, now=moment
        )


def pay_with_family_mandate(
    connection: Any,
    tx_id: str,
    *,
    authorization_validator: AuthorizationValidator | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pay automatically inside a family-signed limit. Only fully safe (APPROVE) bills qualify."""
    moment = _as_utc(now)
    with atomic(connection):
        row, expired = _get_live(connection, tx_id, moment)
        if expired:
            return {"ok": False, "error": "EXPIRED", "transaction": _payload(row)}
        decision = _decision(row)
        if _is_blocked(row, decision):
            raise TransactionFlowError("BLOCKED", http_status=403)
        if row["status"] != "PENDING" or row["outcome_band"] != "APPROVE":
            raise TransactionFlowError("INVALID_STATE")
        _verify(authorization_validator, row, "FAMILY_MANDATE", "mandate")
        return _execute(
            connection, row, expected_status="PENDING", role="FAMILY_MANDATE", method="mandate", now=moment
        )


def extend_for_call(connection: Any, tx_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Son taps "Call Mum": the 2-minute window gets 5 more minutes, once."""
    moment = _as_utc(now)
    with atomic(connection):
        row, expired = _get_live(connection, tx_id, moment)
        if expired:
            return {"ok": False, "error": "EXPIRED", "transaction": _payload(row)}
        if row["status"] != "AWAITING_GUARDIAN":
            raise TransactionFlowError("INVALID_STATE")
        expiry = _timestamp(moment + timedelta(seconds=GUARDIAN_CALL_EXTENSION_SECONDS))
        cursor = connection.execute(
            """UPDATE transactions SET guardian_extended = 1, approval_expires_at = ?
               WHERE id = ? AND status = 'AWAITING_GUARDIAN' AND guardian_extended = 0""",
            (expiry, tx_id),
        )
        if cursor.rowcount != 1:
            raise TransactionFlowError("INVALID_STATE")  # already extended once
        append_log(connection, event="guardian_call_extension", tx_id=tx_id, method="guardian",
                   risk_code=row["risk_code"], status="AWAITING_GUARDIAN", details={"expires_at": expiry})
        return {"ok": True, "transaction": _payload(get_transaction(connection, tx_id))}


def approve_transaction(
    connection: Any,
    tx_id: str,
    *,
    actor_role: str,
    authorization_validator: AuthorizationValidator | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = _as_utc(now)
    _require_guardian(actor_role)
    with atomic(connection):
        row, expired = _get_live(connection, tx_id, moment)
        if expired:
            return {"ok": False, "error": "EXPIRED", "transaction": _payload(row)}
        decision = _decision(row)
        if _is_blocked(row, decision):
            raise TransactionFlowError("BLOCKED", http_status=403)
        if row["outcome_band"] != "REVIEW" or decision.get("outcome_band") != "REVIEW":
            raise TransactionFlowError("NOT_REVIEW", http_status=403)
        if row["status"] != "AWAITING_GUARDIAN":
            raise TransactionFlowError("INVALID_STATE")
        _verify(authorization_validator, row, actor_role, "guardian")
        return _execute(
            connection, row, expected_status="AWAITING_GUARDIAN",
            role=actor_role, method="guardian", now=moment,
        )


def reject_transaction(
    connection: Any,
    tx_id: str,
    *,
    actor_role: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = _as_utc(now)
    _require_guardian(actor_role)
    with atomic(connection):
        row, expired = _get_live(connection, tx_id, moment)
        if expired:
            return {"ok": False, "error": "EXPIRED", "transaction": _payload(row)}
        decision = _decision(row)
        if _is_blocked(row, decision):
            raise TransactionFlowError("BLOCKED", http_status=403)
        if row["outcome_band"] != "REVIEW" or decision.get("outcome_band") != "REVIEW":
            raise TransactionFlowError("NOT_REVIEW", http_status=403)
        cursor = connection.execute(
            """UPDATE transactions SET status = 'REJECTED', updated_at = ?
               WHERE id = ? AND status = 'AWAITING_GUARDIAN' AND outcome_band = 'REVIEW'
                 AND risk_code NOT IN ('SCAM_SIGNAL', 'OVER_HARD_CAP')""",
            (_timestamp(moment), tx_id),
        )
        if cursor.rowcount != 1:
            raise TransactionFlowError("INVALID_STATE")
        append_log(
            connection, event="guardian_rejected", tx_id=tx_id,
            risk_code=row["risk_code"], method="guardian", status="REJECTED",
        )
        return {"ok": True, "transaction": _payload(get_transaction(connection, tx_id))}


def cancel_transaction(connection: Any, tx_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    moment = _as_utc(now)
    with atomic(connection):
        row, expired = _get_live(connection, tx_id, moment)
        if expired:
            return {"ok": False, "error": "EXPIRED", "transaction": _payload(row)}
        if row["status"] != "PENDING":
            raise TransactionFlowError("INVALID_STATE")
        cursor = connection.execute(
            "UPDATE transactions SET status = 'CANCELLED', updated_at = ? WHERE id = ? AND status = 'PENDING'",
            (_timestamp(moment), tx_id),
        )
        if cursor.rowcount != 1:
            raise TransactionFlowError("ALREADY_FINAL")
        append_log(
            connection, event="transaction_cancelled", tx_id=tx_id,
            risk_code=row["risk_code"], method="tap", status="CANCELLED",
        )
        return {"ok": True, "transaction": _payload(get_transaction(connection, tx_id))}


def dispatch_voice_keyword(
    connection: Any,
    tx_id: str,
    *,
    recognized_keyword: str | None,
    actor_role: str,
    authorization_validator: AuthorizationValidator | None,
    acknowledge_warning: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Dispatch Tech 1's keyword result without transcribing or guessing speech."""
    if actor_role != "AH_POR":
        raise TransactionFlowError("AUTHORIZATION_REJECTED", http_status=403)
    if recognized_keyword == "confirm":
        return confirm_transaction(
            connection,
            tx_id,
            method="voice",
            actor_role=actor_role,
            authorization_validator=authorization_validator,
            acknowledge_warning=acknowledge_warning,
            now=now,
        )
    if recognized_keyword == "cancel":
        return cancel_transaction(connection, tx_id, now=now)

    moment = _as_utc(now)
    with atomic(connection):
        row, expired = _get_live(connection, tx_id, moment)
        append_log(
            connection,
            event="voice_keyword_unrecognized",
            tx_id=tx_id,
            risk_code=row["risk_code"],
            method="voice",
            status=row["status"],
            details={"recognized_keyword": None},
        )
        if expired:
            return {
                "ok": False,
                "executed": False,
                "error": "EXPIRED",
                "message": ERROR_MESSAGES["EXPIRED"],
                "transaction": _payload(row),
            }
        return {
            "ok": True,
            "executed": False,
            "code": "VOICE_UNRECOGNIZED",
            "message": MESSAGES["VOICE_UNRECOGNIZED"]["en"],
            "transaction": _payload(row),
        }


def payment_for_transaction(connection: Any, tx_id: str) -> Mapping[str, Any] | None:
    return connection.execute(
        "SELECT * FROM sandbox_payments WHERE tx_id = ?", (tx_id,)
    ).fetchone()
