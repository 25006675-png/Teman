"""Scam reports shared across families, plus the recent-transfer count.

When a son marks a request "This is a scam", the receiving account is
recorded. Other families paying that account then need review (1 report) or
are blocked (2+ reports from different families). Official billers on the
whitelist can never be reported.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import unicodedata

from db import append_log, atomic, load_config

# The demo has one family; a real launch takes this from the signed-in account.
DEMO_FAMILY_ID = "tan_family"


def payee_key(payee_id: str | None, payee_name: str | None) -> str | None:
    value = payee_id or payee_name
    if not value or not value.strip():
        return None
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def report_count(connection: Any, key: str | None) -> int:
    if key is None:
        return 0
    return connection.execute(
        "SELECT COUNT(DISTINCT family_id) FROM scam_reports WHERE payee_key = ?", (key,)
    ).fetchone()[0]


def add_report(connection: Any, *, key: str, payee_name: str | None, tx_id: str, family_id: str) -> int:
    """Record one family's report (repeat reports from a family count once)."""
    with atomic(connection):
        connection.execute(
            """INSERT INTO scam_reports (payee_key, payee_name, family_id, tx_id, reported_at)
               VALUES (?, ?, ?, ?, ?) ON CONFLICT(payee_key, family_id) DO NOTHING""",
            (key, payee_name, family_id, tx_id,
             datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")),
        )
        count = report_count(connection, key)
        append_log(connection, event="scam_reported", tx_id=tx_id, method="guardian", status="REPORTED",
                   details={"payee_key": key, "report_count": count})
    return count


def list_reports(connection: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        """SELECT payee_key, MAX(payee_name) AS payee_name, COUNT(DISTINCT family_id) AS report_count,
                  MAX(reported_at) AS last_reported_at
           FROM scam_reports GROUP BY payee_key ORDER BY last_reported_at DESC"""
    )]


def recent_transfer_count(connection: Any, now: datetime | None = None) -> int:
    """Transfers sent (or waiting for the son) in the configured window."""
    config = load_config("limits.json")["rapid_transfers"]
    since = (now or datetime.now(timezone.utc)) - timedelta(hours=config["window_hours"])
    statuses = config["counted_statuses"]
    return connection.execute(
        f"""SELECT COUNT(*) FROM transactions
            WHERE source_type = 'TRANSFER' AND created_at >= ?
              AND status IN ({",".join("?" for _ in statuses)})""",
        (since.isoformat(timespec="milliseconds").replace("+00:00", "Z"), *statuses),
    ).fetchone()[0]
