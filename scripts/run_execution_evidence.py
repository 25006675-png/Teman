"""Execution evidence (deliverable 06): run every demo scenario against the real server.

Starts the real API (uvicorn) on a fresh, separate database, drives each scenario over
HTTP exactly as the page does (real OCR on the demo photos), then saves:

  docs/execution_evidence/sandbox_logs.json   the /logs export (append-only audit log + decision records)
  docs/execution_evidence/final_state.json    the /state snapshot (balance, payments, transactions)
  docs/execution_evidence/run_results.json    every step: request, HTTP status, expected vs actual
  docs/execution_evidence/RUN_RESULTS.md      the same, as a readable table
  docs/execution_evidence/db_tamper_checks.json  direct SQL edits the database refused

Your demo database (instance/) is not touched. Uses Gemini if GEMINI_API_KEY is set
(synthetic data only); set DIALECTGUARD_AI=off to run without it.

    python scripts/run_execution_evidence.py
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "execution_evidence"
PHOTOS = ROOT / "demo" / "photos"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
PIN = "2580"  # demo PIN, stored only as a salted hash in data/guardians.json
GUARDIAN_WINDOW_SECONDS = 120

steps: list[dict] = []


def rm(minor: int | None) -> str:
    return "-" if minor is None else f"RM {minor / 100:.2f}"


def record(scenario: str, action: str, response: httpx.Response, expected: str, passed: bool, note: str = "") -> dict:
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    tx = body.get("transaction") or {}
    steps.append({
        "scenario": scenario, "action": action, "http_status": response.status_code,
        "tx_id": body.get("tx_id") or tx.get("tx_id"),
        "status": tx.get("status"), "outcome": body.get("outcome") or tx.get("outcome_band"),
        "risk_code": body.get("risk_code_shown") or tx.get("risk_code") or body.get("error"),
        "expected": expected, "result": "PASS" if passed else "FAIL", "note": note,
        "response": body,
    })
    print(f"[{'PASS' if passed else 'FAIL'}] {scenario} | {action} -> {response.status_code} {note}")
    return body


def balance(client: httpx.Client) -> int:
    return client.get("/state").json()["balance_minor"]


def photo(client: httpx.Client, name: str) -> httpx.Response:
    with open(PHOTOS / name, "rb") as handle:
        return client.post("/analyze/photo", files={"photo": (name, handle, "image/png")}, timeout=120)


def start_server(workdir: Path) -> subprocess.Popen:
    env = {**os.environ,
           "DIALECTGUARD_DB_PATH": str(workdir / "evidence.sqlite3"),
           "DIALECTGUARD_KEY_DIR": str(workdir / "keys"),
           "DIALECTGUARD_WHISPER_PRELOAD": "off",
           "PYTHONIOENCODING": "utf-8"}
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            if httpx.get(f"{BASE}/state", timeout=2).status_code == 200:
                return server
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    server.kill()
    raise RuntimeError("server did not start")


def run(client: httpx.Client) -> int:
    client.post("/reset")
    opening = balance(client)

    # Fail-safe timer: start a review request now and leave it unanswered until the end.
    r = client.post("/analyze", json={"preset_id": "clinic_new_payee"})
    silent_tx = r.json()["tx_id"]
    r = client.post(f"/tx/{silent_tx}/ask_child")
    record("8. Son never answers", "Ask my child, then no answer", r, "AWAITING_GUARDIAN",
           r.json().get("transaction", {}).get("status") == "AWAITING_GUARDIAN", "left unanswered on purpose")
    silent_started = time.monotonic()

    # 1. Budget and balance (read only)
    r = client.get("/budget")
    body = r.json()
    steps.append({"scenario": "1. Today's budget", "action": "GET /budget", "http_status": r.status_code,
                  "expected": "daily limit shown, no money moves", "result": "PASS" if r.status_code == 200 else "FAIL",
                  "note": f"opening balance {rm(opening)}", "response": body})
    print(f"[PASS] 1. budget -> {r.status_code}")

    # 2. Normal TNB bill photo -> two confirms at the same instant -> one charge
    r = photo(client, "1-tnb-electricity-bill.png")
    body = record("2. Pay electricity bill (photo)", "Snap 1-tnb-electricity-bill.png (real OCR)", r,
                  "APPROVE, RM 42.50", r.json().get("outcome") == "APPROVE" and r.json()["transaction"]["amount_minor"] == 4250,
                  f"read from photo: {r.json().get('read_from_photo')}")
    tnb_tx = body["tx_id"]
    before = balance(client)
    gate = threading.Barrier(2)

    def tap() -> httpx.Response:
        gate.wait()
        return httpx.post(f"{BASE}/tx/{tnb_tx}/confirm", json={"method": "tap"}, timeout=30)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: tap(), range(2)))
    oks = [x for x in results if x.status_code == 200 and x.json().get("executed")]
    charged = before - balance(client)
    for i, x in enumerate(results, 1):
        record("2. Pay electricity bill (photo)", f"Double-tap test: confirm #{i} (sent at the same instant)", x,
               "exactly one of the two executes", len(oks) == 1 and charged == 4250,
               f"executed={x.json().get('executed')}; total charged {rm(charged)}")

    # 3. Pay later: Air Selangor photo -> saved, paid 2 days before due within the family limit
    r = photo(client, "5-air-selangor-bill.png")
    body = record("3. Pay later", "Snap 5-air-selangor-bill.png (real OCR)", r, "APPROVE, RM 19.20",
                  r.json().get("outcome") == "APPROVE")
    r = client.post(f"/tx/{body['tx_id']}/pay_later")
    saved = r.json().get("scheduled") or {}
    steps.append({"scenario": "3. Pay later", "action": "Pay later (到期先交)", "http_status": r.status_code,
                  "expected": "saved, pay_on = due date - 2 days", "result": "PASS" if saved.get("status") == "SCHEDULED" else "FAIL",
                  "note": f"due {saved.get('due_date')}, pays on {saved.get('pay_on')}", "response": r.json()})
    print(f"[PASS] 3. pay later -> {r.status_code} {saved.get('pay_on')}")

    # 4. Unknown payee -> son approves with PIN; extra fields in the request are ignored
    r = photo(client, "2-klinik-baru-bill.png")
    body = record("4. Family co-sign", "Snap 2-klinik-baru-bill.png (real OCR)", r, "REVIEW (PAYEE_UNKNOWN)",
                  r.json().get("outcome") == "REVIEW")
    clinic_tx = body["tx_id"]
    r = client.post(f"/tx/{clinic_tx}/ask_child")
    record("4. Family co-sign", "Ask my child", r, "AWAITING_GUARDIAN",
           r.json().get("transaction", {}).get("status") == "AWAITING_GUARDIAN")
    r = client.post(f"/tx/{clinic_tx}/approve", json={"pin": "0000"})
    record("4. Family co-sign", "Son approves with a wrong PIN", r, "403 WRONG_PIN, not paid",
           r.status_code == 403 and r.json().get("error") == "WRONG_PIN", f"attempts left: {r.json().get('attempts_left')}")
    before = balance(client)
    r = client.post(f"/tx/{clinic_tx}/approve", json={"pin": PIN, "amount_minor": 1, "payee_name": "Attacker"})
    charged = before - balance(client)
    record("4. Family co-sign", "Son approves with the right PIN, request also carries amount=1 and payee=Attacker",
           r, "DONE; stored RM 120.00 to Klinik Baru paid, extra fields ignored",
           r.json().get("executed") is True and charged == 12000 and r.json()["payment"]["payee_name"] == "Klinik Baru (demo)",
           f"paid {rm(charged)} to {r.json().get('payment', {}).get('payee_name')}")

    # 5. Scam with a hidden instruction -> blocked; nobody can pay or approve it
    r = photo(client, "3-prize-scam-message.png")
    body = record("5. Hidden-instruction scam", "Snap 3-prize-scam-message.png (real OCR)", r, "BLOCKED (SCAM_SIGNAL)",
                  r.json().get("transaction", {}).get("status") == "BLOCKED")
    scam_tx = body["tx_id"]
    before = balance(client)
    for action, call in [
        ("Ah Por tries to confirm", lambda: client.post(f"/tx/{scam_tx}/confirm", json={"method": "tap"})),
        ("Ah Por tries Ask my child", lambda: client.post(f"/tx/{scam_tx}/ask_child")),
        ("Son tries to approve with the right PIN", lambda: client.post(f"/tx/{scam_tx}/approve", json={"pin": PIN})),
        ("Voice says confirm", lambda: client.post("/voice", json={"tx_id": scam_tx, "keyword": "confirm"})),
    ]:
        r = call()
        record("5. Hidden-instruction scam", action, r, "refused, no payment",
               r.status_code >= 400 and balance(client) == before, r.json().get("error", ""))

    # 6. Scam with no keywords -> rules send it to the son -> he reports it
    r = photo(client, "4-inspector-lim-message.png")
    ai = r.json().get("ai_assessment", {})
    body = record("6. No-keyword scam", "Snap 4-inspector-lim-message.png (real OCR)", r, "REVIEW (goes to son)",
                  r.json().get("outcome") == "REVIEW",
                  f"AI analyst: status={ai.get('status')}, tactics={ai.get('tactics')}")
    lim_tx = body["tx_id"]
    client.post(f"/tx/{lim_tx}/ask_child")
    r = client.post(f"/tx/{lim_tx}/report_scam")
    record("6. No-keyword scam", "Son: It's a scam - report & reject", r, "REJECTED, payee reported",
           r.json().get("transaction", {}).get("status") == "REJECTED", f"report count {r.json().get('report_count')}")
    r = client.post("/analyze", json={"source_type": "TRANSFER", "payee_name": "Lim Holdings 5521 (demo)",
                                      "amount_minor": 30000, "source_text": "Please transfer for verification."})
    record("6. No-keyword scam", "Same account asked for money again", r, "COMMUNITY_REPORTED fires (+40)",
           "COMMUNITY_REPORTED" in json.dumps(r.json().get("decision_record", r.json())), "one family's report protects others")

    # 7. Money to the son's own account -> he can't approve it
    r = photo(client, "7-ah-ming-own-account-message.png")
    body = record("7. Son's own account", "Snap 7-ah-ming-own-account-message.png (real OCR)", r,
                  "REVIEW, pays_guardian = true", r.json().get("transaction", {}).get("pays_guardian") is True)
    own_tx = body["tx_id"]
    client.post(f"/tx/{own_tx}/ask_child")
    r = client.post(f"/tx/{own_tx}/approve", json={"pin": PIN})
    record("7. Son's own account", "Son approves with the right PIN", r, "403 SELF_APPROVAL, not paid",
           r.status_code == 403 and r.json().get("error") == "SELF_APPROVAL", r.json().get("error", ""))
    client.post(f"/tx/{own_tx}/reject")

    # 9. Three wrong PINs stop the payment
    r = client.post("/analyze", json={"preset_id": "clinic_new_payee"})
    pin_tx = r.json()["tx_id"]
    client.post(f"/tx/{pin_tx}/ask_child")
    for attempt in (1, 2, 3):
        r = client.post(f"/tx/{pin_tx}/approve", json={"pin": f"111{attempt}"})
        record("9. Wrong PINs", f"Wrong PIN #{attempt}", r, "PIN_LOCKED on #3" if attempt == 3 else "WRONG_PIN",
               r.status_code == 403 and r.json().get("error") == ("PIN_LOCKED" if attempt == 3 else "WRONG_PIN"),
               f"{r.json().get('error')}, attempts left {r.json().get('attempts_left')}")
    r = client.post(f"/tx/{pin_tx}/approve", json={"pin": PIN})
    record("9. Wrong PINs", "Right PIN after lock-out", r, "refused (already stopped)", r.status_code >= 400,
           r.json().get("error", ""))

    # 10. Voice that matches nothing changes nothing
    r = client.post("/analyze", json={"preset_id": "tnb_normal"})
    voice_tx = r.json()["tx_id"]
    r = client.post("/voice", json={"tx_id": voice_tx, "keyword": "maybe"})
    status = client.get("/state").json()
    still = next(t for t in status["transactions"] if t["tx_id"] == voice_tx)["status"]
    record("10. Unclear voice", "Voice keyword not recognised", r, "nothing executed, 'please tap a button'",
           still == "PENDING", r.json().get("message", ""))
    client.post(f"/tx/{voice_tx}/cancel")

    # 8 (end). The unanswered request expires; no money moves
    wait = GUARDIAN_WINDOW_SECONDS + 3 - (time.monotonic() - silent_started)
    if wait > 0:
        print(f"waiting {wait:.0f} s for the son's 2-minute window to run out...")
        time.sleep(wait)
    before = balance(client)
    status = client.get("/state").json()
    final = next(t for t in status["transactions"] if t["tx_id"] == silent_tx)["status"]
    r = client.post(f"/tx/{silent_tx}/approve", json={"pin": PIN})
    record("8. Son never answers", "After 2 minutes: son tries to approve late", r, "EXPIRED, no payment",
           final == "EXPIRED" and balance(client) == before, f"status after window: {final}")
    return opening


def tamper_checks(db_path: Path) -> list[dict]:
    """Try to rewrite history directly in the database file; the triggers must refuse."""
    checks = []
    attempts = [
        ("Edit an audit log entry", "UPDATE logs SET status = 'EDITED' WHERE id = (SELECT MIN(id) FROM logs)"),
        ("Delete an audit log entry", "DELETE FROM logs WHERE id = (SELECT MIN(id) FROM logs)"),
        ("Change a sandbox payment amount", "UPDATE sandbox_payments SET amount_minor = 1"),
        ("Delete a sandbox payment", "DELETE FROM sandbox_payments"),
        ("Move a paid transaction back to PENDING", "UPDATE transactions SET status = 'PENDING' WHERE status = 'DONE'"),
        ("Revive a blocked scam as PENDING", "UPDATE transactions SET status = 'PENDING' WHERE status = 'BLOCKED'"),
    ]
    connection = sqlite3.connect(db_path)
    for name, sql in attempts:
        try:
            connection.execute(sql)
            connection.rollback()
            checks.append({"attempt": name, "sql": sql, "refused": False, "error": None})
        except sqlite3.DatabaseError as error:
            checks.append({"attempt": name, "sql": sql, "refused": True, "error": str(error)})
    connection.close()
    return checks


def write_markdown(meta: dict, checks: list[dict]) -> None:
    passed = sum(s["result"] == "PASS" for s in steps)
    lines = [
        "# Execution run results",
        "",
        f"Generated by `scripts/run_execution_evidence.py` on {meta['run_at']} against the real server "
        f"(uvicorn, fresh database, signed approvals on, AI {'on' if meta['ai_key_present'] else 'off'}).",
        "",
        f"**{passed} of {len(steps)} steps behaved as expected.** "
        f"Opening balance {rm(meta['opening_balance_minor'])} → closing balance {rm(meta['closing_balance_minor'])} "
        f"({meta['payments']} sandbox payments).",
        "",
        "| # | Scenario | Action | HTTP | Expected | Result | Note |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, s in enumerate(steps, 1):
        note = str(s.get("note", "")).replace("|", "/")
        lines.append(f"| {i} | {s['scenario']} | {s['action']} | {s['http_status']} | {s['expected']} | "
                     f"{'✅' if s['result'] == 'PASS' else '❌'} {s['result']} | {note} |")
    lines += ["", "## Direct database edits (after the run)", "",
              "| Attempt | Refused by the database | Error |", "|---|---|---|"]
    for c in checks:
        lines.append(f"| {c['attempt']} | {'✅ yes' if c['refused'] else '❌ no'} | `{c['error']}` |")
    (OUT / "RUN_RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        server = start_server(workdir)
        try:
            with httpx.Client(base_url=BASE, timeout=60) as client:
                run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                opening = run(client)
                logs = client.get("/logs").json()
                state = client.get("/state").json()
        finally:
            server.terminate()
            server.wait(timeout=10)
        checks = tamper_checks(workdir / "evidence.sqlite3")

    meta = {
        "run_at": run_at, "server": BASE, "approval_mode": state["approval_mode"],
        "ai_key_present": bool(os.environ.get("GEMINI_API_KEY") or (ROOT / ".env").exists())
                          and os.environ.get("DIALECTGUARD_AI", "on") != "off",
        "opening_balance_minor": opening, "closing_balance_minor": state["balance_minor"],
        "payments": len(state["payments"]),
    }
    (OUT / "sandbox_logs.json").write_text(json.dumps(logs, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "final_state.json").write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "run_results.json").write_text(json.dumps({"meta": meta, "steps": steps}, indent=2, ensure_ascii=False),
                                          encoding="utf-8")
    (OUT / "db_tamper_checks.json").write_text(json.dumps(checks, indent=2, ensure_ascii=False), encoding="utf-8")
    write_markdown(meta, checks)
    failed = [s for s in steps if s["result"] != "PASS"] + [c for c in checks if not c["refused"]]
    print(f"\n{len(steps) - sum(s['result'] != 'PASS' for s in steps)}/{len(steps)} steps passed; "
          f"{sum(c['refused'] for c in checks)}/{len(checks)} database edits refused. Output: {OUT}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
