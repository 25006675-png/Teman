"""Simulated bank sandbox: Ah Por's balance.

Balance = opening balance (data/bank.json) - every executed sandbox payment.
sandbox_payments is append-only, so the balance can't be edited directly.
"""

from __future__ import annotations

from typing import Any

from db import load_config


def opening_balance_minor() -> int:
    return int(load_config("bank.json")["opening_balance_minor"])


def balance_minor(connection: Any) -> int:
    paid = connection.execute("SELECT COALESCE(SUM(amount_minor), 0) FROM sandbox_payments").fetchone()[0]
    return opening_balance_minor() - int(paid)
