"""Decision map: what the real rules decide for every amount, in each typical situation.

Runs rules.evaluate_transaction (the same function the server uses) for amounts from
RM 0.01 to RM 1,000 and finds the exact amount, to the sen, where the outcome changes.
No server, no AI call: the AI signal is passed in as a yes/no input, as the server does.

    python eval/run_decision_map.py      ->  eval/results/decision_map.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rules import evaluate_transaction  # noqa: E402

RESULTS = ROOT / "eval" / "results" / "decision_map.json"
MAX_MINOR = 100_000  # RM 1,000
HISTORY = json.loads((ROOT / "data" / "history.json").read_text(encoding="utf-8"))["records"]


def history(payee_id: str) -> list[int]:
    return [r["amount_minor"] for r in HISTORY if r["payee_id"] == payee_id]


SCENARIOS = [
    {"label": "TNB bill, 3 past bills", "payee_id": "tnb", "payee_name": "Tenaga Nasional Berhad",
     "history_amounts_minor": history("tnb")},
    {"label": "Air Selangor bill, 1 past bill", "payee_id": "air_selangor", "payee_name": "Air Selangor",
     "history_amounts_minor": history("air_selangor")},
    {"label": "Recognised biller, AI flags a tactic", "payee_id": "tnb", "payee_name": "Tenaga Nasional Berhad",
     "history_amounts_minor": history("tnb"), "ai_suspicious": True},
    {"label": "New payee (never paid before)", "payee_id": None, "payee_name": "Klinik Baru (demo)"},
    {"label": "Transfer, 2 transfers already today", "payee_id": None, "payee_name": "Tan Mei Ling",
     "recent_transfer_count": 2},
    {"label": "Payee reported by 1 family", "payee_id": None, "payee_name": "Lim Holdings 5521 (demo)",
     "community_report_count": 1},
    {"label": "Payee reported by 2 families", "payee_id": None, "payee_name": "Lim Holdings 5521 (demo)",
     "community_report_count": 2},
    {"label": "Message contains a scam phrase", "payee_id": None, "payee_name": "Acct #9910",
     "source_text": "Transfer to the safe account to unfreeze your prize."},
]


def decide(scenario: dict, amount_minor: int) -> dict:
    args = {k: v for k, v in scenario.items() if k != "label"}
    result = evaluate_transaction(amount_minor=amount_minor, **args)
    record = result["decision_record"] if "decision_record" in result else result
    return {"band": result["outcome"] if result["outcome"] in ("BLOCK", "ASK_AGAIN") else result["outcome_band"],
            "score": record.get("total_score"), "fired": sorted(record.get("weights", {}))}


def segments(scenario: dict) -> list[dict]:
    """Scan in 10-sen steps, then binary-search each change to the exact sen."""
    out = []
    start, current = 1, decide(scenario, 1)
    previous = 1
    for amount in range(10, MAX_MINOR + 1, 10):
        now = decide(scenario, amount)
        if now["band"] != current["band"] or now["fired"] != current["fired"]:
            low, high = previous, amount  # change happens in (low, high]
            while high - low > 1:
                mid = (low + high) // 2
                m = decide(scenario, mid)
                if m["band"] == current["band"] and m["fired"] == current["fired"]:
                    low = mid
                else:
                    high = mid
            out.append({"from_minor": start, "to_minor": low, **current})
            start, current = high, decide(scenario, high)
        previous = amount
    out.append({"from_minor": start, "to_minor": MAX_MINOR, **current})
    return out


def main() -> None:
    rows = [{"label": s["label"], "segments": segments(s)} for s in SCENARIOS]
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps({"max_minor": MAX_MINOR, "rows": rows}, indent=1), encoding="utf-8")
    for row in rows:
        print(row["label"])
        for seg in row["segments"]:
            print(f"   RM {seg['from_minor'] / 100:>8.2f} - {seg['to_minor'] / 100:>8.2f}  {seg['band']:<8} "
                  f"score {seg['score']}  {'+'.join(seg['fired'])}")


if __name__ == "__main__":
    main()
