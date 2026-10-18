"""Shadow risk model: scores a transaction but never changes the decision.

Trained on PaySim (CC BY-SA 4.0) by ml/train_risk_model.py; weights live in
data/risk_model.json. The rules in rules.py still make every decision. The
score is only logged, the way banks trial a new model before trusting it.

The same features() function is used for training and live scoring, so the
model always sees inputs computed the same way.
"""

from __future__ import annotations

import math
from typing import Any

from db import load_config


FEATURE_NAMES = ("is_transfer", "log_amount_to_balance", "empties_balance", "night_hour")
RATIO_CAP = 10.0


def features(*, is_transfer: bool, amount: float, balance_before: float, hour: int) -> list[float]:
    """Scale-free inputs, so PaySim amounts and RM amounts are comparable."""
    ratio = RATIO_CAP if balance_before <= 0 else min(amount / balance_before, RATIO_CAP)
    return [
        1.0 if is_transfer else 0.0,
        math.log1p(ratio),
        # Sends (almost) the whole balance: the "move all your savings" pattern.
        1.0 if balance_before > 0 and 0.99 <= ratio <= 1.0 else 0.0,
        1.0 if 0 <= hour % 24 < 6 else 0.0,
    ]


def _model() -> dict[str, Any]:
    return load_config("risk_model.json")


def score(*, source_type: str, amount_minor: int, balance_minor: int, hour: int) -> dict[str, Any]:
    model = _model()
    values = features(
        is_transfer=source_type == "TRANSFER",
        amount=amount_minor,
        balance_before=balance_minor,
        hour=hour,
    )
    logit = model["intercept"] + sum(
        weight * (value - mean) / scale
        for weight, value, mean, scale in zip(model["coefficients"], values, model["means"], model["scales"])
    )
    probability = 1.0 / (1.0 + math.exp(-logit))
    return {
        "mode": "shadow",
        "model_version": model["model_version"],
        "score": round(probability, 4),
        "would_flag": probability >= model["threshold"],
        "features": dict(zip(FEATURE_NAMES, values)),
    }
