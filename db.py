"""SQLite persistence for DialectGuard's demo backend."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import count
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DEFAULT_DB_PATH = ROOT / "instance" / "dialectguard.sqlite3"
SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 10_000
_savepoint_ids = count(1)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _database_path(path: str | os.PathLike[str] | None) -> str:
    if path is not None:
        raw_path = os.fspath(path)
    else:
        raw_path = os.environ.get("DIALECTGUARD_DB_PATH", str(DEFAULT_DB_PATH))
    if raw_path != ":memory:":
        Path(raw_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        return str(Path(raw_path).expanduser().resolve())
    return raw_path


def connect_db(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Open a configured connection. The caller is responsible for closing it."""
    connection = sqlite3.connect(
        _database_path(path),
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


@contextmanager
def db_session(path: str | os.PathLike[str] | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a short-lived connection and always close it."""
    connection = connect_db(path)
    try:
        yield connection
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


@contextmanager
def atomic(connection: sqlite3.Connection, *, immediate: bool = True) -> Iterator[None]:
    """Commit a unit of work, rolling it back on any exception; supports nesting."""
    if connection.in_transaction:
        savepoint = f"dg_sp_{next(_savepoint_ids)}"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except BaseException:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        return

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def load_config(filename: str) -> dict[str, Any]:
    """Load a JSON config from data/, rejecting path traversal and malformed JSON."""
    if Path(filename).name != filename or not filename.endswith(".json"):
        raise ValueError("Config must be a JSON filename directly inside data/")
    config_path = DATA_DIR / filename
    with config_path.open("r", encoding="utf-8") as config_file:
        return json.load(config_file)


def _schema_sql() -> str:
    return """
    BEGIN IMMEDIATE;

    CREATE TABLE IF NOT EXISTS schema_info (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        schema_version INTEGER NOT NULL,
        initialized_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(id)) BETWEEN 1 AND 100),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        source_type TEXT NOT NULL CHECK (source_type IN ('BILL', 'TRANSFER')),
        payee_id TEXT,
        payee_name TEXT,
        amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
        currency TEXT NOT NULL DEFAULT 'MYR' CHECK (currency = 'MYR'),
        due_date TEXT,
        status TEXT NOT NULL CHECK (status IN (
            'PENDING', 'AWAITING_GUARDIAN', 'DONE', 'CANCELLED',
            'BLOCKED', 'REJECTED', 'EXPIRED'
        )),
        outcome_band TEXT CHECK (outcome_band IN ('APPROVE', 'WARN', 'REVIEW', 'BLOCK')),
        risk_code TEXT,
        risk_score INTEGER NOT NULL DEFAULT 0 CHECK (risk_score >= 0),
        decision_json TEXT NOT NULL DEFAULT '{}',
        fields_json TEXT NOT NULL DEFAULT '{}',
        config_version TEXT NOT NULL,
        approval_expires_at TEXT,
        completed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS history (
        id TEXT PRIMARY KEY NOT NULL,
        occurred_at TEXT NOT NULL,
        payee_id TEXT,
        payee_name TEXT NOT NULL,
        category TEXT NOT NULL,
        amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
        currency TEXT NOT NULL DEFAULT 'MYR' CHECK (currency = 'MYR'),
        source TEXT NOT NULL CHECK (source IN ('SYNTHETIC_DEMO', 'SANDBOX_TRANSACTION')),
        synthetic INTEGER NOT NULL CHECK (synthetic IN (0, 1))
    );

    CREATE TABLE IF NOT EXISTS budget_limits (
        period_start TEXT PRIMARY KEY NOT NULL,
        period_end TEXT NOT NULL,
        daily_limit_minor INTEGER CHECK (daily_limit_minor IS NULL OR daily_limit_minor >= 0),
        remaining_minor INTEGER NOT NULL,
        essential_costs_minor INTEGER NOT NULL CHECK (essential_costs_minor >= 0),
        status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'BUDGET_GAP')),
        reason_json TEXT NOT NULL DEFAULT '{}',
        calculated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sandbox_payments (
        tx_id TEXT PRIMARY KEY NOT NULL,
        payee_id TEXT,
        payee_name TEXT,
        amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
        currency TEXT NOT NULL DEFAULT 'MYR' CHECK (currency = 'MYR'),
        executed_at TEXT NOT NULL,
        method TEXT NOT NULL CHECK (method IN ('tap', 'voice', 'guardian')),
        FOREIGN KEY (tx_id) REFERENCES transactions(id) ON UPDATE RESTRICT ON DELETE RESTRICT
    );

    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        occurred_at TEXT NOT NULL,
        tx_id TEXT,
        event TEXT NOT NULL,
        risk_code TEXT,
        method TEXT CHECK (method IN ('voice', 'tap', 'system', 'guardian') OR method IS NULL),
        status TEXT,
        details_json TEXT NOT NULL DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS mandates (
        id TEXT PRIMARY KEY NOT NULL,
        mandate_type TEXT NOT NULL CHECK (mandate_type IN ('DELEGATED', 'PRESENT')),
        tx_id TEXT,
        payee_id TEXT,
        amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
        decision TEXT NOT NULL CHECK (decision IN ('APPROVE', 'WARN', 'REVIEW')),
        role TEXT NOT NULL CHECK (role IN ('AH_POR', 'GUARDIAN', 'FAMILY_SETUP')),
        method TEXT NOT NULL CHECK (method IN ('tap', 'voice', 'setup')),
        created_at TEXT NOT NULL,
        expires_at TEXT,
        nonce TEXT NOT NULL UNIQUE,
        algorithm TEXT NOT NULL DEFAULT 'ECDSA_P256_SHA256',
        signature TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('ISSUED', 'USED', 'REVOKED', 'EXPIRED')),
        used_at TEXT,
        record_json TEXT NOT NULL DEFAULT '{}',
        CHECK (
            (mandate_type = 'DELEGATED' AND tx_id IS NULL AND payee_id IS NOT NULL)
            OR (mandate_type = 'PRESENT' AND tx_id IS NOT NULL)
        ),
        FOREIGN KEY (tx_id) REFERENCES transactions(id) ON UPDATE RESTRICT ON DELETE RESTRICT
    );

    CREATE TABLE IF NOT EXISTS scam_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        payee_key TEXT NOT NULL,
        payee_name TEXT,
        family_id TEXT NOT NULL,
        tx_id TEXT,
        reported_at TEXT NOT NULL,
        UNIQUE (payee_key, family_id)
    );

    CREATE INDEX IF NOT EXISTS idx_transactions_status_created
        ON transactions(status, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_transactions_payee_created
        ON transactions(payee_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_transactions_expiry
        ON transactions(status, approval_expires_at);
    CREATE INDEX IF NOT EXISTS idx_transactions_risk_created
        ON transactions(risk_code, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_transactions_status_completed
        ON transactions(status, completed_at);
    CREATE INDEX IF NOT EXISTS idx_sandbox_payments_executed
        ON sandbox_payments(executed_at DESC);
    CREATE INDEX IF NOT EXISTS idx_history_payee_date
        ON history(payee_id, occurred_at DESC);
    CREATE INDEX IF NOT EXISTS idx_history_date
        ON history(occurred_at DESC);
    CREATE INDEX IF NOT EXISTS idx_history_category_date
        ON history(category, occurred_at DESC);
    CREATE INDEX IF NOT EXISTS idx_logs_tx_time
        ON logs(tx_id, occurred_at DESC);
    CREATE INDEX IF NOT EXISTS idx_logs_time
        ON logs(occurred_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS idx_mandates_tx_status
        ON mandates(tx_id, status);
    CREATE INDEX IF NOT EXISTS idx_mandates_payee_type
        ON mandates(payee_id, mandate_type, status);

    CREATE TRIGGER IF NOT EXISTS prevent_log_update
    BEFORE UPDATE ON logs
    BEGIN
        SELECT RAISE(ABORT, 'logs are append-only');
    END;

    CREATE TRIGGER IF NOT EXISTS prevent_log_delete
    BEFORE DELETE ON logs
    BEGIN
        SELECT RAISE(ABORT, 'logs are append-only');
    END;

    CREATE TRIGGER IF NOT EXISTS prevent_sandbox_payment_update
    BEFORE UPDATE ON sandbox_payments
    BEGIN
        SELECT RAISE(ABORT, 'sandbox payments are append-only');
    END;

    CREATE TRIGGER IF NOT EXISTS prevent_sandbox_payment_delete
    BEFORE DELETE ON sandbox_payments
    BEGIN
        SELECT RAISE(ABORT, 'sandbox payments are append-only');
    END;

    DROP TRIGGER IF EXISTS enforce_transaction_state_transition;
    CREATE TRIGGER enforce_transaction_state_transition
    BEFORE UPDATE OF status ON transactions
    WHEN NOT (
        OLD.status = NEW.status
        OR (OLD.status = 'PENDING' AND NEW.status IN (
            'AWAITING_GUARDIAN', 'DONE', 'CANCELLED', 'BLOCKED', 'EXPIRED'
        ))
        OR (OLD.status = 'AWAITING_GUARDIAN' AND NEW.status IN (
            'DONE', 'REJECTED', 'EXPIRED'
        ))
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid transaction state transition');
    END;

    COMMIT;
    """


def seed_demo_history(connection: sqlite3.Connection) -> int:
    """Insert the synthetic bill history once, leaving later runtime history intact."""
    if connection.execute("SELECT 1 FROM history LIMIT 1").fetchone():
        return 0
    config = load_config("history.json")
    records = config.get("records")
    if not isinstance(records, list):
        raise ValueError("history.json must contain a records array")
    rows = []
    for record in records:
        rows.append((
            record["id"], record["occurred_at"], record.get("payee_id"),
            record["payee_name"], record["category"], record["amount_minor"],
            record.get("currency", "MYR"), record.get("source", "SYNTHETIC_DEMO"),
            int(record.get("synthetic", True)),
        ))
    with atomic(connection):
        connection.executemany(
            """INSERT INTO history
               (id, occurred_at, payee_id, payee_name, category, amount_minor,
                currency, source, synthetic)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    return len(rows)


def initialize_database(
    path: str | os.PathLike[str] | None = None,
    *,
    seed_history: bool = True,
) -> str:
    """Create/verify the schema, enable WAL, and optionally seed synthetic history."""
    database_path = _database_path(path)
    connection = connect_db(database_path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA wal_autocheckpoint = 1000")
        connection.executescript(_schema_sql())
        with atomic(connection):
            schema_row = connection.execute(
                "SELECT schema_version FROM schema_info WHERE id = 1"
            ).fetchone()
            if schema_row is None:
                connection.execute(
                    "INSERT INTO schema_info (id, schema_version, initialized_at) "
                    "VALUES (1, ?, ?)",
                    (SCHEMA_VERSION, utc_now()),
                )
            elif schema_row[0] < SCHEMA_VERSION:
                connection.execute(
                    "UPDATE schema_info SET schema_version = ?, initialized_at = ? WHERE id = 1",
                    (SCHEMA_VERSION, utc_now()),
                )
        version = connection.execute(
            "SELECT schema_version FROM schema_info WHERE id = 1"
        ).fetchone()[0]
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {version} is not supported; expected {SCHEMA_VERSION}"
            )
        if seed_history:
            seed_demo_history(connection)
        return database_path
    finally:
        connection.close()


def reset_database(path: str | os.PathLike[str] | None = None) -> str:
    """Drop every table and rebuild the demo state (for POST /reset).

    Logs and payments block DELETE, but DROP TABLE skips those triggers.
    """
    connection = connect_db(path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        with atomic(connection):
            for table in ("sandbox_payments", "mandates", "logs", "budget_limits", "scam_reports",
                          "history", "transactions", "schema_info"):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
    finally:
        connection.close()
    return initialize_database(path)


def append_log(
    connection: sqlite3.Connection,
    *,
    event: str,
    tx_id: str | None = None,
    risk_code: str | None = None,
    method: str | None = None,
    status: str | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    """Append a minimal audit event; do not put raw audio or unnecessary PII in details."""
    with atomic(connection):
        cursor = connection.execute(
            """INSERT INTO logs
               (occurred_at, tx_id, event, risk_code, method, status, details_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                utc_now(), tx_id, event, risk_code, method, status,
                json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        return int(cursor.lastrowid)


def get_transaction(connection: sqlite3.Connection, tx_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM transactions WHERE id = ?", (tx_id,)
    ).fetchone()


def get_history_for_payee(
    connection: sqlite3.Connection,
    payee_id: str,
) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT id, occurred_at, amount_minor, category
           FROM history WHERE payee_id = ? ORDER BY occurred_at DESC""",
        (payee_id,),
    ).fetchall()


if __name__ == "__main__":
    print(initialize_database())