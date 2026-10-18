"""Deterministic payment checks and fixed response templates."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import unicodedata
from types import MappingProxyType
from typing import Any

from db import atomic, append_log, load_config


CENT = Decimal("0.01")
BLOCKING_SEQUENCE = (
    "BLOCK_MISSING_REQUIRED_FIELDS",
    "BLOCK_SCAM_KEYWORD",
    "BLOCK_BLACKLISTED_PAYEE",
    "BLOCK_OVER_HARD_CAP",
)
SCORING_SEQUENCE = ("PAYEE_UNKNOWN", "OVER_LIMIT", "AMOUNT_HIGH")
ACTION_FLOWS = MappingProxyType({
    "DATA_INSUFFICIENT": ("retake", "type_in"),
    "SCAM_SIGNAL": ("cancel", "check_officially"),
    "OVER_HARD_CAP": ("cancel", "check_officially"),
    "APPROVE": ("confirm", "cancel"),
    "WARN": ("pay_anyway", "cancel"),
    "REVIEW": ("ask_child", "cancel"),
})
NEXT_STEPS = MappingProxyType({
    "DATA_INSUFFICIENT": MappingProxyType({
        "retake": "REANALYZE_REQUIRED",
        "type_in": "REANALYZE_REQUIRED",
    }),
    "SCAM_SIGNAL": MappingProxyType({
        "cancel": "DISMISS_BLOCKED_CARD",
        "check_officially": "SHOW_OFFICIAL_CHANNEL",
    }),
    "OVER_HARD_CAP": MappingProxyType({
        "cancel": "DISMISS_BLOCKED_CARD",
        "check_officially": "SHOW_OFFICIAL_CHANNEL",
    }),
    "APPROVE": MappingProxyType({
        "confirm": "VERIFY_APPROVAL_THEN_EXECUTE",
        "cancel": "PENDING_TO_CANCELLED",
    }),
    "WARN": MappingProxyType({
        "pay_anyway": "REQUIRES_EXPLICIT_WARNING_ACK_THEN_APPROVAL_AND_EXECUTION",
        "cancel": "PENDING_TO_CANCELLED",
    }),
    "REVIEW": MappingProxyType({
        "ask_child": "PENDING_TO_AWAITING_GUARDIAN",
        "cancel": "PENDING_TO_CANCELLED",
    }),
})
GUARDIAN_FOLLOW_UP = MappingProxyType({
    "approve": "AWAITING_GUARDIAN_TO_DONE_ONLY_IF_VALID_AND_STILL_REVIEW",
    "reject": "AWAITING_GUARDIAN_TO_REJECTED",
    "expire": "AWAITING_GUARDIAN_TO_EXPIRED_WITHOUT_PAYMENT",
})


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _load_runtime_config() -> Mapping[str, Any]:
    filenames = {
        "limits": "limits.json",
        "risk_weights": "risk_weights.json",
        "scam_words": "scam_words.json",
        "blacklist": "blacklist.json",
        "whitelist": "whitelist.json",
        "messages": "messages.json",
    }
    configs = {key: load_config(filename) for key, filename in filenames.items()}
    versions = {config.get("config_version") for config in configs.values()}
    if len(versions) != 1 or None in versions:
        raise ValueError("Rule configuration files must share one config_version")

    configured_blocking = tuple(
        rule.get("id") for rule in configs["limits"].get("blocking_rules", ())
    )
    if configured_blocking != BLOCKING_SEQUENCE:
        raise ValueError("limits.json blocking rules have an invalid order or ID")
    if tuple(configs["risk_weights"].get("weights", {})) != SCORING_SEQUENCE:
        raise ValueError("risk_weights.json scoring rules have an invalid order or ID")
    required_templates = {
        "PAYMENT_CONFIRMATION", "DATA_INSUFFICIENT", "SCAM_SIGNAL",
        "OVER_HARD_CAP", "PAYEE_UNKNOWN", "OVER_LIMIT", "AMOUNT_HIGH",
    }
    missing_templates = required_templates - set(configs["messages"].get("templates", {}))
    if missing_templates:
        raise ValueError(f"Missing fixed message templates: {sorted(missing_templates)}")
    return _freeze(configs)


CONFIG = _load_runtime_config()
MESSAGES = CONFIG["messages"]["templates"]
CONFIG_VERSION = CONFIG["limits"]["config_version"]


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be numeric") from None
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _valid_amount_minor(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def outcome_for_score(score: Decimal | int | float | str) -> str:
    """Resolve score against lower-inclusive, upper-exclusive config bands."""
    score_value = _decimal(score, label="score").quantize(CENT, rounding=ROUND_HALF_UP)
    if score_value < 0:
        raise ValueError("Risk score cannot be negative")
    for band in CONFIG["limits"]["risk_score_bands"]:
        lower = _decimal(band["minimum_inclusive"], label="band minimum")
        upper_value = band.get("maximum_exclusive")
        if score_value >= lower and (
            upper_value is None
            or score_value < _decimal(upper_value, label="band maximum")
        ):
            return band["outcome"]
    raise ValueError(f"No configured risk-score band contains {score_value:.2f}")


def _template_text(template_code: str, language: str) -> str:
    template = MESSAGES.get(template_code)
    if template is None or language not in template:
        raise KeyError(f"Missing fixed message template: {template_code}/{language}")
    return template[language]


def _match_whitelist(
    payee_id: str | None,
    payee_name: str | None,
) -> Mapping[str, Any] | None:
    id_matches: set[str] | None = None
    name_matches: set[str] | None = None
    payees = CONFIG["whitelist"]["payees"]

    if payee_id:
        normalized_id = _normalize(payee_id)
        id_matches = {
            str(payee["id"]) for payee in payees
            if _normalize(str(payee["id"])) == normalized_id
        }
    if payee_name:
        normalized_name = _normalize(payee_name)
        name_matches = set()
        for payee in payees:
            names = (payee["display_name"], *payee.get("aliases", ()))
            biller_id = payee.get("official_biller_id")
            if biller_id:
                names = (*names, str(biller_id), f"{payee['display_name']} ({biller_id})")
            if any(_normalize(str(name)) == normalized_name for name in names):
                name_matches.add(str(payee["id"]))

    if id_matches is not None and name_matches is not None:
        matches = id_matches & name_matches
    elif id_matches is not None:
        matches = id_matches
    else:
        matches = name_matches or set()
    if len(matches) != 1:
        return None
    payee_id_match = next(iter(matches))
    return next(payee for payee in payees if payee["id"] == payee_id_match)


def _blacklist_matches(payee_id: str | None, payee_name: str | None) -> tuple[str, ...]:
    candidates = {_normalize(value) for value in (payee_id, payee_name) if value}
    matches = []
    for entry in CONFIG["blacklist"]["payees"]:
        values = {entry["id"], *entry.get("match_values", ())}
        if candidates & {_normalize(str(value)) for value in values}:
            matches.append(str(entry["id"]))
    return tuple(matches)


def _compact(value: str) -> str:
    """Letters and digits only, so "安全 账户", "safe-account" and hidden zero-width spaces still match."""
    return "".join(character for character in value if character.isalnum())


def _scam_matches(source_text: str) -> tuple[str, ...]:
    normalized_text = _normalize(source_text)
    if not normalized_text:
        return ()
    compact_text = _compact(normalized_text)
    return tuple(
        str(group["id"])
        for group in CONFIG["scam_words"]["groups"]
        if any(
            _normalize(str(phrase)) in normalized_text
            or _compact(_normalize(str(phrase))) in compact_text
            for phrase in group["phrases"]
        )
    )


def _sorted_history(values: Iterable[int]) -> list[Decimal]:
    result = []
    for amount in values:
        if not _valid_amount_minor(amount):
            raise ValueError("History values must be non-negative integer minor units")
        result.append(Decimal(amount))
    return sorted(result)


def _median(values: list[Decimal]) -> Decimal:
    midpoint = len(values) // 2
    if len(values) % 2:
        return values[midpoint]
    return (values[midpoint - 1] + values[midpoint]) / Decimal(2)


def _make_result(
    *,
    outcome: str,
    outcome_band: str | None,
    risk_code: str | None,
    template_code: str,
    score: Decimal,
    rules_fired: list[dict[str, Any]],
    rule_trace: list[dict[str, Any]],
    language: str,
    stopped_after: str | None = None,
    guardian_alert_required: bool = False,
) -> dict[str, Any]:
    timestamp = _now_utc()
    score_text = format(score.quantize(CENT, rounding=ROUND_HALF_UP), ".2f")
    record = {
        "rules_fired": rules_fired,
        "weights": {
            rule["rule_id"]: rule["weight"]
            for rule in rules_fired
            if rule["weight"] is not None
        },
        "total_score": score_text,
        "outcome_band": outcome_band,
        "risk_code_shown": risk_code,
        "config_version": CONFIG_VERSION,
        "timestamp": timestamp,
        "outcome": outcome,
        "template_code": template_code,
        "rule_trace": rule_trace,
        "stopped_after": stopped_after,
    }
    action_key = outcome if outcome in ACTION_FLOWS else template_code
    actions = ACTION_FLOWS[action_key]
    return {
        "outcome": outcome,
        "outcome_band": outcome_band,
        "risk_code_shown": risk_code,
        "template_code": template_code,
        "message": _template_text(template_code, language),
        "actions": list(actions),
        "next_steps": [
            {"action": action, "route": NEXT_STEPS[action_key][action]}
            for action in actions
        ],
        "guardian_follow_up": (
            dict(GUARDIAN_FOLLOW_UP) if action_key == "REVIEW" else None
        ),
        "risk_score": score_text,
        "guardian_alert_required": guardian_alert_required,
        "decision_record": record,
    }


def evaluate_transaction(
    *,
    payee_id: str | None,
    payee_name: str | None,
    amount_minor: int | None,
    source_text: str = "",
    history_amounts_minor: Iterable[int] = (),
    language: str = "en",
) -> dict[str, Any]:
    """Run ordered blocking checks, then score every applicable risk rule.

    Inputs are normalized Tech 1 output plus server-held transaction/history data.
    Payment execution and transaction-state changes are deliberately outside this function.
    """
    if not isinstance(source_text, str):
        raise ValueError("source_text must be text")
    if language not in ("en", "zh_yue_draft"):
        raise ValueError("language must identify a configured fixed template")

    rule_trace: list[dict[str, Any]] = []
    rules_fired: list[dict[str, Any]] = []

    def trace(
        rule_id: str,
        risk_code: str,
        matched: bool,
        *,
        phase: str,
        weight: str | None = None,
        match_count: int | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "sequence": len(rule_trace) + 1,
            "rule_id": rule_id,
            "risk_code": risk_code,
            "phase": phase,
            "matched": matched,
        }
        if weight is not None:
            entry["weight"] = weight
        if match_count is not None:
            entry["match_count"] = match_count
        rule_trace.append(entry)
        if matched:
            rules_fired.append({
                "sequence": entry["sequence"],
                "rule_id": rule_id,
                "risk_code": risk_code,
                "weight": weight,
            })

    def blocked(
        outcome: str,
        risk_code: str,
        *,
        stopped_after: str,
        guardian_alert_required: bool = False,
    ) -> dict[str, Any]:
        return _make_result(
            outcome=outcome,
            outcome_band=None,
            risk_code=risk_code,
            template_code=risk_code,
            score=Decimal("0.00"),
            rules_fired=rules_fired,
            rule_trace=rule_trace,
            language=language,
            stopped_after=stopped_after,
            guardian_alert_required=guardian_alert_required,
        )

    missing = []
    if not (isinstance(payee_id, str) and payee_id.strip()) and not (
        isinstance(payee_name, str) and payee_name.strip()
    ):
        missing.append("payee")
    if not _valid_amount_minor(amount_minor) or amount_minor == 0:
        missing.append("amount_minor")
    trace(
        "BLOCK_MISSING_REQUIRED_FIELDS", "DATA_INSUFFICIENT", bool(missing),
        phase="blocking", match_count=len(missing),
    )
    if missing:
        result = blocked(
            "DATA_INSUFFICIENT", "DATA_INSUFFICIENT",
            stopped_after="BLOCK_MISSING_REQUIRED_FIELDS",
        )
        result["missing_fields"] = missing
        return result

    scam_group_ids = _scam_matches(f"{source_text}\n{payee_name or ''}")
    trace(
        "BLOCK_SCAM_KEYWORD", "SCAM_SIGNAL", bool(scam_group_ids),
        phase="blocking", match_count=len(scam_group_ids),
    )
    if scam_group_ids:
        result = blocked(
            "BLOCK", "SCAM_SIGNAL", stopped_after="BLOCK_SCAM_KEYWORD",
            guardian_alert_required=True,
        )
        result["matched_scam_group_ids"] = list(scam_group_ids)
        return result

    blacklisted_ids = _blacklist_matches(payee_id, payee_name)
    trace(
        "BLOCK_BLACKLISTED_PAYEE", "SCAM_SIGNAL", bool(blacklisted_ids),
        phase="blocking", match_count=len(blacklisted_ids),
    )
    if blacklisted_ids:
        result = blocked(
            "BLOCK", "SCAM_SIGNAL", stopped_after="BLOCK_BLACKLISTED_PAYEE",
            guardian_alert_required=True,
        )
        result["matched_blacklist_ids"] = list(blacklisted_ids)
        return result

    amount_value = int(amount_minor)
    hard_cap_minor = CONFIG["limits"]["blocking_rules"][3]["maximum_amount_minor"]
    over_hard_cap = amount_value > hard_cap_minor
    trace("BLOCK_OVER_HARD_CAP", "OVER_HARD_CAP", over_hard_cap, phase="blocking")
    if over_hard_cap:
        return blocked(
            "BLOCK", "OVER_HARD_CAP", stopped_after="BLOCK_OVER_HARD_CAP",
        )

    payee = _match_whitelist(payee_id, payee_name)
    history = _sorted_history(history_amounts_minor)
    history_config = CONFIG["limits"]["history_limits"]
    minimum_history = history_config["minimum_bill_count_for_median"]
    enough_history = len(history) >= minimum_history
    median = _median(history) if history else None
    weights = CONFIG["risk_weights"]["weights"]

    unknown_payee = payee is None
    trace(
        "PAYEE_UNKNOWN", "PAYEE_UNKNOWN", unknown_payee, phase="scoring",
        weight=f"{_decimal(weights['PAYEE_UNKNOWN'], label='PAYEE_UNKNOWN'):.2f}",
    )

    if enough_history:
        assert median is not None
        auto_pay_limit = median * _decimal(
            history_config["auto_pay_limit_median_multiplier"],
            label="auto-pay median multiplier",
        )
    else:
        auto_pay_limit = Decimal(history_config["fallback_auto_pay_limit_minor"])
    over_limit = Decimal(amount_value) > auto_pay_limit
    trace(
        "OVER_LIMIT", "OVER_LIMIT", over_limit, phase="scoring",
        weight=f"{_decimal(weights['OVER_LIMIT'], label='OVER_LIMIT'):.2f}",
    )

    skip_amount_high = (
        not enough_history
        and history_config["skip_high_amount_rule_below_minimum_history"]
    )
    amount_high = False
    if not skip_amount_high:
        assert median is not None
        amount_high = Decimal(amount_value) > median * _decimal(
            history_config["high_amount_median_multiplier"],
            label="high-amount median multiplier",
        )
    trace(
        "AMOUNT_HIGH", "AMOUNT_HIGH", amount_high, phase="scoring",
        weight=f"{_decimal(weights['AMOUNT_HIGH'], label='AMOUNT_HIGH'):.2f}",
    )

    score = sum(
        (_decimal(rule["weight"], label="risk weight") for rule in rules_fired),
        start=Decimal("0"),
    ).quantize(CENT, rounding=ROUND_HALF_UP)
    outcome = outcome_for_score(score)
    if rules_fired:
        highest_weight = max(_decimal(rule["weight"], label="risk weight") for rule in rules_fired)
        primary_rule = next(
            rule for rule in rules_fired
            if _decimal(rule["weight"], label="risk weight") == highest_weight
        )
        risk_code = primary_rule["risk_code"]
        template_code = risk_code
    else:
        risk_code = None
        template_code = "PAYMENT_CONFIRMATION"

    return _make_result(
        outcome=outcome,
        outcome_band=outcome,
        risk_code=risk_code,
        template_code=template_code,
        score=score,
        rules_fired=rules_fired,
        rule_trace=rule_trace,
        language=language,
    )


def record_decision(connection: Any, tx_id: str, decision: Mapping[str, Any]) -> None:
    """Atomically persist a decision and ordered, privacy-minimal rule events."""
    record = decision["decision_record"]
    serialized = json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    with atomic(connection):
        cursor = connection.execute(
            """UPDATE transactions
               SET risk_code = ?, risk_score = ?, outcome_band = ?,
                   decision_json = ?, config_version = ?, updated_at = ?
               WHERE id = ? AND status IN ('PENDING', 'AWAITING_GUARDIAN')""",
            (
                decision["risk_code_shown"], float(Decimal(decision["risk_score"])),
                decision["outcome_band"], serialized, record["config_version"],
                record["timestamp"], tx_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("Decision requires an existing pending transaction")

        for rule in record["rule_trace"]:
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
        append_log(
            connection,
            event="decision_recorded",
            tx_id=tx_id,
            risk_code=decision["risk_code_shown"],
            status=decision["outcome"],
            details={
                "risk_score": decision["risk_score"],
                "outcome_band": decision["outcome_band"],
                "config_version": record["config_version"],
                "timestamp": record["timestamp"],
            },
        )