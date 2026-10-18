"""Scam-detection evaluation: rules alone vs rules + AI scam analyst.

Each labelled request in eval/scam_messages.json goes through the same
services.analyze() path the app uses, on a fresh synthetic demo database:
1. rules only (the AI check is switched off), and
2. rules + AI (real Gemini calls; answers are cached in
   eval/results/ai_cache.json so re-runs don't call the API again).

"Stopped" means the payment can't go through without her son (REVIEW) or at
all (BLOCK). The dataset was written by a separate assistant that never saw
the rules. It is still synthetic: read eval/SCAM_EVAL.md before quoting numbers.

Run from the repo root:
    python eval/run_scam_eval.py              # both passes (needs GEMINI_API_KEY for pass 2)
    python eval/run_scam_eval.py --rules-only # no network
"""

from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATASET = ROOT / "eval" / "scam_messages.json"
RESULTS_DIR = ROOT / "eval" / "results"
CACHE = RESULTS_DIR / "ai_cache.json"

# Never touch the demo database.
os.environ["DIALECTGUARD_DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="dg_eval_")) / "eval.sqlite3")

import ai_analyst  # noqa: E402
import db  # noqa: E402
import ocr  # noqa: E402
import services  # noqa: E402

STOPPED = ("BLOCK", "REVIEW")
HARD_CAP_MINOR = 50000


def load_dataset(path: Path = DATASET) -> list[dict]:
    items = json.loads(path.read_text(encoding="utf-8"))
    for item in items:
        amount = Decimal(item["amount_rm"])
        if amount <= 0 or amount != amount.quantize(Decimal("0.01")):
            raise ValueError(f"{item['id']}: amount_rm must be positive with 2 decimals")
        item["amount_minor"] = int(amount * 100)
    return items


# The OCR step turns a recognised biller into its standard id and name
# (ocr.PAYEE_INFO), so a bill printed "Pengurusan Air Selangor Sdn Bhd"
# reaches the rules as "Air Selangor". Mirror that here.
CANONICAL_NAMES = {payee_id: name for payee_id, name in ocr.PAYEE_INFO.values()}


def fields_for(item: dict) -> dict:
    return {
        "payee_id": item["payee_id"],
        "payee_name": CANONICAL_NAMES.get(item["payee_id"], item["payee_name"]),
        "amount_minor": item["amount_minor"], "due_date": None,
        "source_type": item["source_type"], "source_text": item["text"],
    }


def run_item(item: dict, assess) -> dict:
    """Analyse one request on a fresh demo database with the given AI function."""
    db.reset_database()
    original = ai_analyst.assess
    ai_analyst.assess = assess
    try:
        with db.db_session() as connection:
            result = services.analyze(connection, fields_for(item))
    finally:
        ai_analyst.assess = original
    record = result["decision_record"]
    ai = result.get("ai_assessment") or {}
    return {
        "outcome": result["outcome"],
        "risk_code_shown": result["risk_code_shown"],
        "risk_score": result["risk_score"],
        "rules_fired": [rule["rule_id"] for rule in record["rules_fired"]],
        "stopped_after": record.get("stopped_after"),
        "ai_status": ai.get("status"),
        "ai_suspicious": ai.get("suspicious"),
        "ai_tactics": ai.get("tactics", []),
    }


def rules_only(_context: dict) -> dict:
    return ai_analyst.skipped("evaluation: rules only")


original_assess = ai_analyst.assess


def make_cached_ai(cache: dict, item_id: str, delay: float):
    def assess(context: dict) -> dict:
        cached = cache.get(item_id)
        if cached and cached.get("status") == "ok":
            return cached
        answer = original_assess(context)
        cache[item_id] = answer
        time.sleep(delay)  # stay under the free-tier rate limit
        return answer
    return assess


def pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100 * part / whole:.0f}%)" if whole else "0/0"


def summarise(items: list[dict], mode: str) -> list[tuple[str, str]]:
    scams = [i for i in items if i["label"] == "scam"]
    genuine = [i for i in items if i["label"] == "genuine"]
    small_scams = [i for i in scams if i["amount_minor"] <= HARD_CAP_MINOR]
    official = [i for i in genuine if i["payee_id"]]
    other = [i for i in genuine if not i["payee_id"]]

    def count(group: list[dict], outcomes: tuple[str, ...]) -> str:
        return pct(sum(i[mode]["outcome"] in outcomes for i in group), len(group))

    return [
        ("Scams stopped (blocked or sent to her son)", count(scams, STOPPED)),
        ("… blocked outright", count(scams, ("BLOCK",))),
        ("Scams of RM 500 or less stopped (the hard cap can't help)", count(small_scams, STOPPED)),
        ("Scams that would go through on her tap alone", count(scams, ("APPROVE", "WARN"))),
        ("Genuine TNB / Air Selangor bills payable on her tap alone", count(official, ("APPROVE", "WARN"))),
        ("Genuine payments to other payees sent to her son", count(other, ("REVIEW",))),
        ("Genuine payments wrongly blocked", count(genuine, ("BLOCK",))),
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules-only", action="store_true", help="skip the Gemini pass")
    parser.add_argument("--delay", type=float, default=4.0, help="seconds between Gemini calls")
    args = parser.parse_args()

    items = load_dataset()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    for item in items:
        item["rules"] = run_item(item, rules_only)

    use_ai = not args.rules_only and ai_analyst._api_key() and os.environ.get("DIALECTGUARD_AI", "on") != "off"
    if use_ai:
        cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
        for number, item in enumerate(items, start=1):
            item["rules_ai"] = run_item(item, make_cached_ai(cache, item["id"], args.delay))
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"\r{number}/{len(items)} AI checks", end="", flush=True)
        print()

    modes = ["rules"] + (["rules_ai"] if use_ai else [])
    columns = [summarise(items, mode) for mode in modes]
    print("| Measure | Rules only |" + (" Rules + AI |" if use_ai else ""))
    print("|---|---|" + ("---|" if use_ai else ""))
    for row in zip(*columns):
        print(f"| {row[0][0]} | " + " | ".join(value for _label, value in row) + " |")

    counts = Counter(i["label"] for i in items)
    print(f"\nDataset: {counts['scam']} scams, {counts['genuine']} genuine")
    if use_ai:
        ai_ok = [i for i in items if i["rules_ai"]["ai_status"] == "ok"]
        print(f"AI answered {len(ai_ok)}/{len(items)} requests "
              f"(others were unavailable; re-run to retry them)")
        flagged = Counter((i["label"], bool(i["rules_ai"]["ai_suspicious"])) for i in ai_ok)
        print(f"AI flagged {flagged[('scam', True)]} scams and {flagged[('genuine', True)]} genuine requests")
    keep = ("id", "label", "category", "language", "source_type", "payee_id", "payee_name", "amount_rm", *modes)
    (RESULTS_DIR / "scam_eval_results.json").write_text(
        json.dumps([{k: i[k] for k in keep} for i in items], ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Per-item results: {RESULTS_DIR / 'scam_eval_results.json'}")


if __name__ == "__main__":
    main()
