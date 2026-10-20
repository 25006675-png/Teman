"""Randomised attack test (Monte Carlo): thousands of random actions, safety rules checked after each one.

Drives the real app (FastAPI endpoints, rules, signed approvals, SQLite triggers) in-process
on a temporary database, with the AI off. Each sequence starts from a reset and fires random
actions: new requests with random payees, amounts and text (including scam phrases with hidden
characters), taps, voice words, the son's approvals with right and wrong PINs, simultaneous
bursts, cancels, "Call Mum", scam reports, pay later, requests carrying a different amount,
and jumps forward in time so timers run out. After every action, 11 safety rules are checked
against the database. Fixed seed, so the run is reproducible.

    python eval/run_invariant_fuzz.py [--sequences 200] [--steps 50] [--seed 2026]
        -> eval/results/invariant_fuzz_results.json

Self-check that the test can catch a broken safeguard (results are printed, not saved):
    python eval/run_invariant_fuzz.py --sequences 20 --break pin
    python eval/run_invariant_fuzz.py --sequences 20 --break self_approval
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "eval" / "results" / "invariant_fuzz_results.json"
WORKDIR = Path(tempfile.mkdtemp(prefix="teman_fuzz_"))
DB_PATH = WORKDIR / "fuzz.sqlite3"
os.environ.update({
    "DIALECTGUARD_DB_PATH": str(DB_PATH), "DIALECTGUARD_KEY_DIR": str(WORKDIR / "keys"),
    "DIALECTGUARD_AI": "off", "DIALECTGUARD_SCHEDULER": "off", "DIALECTGUARD_WHISPER_PRELOAD": "off",
})
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import transactions  # noqa: E402

PIN = "2580"
GUARDIAN_ACCOUNT = "Tan Ah Ming (Maybank 5512)"
TERMINAL = {"DONE", "CANCELLED", "BLOCKED", "REJECTED", "EXPIRED"}
ZW = "​"

RULES = {
    "charged_once": "Never charged twice for one request",
    "blocked_never_paid": "Blocked requests are never paid",
    "frozen_fields": "The amount and payee paid are the ones stored at creation",
    "balance_adds_up": "Balance always equals opening balance minus payments",
    "review_needs_son_pin": "A review payment needs the son's right PIN",
    "her_payments_safe_only": "Her own payments: safe or acknowledged-warning bills only",
    "never_to_son": "Never paid to the son's own account",
    "nothing_after_deadline": "Nothing is paid after its deadline",
    "final_stays_final": "A finished request never changes again",
    "payment_logged": "Every payment is in the audit log",
    "log_only_grows": "The audit log only grows",
}

# ---- controllable clock: lets timers run out without waiting -------------------------------
_offset = timedelta(0)
_real_as_utc = transactions._as_utc


def _shifted_as_utc(value):
    return _real_as_utc(value) if value is not None else _real_as_utc(datetime.now(timezone.utc) + _offset)


transactions._as_utc = _shifted_as_utc


def clock_now() -> datetime:
    return datetime.now(timezone.utc) + _offset


def parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


# ---- random inputs ---------------------------------------------------------------------------
def random_amount(rng: random.Random) -> int:
    pick = rng.random()
    if pick < 0.25:  # edge values around every threshold
        return rng.choice([0, -100, 1, 4250, 4608, 4609, 5000, 5001, 5760, 5761, 49999, 50000, 50001, 10**9])
    return int(round(10 ** rng.uniform(2, 4.85)))  # RM 1 to ~RM 700, log-uniform


def scam_text(rng: random.Random) -> str:
    phrase = rng.choice(["safe account", "ignore the rules", "unfreeze", "安全账户", "prize fee"])
    if rng.random() < 0.5:  # hide it: zero-width spaces, extra spacing, case changes
        phrase = ZW.join(phrase) if rng.random() < 0.5 else " ".join(phrase).upper()
    return f"Dear customer, {phrase}. Transfer now."


def new_request(rng: random.Random) -> tuple[str, dict]:
    kind = rng.choice(["tnb", "tnb", "air_selangor", "new_payee", "blacklisted", "scam_text",
                       "son_account", "preset"])
    if kind == "preset":
        return kind, {"preset_id": rng.choice(["tnb_normal", "tnb_unusually_high", "tnb_ocr_typo", "air_selangor_normal",
                                               "clinic_new_payee", "scam_prize_transfer", "police_impersonation_transfer"])}
    body = {"amount_minor": random_amount(rng), "source_type": rng.choice(["BILL", "TRANSFER"]),
            "source_text": "Monthly bill." if rng.random() < 0.7 else "Please pay quickly, kindly keep this private."}
    if kind == "tnb":
        body.update(payee_id="tnb", payee_name="Tenaga Nasional Berhad")
    elif kind == "air_selangor":
        body.update(payee_id="air_selangor", payee_name="Air Selangor")
    elif kind == "new_payee":
        body.update(payee_name=rng.choice(["Klinik Baru (demo)", "Lim Holdings 5521 (demo)", "Tan Mei Ling"]))
    elif kind == "blacklisted":
        body.update(payee_name="Acct #9910-SCAM")
    elif kind == "scam_text":
        body.update(payee_name="Kedai Hadiah", source_text=scam_text(rng))
    elif kind == "son_account":
        body.update(payee_name=GUARDIAN_ACCOUNT)
    if rng.random() < 0.05:
        body["payee_name"] = None  # missing payee
    return kind, body


# ---- database snapshot (read-only connection) ---------------------------------------------------
def snapshot() -> dict:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    txs = {r["id"]: dict(r) for r in con.execute("SELECT * FROM transactions")}
    pays = [dict(r) for r in con.execute("SELECT * FROM sandbox_payments")]
    logs = con.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    paid_logs = con.execute("SELECT COUNT(*) FROM logs WHERE event = 'payment_executed'").fetchone()[0]
    con.close()
    return {"txs": txs, "pays": pays, "logs": logs, "paid_logs": paid_logs}


class Run:
    def __init__(self, client: TestClient, rng: random.Random) -> None:
        self.client, self.rng = client, rng
        self.created: dict[str, dict] = {}  # tx_id -> fields stored at creation, as reported by /analyze
        self.actions = Counter()
        self.outcomes: dict[str, Counter] = defaultdict(Counter)
        self.checks = Counter()
        self.violations: list[dict] = []
        self.server_errors: list[dict] = []
        self.opening = 0
        self.snap: dict = {}

    # -- helpers
    def pick_tx(self, prefer: str | None = None) -> str:
        ids = list(self.created)
        if not ids or self.rng.random() < 0.04:
            return "0" * 32  # a transaction that doesn't exist
        if prefer:
            wanted = [t for t in ids if self.snap["txs"].get(t, {}).get("status") == prefer]
            if wanted and self.rng.random() < 0.8:
                return self.rng.choice(wanted)
        return self.rng.choice(ids)

    def burst(self, n: int, path: str, body: dict) -> list:
        with ThreadPoolExecutor(n) as pool:
            return list(pool.map(lambda _: self.client.post(path, json=body), range(n)))

    # -- one random action
    def act(self) -> None:
        global _offset
        r = self.rng.random()
        before = self.snap
        clock = clock_now()
        intent = {}  # what the action was allowed to do, for the checks
        if r < 0.22:
            name = "New payment request"
            kind, body = new_request(self.rng)
            resp = self.client.post("/analyze", json=body)
            if resp.status_code == 200 and resp.json().get("tx_id"):
                tx = resp.json()["transaction"]
                self.created[tx["tx_id"]] = {"amount_minor": tx["amount_minor"], "payee_name": tx["payee_name"]}
            responses = [resp]
        elif r < 0.36:
            name = "Ah Por confirms (tap or voice)"
            tx = self.pick_tx("PENDING")
            ack = self.rng.random() < 0.5
            body = {"method": self.rng.choice(["tap", "voice"]), "acknowledge_warning": ack}
            if self.rng.random() < 0.3:  # try to change what gets paid
                body.update(amount_minor=1, payee_name="Attacker", payee_id="tnb")
            intent = {"tx": tx, "role": "AH_POR", "ack": ack}
            responses = [self.client.post(f"/tx/{tx}/confirm", json=body)]
        elif r < 0.43:
            name = "Simultaneous confirms (2-8)"
            tx = self.pick_tx("PENDING")
            intent = {"tx": tx, "role": "AH_POR", "ack": True}
            responses = self.burst(self.rng.randint(2, 8), f"/tx/{tx}/confirm", {"method": "tap", "acknowledge_warning": True})
        elif r < 0.51:
            name = "Ask my child"
            responses = [self.client.post(f"/tx/{self.pick_tx('PENDING')}/ask_child")]
        elif r < 0.60:
            name = "Son approves, right PIN"
            tx = self.pick_tx("AWAITING_GUARDIAN")
            body = {"pin": PIN}
            if self.rng.random() < 0.3:
                body.update(amount_minor=1, payee_name="Attacker")
            intent = {"tx": tx, "role": "GUARDIAN", "pin_ok": True}
            responses = [self.client.post(f"/tx/{tx}/approve", json=body)]
        elif r < 0.65:
            name = "Simultaneous son approvals (2-8)"
            tx = self.pick_tx("AWAITING_GUARDIAN")
            intent = {"tx": tx, "role": "GUARDIAN", "pin_ok": True}
            responses = self.burst(self.rng.randint(2, 8), f"/tx/{tx}/approve", {"pin": PIN})
        elif r < 0.71:
            name = "Son approves, wrong PIN"
            tx = self.pick_tx("AWAITING_GUARDIAN")
            intent = {"tx": tx, "role": "GUARDIAN", "pin_ok": False}
            responses = [self.client.post(f"/tx/{tx}/approve", json={"pin": self.rng.choice(["0000", "2581", "", "25800"])})]
        elif r < 0.75:
            name = "Voice word"
            tx = self.pick_tx("PENDING")
            word = self.rng.choice(["confirm", "cancel", None, "pay", "yes please"])
            intent = {"tx": tx, "role": "AH_POR", "ack": False}
            responses = [self.client.post("/voice", json={"tx_id": tx, "keyword": word})]
        elif r < 0.80:
            name = "Cancel or reject"
            tx = self.pick_tx()
            responses = [self.client.post(f"/tx/{tx}/{self.rng.choice(['cancel', 'reject'])}")]
        elif r < 0.84:
            name = "Report as scam"
            responses = [self.client.post(f"/tx/{self.pick_tx()}/report_scam")]
        elif r < 0.87:
            name = "Call Mum (+5 min)"
            responses = [self.client.post(f"/tx/{self.pick_tx('AWAITING_GUARDIAN')}/call_extension")]
        elif r < 0.90:
            name = "Pay later"
            responses = [self.client.post(f"/tx/{self.pick_tx('PENDING')}/pay_later")]
        else:
            name = "Time passes (1 s - 12 min)"
            _offset += timedelta(seconds=self.rng.choice([1, 30, 61, 119, 121, 299, 301, 421, 599, 601, 720]))
            responses = [self.client.get("/state")]  # reading also expires stale requests

        self.actions[name] += 1
        after = snapshot()
        new_pays = [p for p in after["pays"] if p["tx_id"] not in {q["tx_id"] for q in before["pays"]}]
        if any(x.status_code >= 500 for x in responses):
            self.outcomes[name]["Server error"] += 1
            self.server_errors.append({"action": name, "status": [x.status_code for x in responses]})
        if new_pays:
            self.outcomes[name]["Money moved"] += 1
        elif any(x.status_code >= 400 or (x.status_code == 200 and x.json().get("ok") is False) for x in responses):
            self.outcomes[name]["Refused"] += 1
        else:
            self.outcomes[name]["Allowed, no money moved"] += 1
        self.check(before, after, new_pays, intent, clock, name)
        self.snap = after

    # -- the 11 safety rules
    def fail(self, rule: str, detail: str) -> None:
        self.violations.append({"rule": rule, "detail": detail})

    def check(self, before: dict, after: dict, new_pays: list, intent: dict, clock: datetime, action: str) -> None:
        txs = after["txs"]
        per_tx = Counter(p["tx_id"] for p in after["pays"])
        self.checks.update(RULES.keys())
        if any(n > 1 for n in per_tx.values()):
            self.fail("charged_once", f"after {action}: {per_tx.most_common(1)}")
        for p in after["pays"]:
            t = txs[p["tx_id"]]
            decision = json.loads(t["decision_json"] or "{}")
            if t["risk_code"] in ("SCAM_SIGNAL", "OVER_HARD_CAP") or decision.get("outcome") == "BLOCK":
                self.fail("blocked_never_paid", p["tx_id"])
            made = self.created.get(p["tx_id"])
            if made and (p["amount_minor"] != made["amount_minor"] or p["payee_name"] != made["payee_name"]):
                self.fail("frozen_fields", f"{p['tx_id']}: paid {p['amount_minor']} to {p['payee_name']}, created {made}")
            if p["payee_name"] == GUARDIAN_ACCOUNT:
                self.fail("never_to_son", p["tx_id"])
        balance = self.client.get("/state").json()["balance_minor"]
        if balance != self.opening - sum(p["amount_minor"] for p in after["pays"]):
            self.fail("balance_adds_up", f"balance {balance}")
        for p in new_pays:
            t = txs[p["tx_id"]]
            prior = before["txs"].get(p["tx_id"], {})
            if p["tx_id"] != intent.get("tx"):
                self.fail("review_needs_son_pin" if t["outcome_band"] == "REVIEW" else "her_payments_safe_only",
                          f"{action} paid a different request {p['tx_id']}")
                continue
            if t["outcome_band"] == "REVIEW" and not (intent.get("role") == "GUARDIAN" and intent.get("pin_ok")):
                self.fail("review_needs_son_pin", f"{action} paid REVIEW {p['tx_id']}")
            if intent.get("role") == "AH_POR" and (t["outcome_band"] not in ("APPROVE", "WARN")
                                                   or (t["outcome_band"] == "WARN" and not intent.get("ack"))):
                self.fail("her_payments_safe_only", f"{action} paid {t['outcome_band']} {p['tx_id']}")
            deadline = parse(prior.get("approval_expires_at"))
            if deadline and clock > deadline + timedelta(seconds=1):
                self.fail("nothing_after_deadline", f"{p['tx_id']} paid at {clock} after {deadline}")
        for tx_id, old in before["txs"].items():
            if old["status"] in TERMINAL and txs[tx_id]["status"] != old["status"]:
                self.fail("final_stays_final", f"{tx_id}: {old['status']} -> {txs[tx_id]['status']}")
        if after["paid_logs"] != len(after["pays"]):
            self.fail("payment_logged", f"{after['paid_logs']} logs for {len(after['pays'])} payments")
        if after["logs"] < before["logs"]:
            self.fail("log_only_grows", f"{before['logs']} -> {after['logs']}")

    def sequence(self, steps: int) -> None:
        global _offset
        _offset = timedelta(0)
        self.client.post("/reset")
        self.created.clear()
        self.snap = snapshot()
        self.opening = self.client.get("/state").json()["balance_minor"]
        for _ in range(steps):
            self.act()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sequences", type=int, default=200)
    parser.add_argument("--steps", type=int, default=50)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--break", dest="breaks", choices=["pin", "self_approval"], action="append", default=[],
                        help="self-check: deliberately disable a safeguard; the run must then report violations")
    args = parser.parse_args()
    if "pin" in args.breaks:
        app_module._guardian_pin_ok = lambda pin: True
    if "self_approval" in args.breaks:
        transactions.pays_guardian = lambda row: False
    rng = random.Random(args.seed)
    started = time.perf_counter()
    with TestClient(app_module.app, raise_server_exceptions=False) as client:
        run = Run(client, rng)
        for i in range(args.sequences):
            run.sequence(args.steps)
            if (i + 1) % 20 == 0:
                print(f"{i + 1}/{args.sequences} sequences, {sum(run.actions.values())} actions, "
                      f"{len(run.violations)} violations")
    total_actions = sum(run.actions.values())
    results = {
        "seed": args.seed, "sequences": args.sequences, "steps_per_sequence": args.steps,
        "actions": total_actions, "seconds": round(time.perf_counter() - started, 1),
        "rule_checks": sum(run.checks.values()), "rules": RULES,
        "per_action": {name: {"count": run.actions[name], **run.outcomes[name]} for name in run.actions},
        "payments": sum(c["Money moved"] for c in run.outcomes.values()),
        "violations": run.violations[:50], "violation_count": len(run.violations),
        "violations_by_rule": dict(Counter(v["rule"] for v in run.violations)),
        "server_errors": len(run.server_errors), "server_error_examples": run.server_errors[:10],
        "passed": not run.violations,
    }
    if args.breaks:  # a self-check run: report, but never overwrite the real results
        print(f"SELF-CHECK with {args.breaks} disabled: {len(run.violations)} violations",
              dict(Counter(v["rule"] for v in run.violations)))
        return
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in results.items() if k not in ("violations", "rules")}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
