"""Train the shadow risk model on PaySim and write data/risk_model.json.

Data: PaySim, "Synthetic Financial Datasets For Fraud Detection"
(Lopez-Rojas, Elmir & Axelsson, 2016), Kaggle ealaxi/paysim1, CC BY-SA 4.0.
The data is downloaded to the kagglehub cache and never committed to this repo.

Run from the repo root:
    pip install -r ml/requirements.txt
    python ml/train_risk_model.py            # downloads PaySim with your Kaggle token
    python ml/train_risk_model.py --csv FILE  # or use a local copy
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from risk_model import FEATURE_NAMES, features  # noqa: E402  (same code as live scoring)

OUTPUT = ROOT / "data" / "risk_model.json"


def load_paysim(csv_path: str | None) -> pd.DataFrame:
    if csv_path is None:
        import kagglehub
        folder = Path(kagglehub.dataset_download("ealaxi/paysim1"))
        csv_path = next(folder.glob("*.csv"))
    columns = ["step", "type", "amount", "oldbalanceOrg", "isFraud", "isFlaggedFraud"]
    frame = pd.read_csv(csv_path, usecols=columns)
    # Only the transaction types Teman has: bill payments and transfers.
    return frame[frame["type"].isin(["PAYMENT", "TRANSFER"])].reset_index(drop=True)


def build_matrix(frame: pd.DataFrame) -> np.ndarray:
    rows = zip(frame["type"].to_numpy(), frame["amount"].to_numpy(),
               frame["oldbalanceOrg"].to_numpy(), frame["step"].to_numpy())
    return np.array([
        features(is_transfer=kind == "TRANSFER", amount=amount, balance_before=balance, hour=int(step) % 24)
        for kind, amount, balance, step in rows
    ])


def time_split(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Train on the earliest 70% of hours, pick the threshold on the next 15%, test on the last 15%."""
    cumulative = frame["step"].rank(method="first") / len(frame)
    return (cumulative <= 0.70).to_numpy(), ((cumulative > 0.70) & (cumulative <= 0.85)).to_numpy(), (cumulative > 0.85).to_numpy()


def at_threshold(y_true: np.ndarray, flagged: np.ndarray) -> dict[str, float | int]:
    true_positive = int((flagged & (y_true == 1)).sum())
    precision = true_positive / max(int(flagged.sum()), 1)
    recall = true_positive / max(int((y_true == 1).sum()), 1)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
            "flagged": int(flagged.sum())}


def evaluate(name: str, y_true: np.ndarray, scores: np.ndarray, threshold: float) -> dict[str, object]:
    result = {
        "model": name,
        "pr_auc": round(float(average_precision_score(y_true, scores)), 4),
        "roc_auc": round(float(roc_auc_score(y_true, scores)), 4),
        "threshold": round(float(threshold), 6),
        **at_threshold(y_true, scores >= threshold),
    }
    print(json.dumps(result))
    return result


def best_f1_threshold(y_true: np.ndarray, scores: np.ndarray) -> float:
    precision, recall, thresholds = precision_recall_curve(y_true, scores)
    f1 = 2 * precision[:-1] * recall[:-1] / np.clip(precision[:-1] + recall[:-1], 1e-12, None)
    return float(thresholds[int(np.argmax(f1))])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", help="local PaySim CSV instead of downloading")
    parser.add_argument("--output", default=str(OUTPUT))
    args = parser.parse_args()

    frame = load_paysim(args.csv)
    x = build_matrix(frame)
    y = frame["isFraud"].to_numpy()
    train, validation, test = time_split(frame)
    print(f"rows={len(frame)} fraud={int(y.sum())} train={int(train.sum())} "
          f"validation={int(validation.sum())} test={int(test.sum())}")

    scaler = StandardScaler().fit(x[train])
    logistic = LogisticRegression(max_iter=1000).fit(scaler.transform(x[train]), y[train])
    lr_threshold = best_f1_threshold(y[validation], logistic.predict_proba(scaler.transform(x[validation]))[:, 1])
    lr_test = evaluate("logistic_regression", y[test], logistic.predict_proba(scaler.transform(x[test]))[:, 1], lr_threshold)

    comparisons = [lr_test]
    try:
        import lightgbm
        booster = lightgbm.LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=15, verbose=-1)
        booster.fit(x[train], y[train])
        gb_threshold = best_f1_threshold(y[validation], booster.predict_proba(x[validation])[:, 1])
        comparisons.append(evaluate("lightgbm", y[test], booster.predict_proba(x[test])[:, 1], gb_threshold))
    except ImportError:
        print("lightgbm not installed; skipping comparison")
    # PaySim's own built-in rule (flags transfers over 200,000) as a baseline.
    comparisons.append(evaluate("paysim_builtin_rule", y[test], frame["isFlaggedFraud"].to_numpy()[test].astype(float), 0.5))

    model = {
        "model_version": f"paysim-logreg-{date.today().isoformat()}",
        "mode": "shadow",
        "feature_names": list(FEATURE_NAMES),
        "means": [round(float(v), 8) for v in scaler.mean_],
        "scales": [round(float(v), 8) for v in scaler.scale_],
        "coefficients": [round(float(v), 8) for v in logistic.coef_[0]],
        "intercept": round(float(logistic.intercept_[0]), 8),
        "threshold": round(lr_threshold, 6),
        "training_data": {
            "name": "PaySim: Synthetic Financial Datasets For Fraud Detection",
            "source": "https://www.kaggle.com/datasets/ealaxi/paysim1",
            "authors": "E. A. Lopez-Rojas, A. Elmir, S. Axelsson (2016)",
            "license": "CC BY-SA 4.0",
            "rows_used": int(len(frame)),
            "types_used": ["PAYMENT", "TRANSFER"],
            "fraud_rows": int(y.sum()),
            "split": "by time: first 70% of hours train, next 15% threshold, last 15% test",
        },
        "test_results": comparisons,
        "limitations": "Simulated mobile-money data, not elderly bill payments in Malaysia. "
                       "Shadow mode only: the score never changes a decision.",
    }
    Path(args.output).write_text(json.dumps(model, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
