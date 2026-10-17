"""Ordered JSON export for append-only audit events and decision records."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from db import utc_now


EXPORT_FORMAT = "dialectguard.logs.v1"


def _decode_object(raw_value: str, *, field: str, row_id: int | str) -> dict[str, Any]:
    try:
        decoded = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid JSON in {field} for record {row_id}") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"Expected a JSON object in {field} for record {row_id}")
    return decoded


def build_logs_export(connection: Any) -> dict[str, Any]:
    """Return a stable, chronological JSON-ready bundle of logs and decisions."""
    log_rows = connection.execute(
        """SELECT id, occurred_at, tx_id, event, risk_code, method, status, details_json
           FROM logs ORDER BY id ASC"""
    ).fetchall()
    logs = []
    standalone_decisions = []
    for row in log_rows:
        details = _decode_object(
            row["details_json"], field="logs.details_json", row_id=row["id"]
        )
        logs.append({
            "event_id": row["id"],
            "occurred_at": row["occurred_at"],
            "tx_id": row["tx_id"],
            "event": row["event"],
            "risk_code": row["risk_code"],
            "method": row["method"],
            "status": row["status"],
            "details": details,
        })
        if row["event"] == "decision_recorded" and row["tx_id"] is None:
            decision_record = details.get("decision_record")
            if isinstance(decision_record, dict):
                standalone_decisions.append({
                    "tx_id": None,
                    "recorded_at": row["occurred_at"],
                    "status": row["status"],
                    "risk_code": row["risk_code"],
                    "decision_record": decision_record,
                })

    transaction_rows = connection.execute(
        """SELECT id, created_at, status, risk_code, risk_score, outcome_band,
                  decision_json
           FROM transactions
           WHERE decision_json != '{}' AND decision_json IS NOT NULL
           ORDER BY created_at ASC, id ASC"""
    ).fetchall()
    decisions = []
    for row in transaction_rows:
        record = _decode_object(
            row["decision_json"], field="transactions.decision_json", row_id=row["id"]
        )
        decisions.append({
            "tx_id": row["id"],
            "created_at": row["created_at"],
            "status": row["status"],
            "risk_code": row["risk_code"],
            "risk_score": row["risk_score"],
            "outcome_band": row["outcome_band"],
            "decision_record": record,
        })
    decisions.extend(standalone_decisions)
    decisions.sort(key=lambda item: (item["recorded_at"] if "recorded_at" in item else item["created_at"], item["tx_id"] or ""))

    return {
        "format": EXPORT_FORMAT,
        "exported_at": utc_now(),
        "event_count": len(logs),
        "decision_count": len(decisions),
        "logs": logs,
        "decisions": decisions,
    }


def export_logs_json(connection: Any) -> str:
    """Serialize the `/logs` export payload as readable UTF-8 JSON text."""
    return json.dumps(
        build_logs_export(connection),
        ensure_ascii=False,
        indent=2,
        separators=(",", ": "),
    )
