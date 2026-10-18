"""Signed approval records (Family Mandate Protocol, PROPOSAL §6d).

When Ah Por confirms or her son approves, the server signs an approval record
with that role's ECDSA P-256 key. Just before payment, transactions.py calls
the verifier, which checks:
signature valid, fields match the stored transaction, role allowed for the
outcome, not expired, and the nonce has never been used.

Demo limitation (known risk): the server holds every role's private key.
A real launch keeps each key on that person's device.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from db import ROOT, append_log, atomic


KEY_DIR = Path(os.environ.get("DIALECTGUARD_KEY_DIR", ROOT / "instance" / "keys"))
ALGORITHM = "ECDSA_P256_SHA256"
APPROVAL_TTL_SECONDS = 60
# Which outcome each role may approve. Only the son can resolve "Review".
ROLE_DECISIONS = {"AH_POR": ("APPROVE", "WARN"), "GUARDIAN": ("REVIEW",)}
# transactions.py calls the son's approval method "guardian"; he taps a button.
SIGNED_METHOD = {"tap": "tap", "voice": "voice", "guardian": "tap"}
SIGNED_FIELDS = (
    "tx_id", "payee_id", "payee_name", "amount_minor", "decision",
    "role", "method", "issued_at", "expires_at", "nonce",
)


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _private_key(role: str) -> ec.EllipticCurvePrivateKey:
    """Load the role's demo key, creating it on first use."""
    path = KEY_DIR / f"{role.lower()}.pem"
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    return key


def canonical_bytes(record: Mapping[str, Any]) -> bytes:
    """The exact bytes that get signed: fixed fields, sorted keys, no whitespace."""
    payload = {field: record.get(field) for field in SIGNED_FIELDS}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def issue_approval(
    connection: Any,
    transaction: Mapping[str, Any],
    *,
    role: str,
    method: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Sign and store an approval for the transaction's stored values.

    Returns None when this role may not approve this outcome (for example a
    blocked transaction); the payment call then fails with its own error.
    """
    decision = transaction["outcome_band"]
    if decision not in ROLE_DECISIONS.get(role, ()) or method not in SIGNED_METHOD:
        return None
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    record = {
        "tx_id": transaction["id"],
        "payee_id": transaction["payee_id"],
        "payee_name": transaction["payee_name"],
        "amount_minor": transaction["amount_minor"],
        "decision": decision,
        "role": role,
        "method": SIGNED_METHOD[method],
        "issued_at": _timestamp(moment),
        "expires_at": _timestamp(moment + timedelta(seconds=APPROVAL_TTL_SECONDS)),
        "nonce": secrets.token_hex(16),
    }
    signature = _private_key(role).sign(canonical_bytes(record), ec.ECDSA(hashes.SHA256()))
    approval = {**record, "algorithm": ALGORITHM, "signature": base64.b64encode(signature).decode("ascii")}
    with atomic(connection):
        connection.execute(
            """INSERT INTO mandates
               (id, mandate_type, tx_id, payee_id, amount_minor, decision, role, method,
                created_at, expires_at, nonce, algorithm, signature, status, record_json)
               VALUES (?, 'PRESENT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ISSUED', ?)""",
            (
                record["nonce"], record["tx_id"], record["payee_id"], record["amount_minor"],
                decision, role, record["method"], record["issued_at"], record["expires_at"],
                record["nonce"], ALGORITHM, approval["signature"], canonical_bytes(record).decode("utf-8"),
            ),
        )
        append_log(
            connection, event="approval_signed", tx_id=record["tx_id"],
            method=record["method"], status="ISSUED",
            details={"role": role, "nonce": record["nonce"], "algorithm": ALGORITHM},
        )
    return approval


def verify_approval(
    connection: Any,
    approval: Mapping[str, Any] | None,
    transaction: Mapping[str, Any],
    role: str,
    method: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Check an approval against the stored transaction and use up its nonce.

    Call inside the payment's database transaction, so a failed payment also
    rolls back the nonce use.
    """
    if not approval:
        return False
    try:
        signature = base64.b64decode(approval["signature"], validate=True)
        _private_key(role).public_key().verify(
            signature, canonical_bytes(approval), ec.ECDSA(hashes.SHA256())
        )
        expired = (now or datetime.now(timezone.utc)) >= _parse(approval["expires_at"])
    except (InvalidSignature, KeyError, TypeError, ValueError):
        return False
    fields_match = (
        approval.get("tx_id") == transaction["id"]
        and approval.get("payee_id") == transaction["payee_id"]
        and approval.get("payee_name") == transaction["payee_name"]
        and approval.get("amount_minor") == transaction["amount_minor"]
        and approval.get("decision") == transaction["outcome_band"]
        and approval.get("role") == role
        and approval.get("decision") in ROLE_DECISIONS.get(role, ())
        and approval.get("method") == SIGNED_METHOD.get(method)
    )
    if not fields_match or expired:
        return False
    used_at = _timestamp(now or datetime.now(timezone.utc))
    cursor = connection.execute(
        """UPDATE mandates SET status = 'USED', used_at = ?
           WHERE nonce = ? AND signature = ? AND status = 'ISSUED'""",
        (used_at, approval["nonce"], approval["signature"]),
    )
    return cursor.rowcount == 1


def signed_validator(connection: Any, approval: Mapping[str, Any] | None, *, now: datetime | None = None):
    """Adapt verify_approval to the validator shape transactions.py expects."""
    def validator(transaction: Mapping[str, Any], role: str, method: str) -> bool:
        return verify_approval(connection, approval, transaction, role, method, now=now)
    return validator


def basic_validator(transaction: Mapping[str, Any], role: str, method: str) -> bool:
    """Backup with no signatures: role and outcome check only.

    Use only if signed approvals break before the demo
    (set DIALECTGUARD_APPROVAL_MODE=basic). transactions.py still enforces
    state, blocking rules, expiry and double-charge protection.
    """
    return transaction["outcome_band"] in ROLE_DECISIONS.get(role, ()) and method in SIGNED_METHOD
