"""Concurrency stress test: many simultaneous taps or approvals still charge at most once.

Starts its own server (temporary database, AI off) and fires concurrent requests:
A. 100 simultaneous "confirm" taps from Ah Por on one bill      -> exactly 1 charge
B. 50 simultaneous approvals (right PIN) from Ah Ming on one review -> exactly 1 charge
C. 30 simultaneous wrong-PIN approvals on another review          -> 0 charges, payment stopped

Run from the repo root (about 30 seconds):
    python eval/run_stress_test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "eval" / "results" / "stress_test_results.json"
PORT = 8777
BASE = f"http://127.0.0.1:{PORT}"
PIN = "2580"  # demo value from data/guardians.json


def burst(n: int, method: str, path: str, body: dict | None = None) -> list[httpx.Response]:
    with httpx.Client(base_url=BASE, timeout=60) as client, ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(lambda _: client.request(method, path, json=body), range(n)))


def state() -> dict:
    return httpx.get(f"{BASE}/state", timeout=30).json()


def payments_for(tx_id: str) -> int:
    return sum(1 for p in state()["payments"] if p["tx_id"] == tx_id)


def main() -> None:
    env = {**os.environ, "DIALECTGUARD_DB_PATH": str(Path(tempfile.mkdtemp(prefix="teman_stress_")) / "s.sqlite3"),
           "DIALECTGUARD_AI": "off", "DIALECTGUARD_WHISPER_PRELOAD": "off"}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(PORT),
                               "--log-level", "warning"], cwd=ROOT, env=env)
    try:
        for _ in range(60):
            try:
                httpx.get(f"{BASE}/state", timeout=2); break
            except httpx.HTTPError:
                time.sleep(0.5)
        httpx.post(f"{BASE}/reset", timeout=30)
        opening = state()["balance_minor"]
        results = {}

        # A: Ah Por taps "confirm" 100 times at once
        tx = httpx.post(f"{BASE}/analyze", json={"preset_id": "tnb_normal"}, timeout=30).json()["tx_id"]
        t0 = time.perf_counter()
        replies = burst(100, "POST", f"/tx/{tx}/confirm", {"method": "tap"})
        results["A_confirm_taps"] = {
            "requests": 100, "seconds": round(time.perf_counter() - t0, 2),
            "executed": sum(1 for r in replies if r.status_code == 200 and r.json().get("executed") is True),
            "charges": payments_for(tx),
            "balance_change_minor": state()["balance_minor"] - opening,
            "expected_change_minor": -4250,
        }

        # B: Ah Ming approves the same review 50 times at once (right PIN)
        before = state()["balance_minor"]
        tx = httpx.post(f"{BASE}/analyze", json={"preset_id": "clinic_new_payee"}, timeout=30).json()["tx_id"]
        httpx.post(f"{BASE}/tx/{tx}/ask_child", timeout=30)
        t0 = time.perf_counter()
        replies = burst(50, "POST", f"/tx/{tx}/approve", {"pin": PIN})
        results["B_son_approvals"] = {
            "requests": 50, "seconds": round(time.perf_counter() - t0, 2),
            "executed": sum(1 for r in replies if r.status_code == 200 and r.json().get("executed") is True),
            "charges": payments_for(tx),
            "balance_change_minor": state()["balance_minor"] - before,
            "expected_change_minor": -12000,
        }

        # C: 30 wrong-PIN approvals at once
        before = state()["balance_minor"]
        tx = httpx.post(f"{BASE}/analyze", json={"payee_name": "Lim Holdings 5521 (demo)", "amount_minor": 30000,
                                                 "source_type": "TRANSFER", "source_text": "kindly transfer"}, timeout=30).json()["tx_id"]
        httpx.post(f"{BASE}/tx/{tx}/ask_child", timeout=30)
        replies = burst(30, "POST", f"/tx/{tx}/approve", {"pin": "0000"})
        final = next(t for t in state()["transactions"] if t["tx_id"] == tx)
        results["C_wrong_pins"] = {
            "requests": 30,
            "wrong_pin_replies": sum(1 for r in replies if r.json().get("error") == "WRONG_PIN"),
            "locked_replies": sum(1 for r in replies if r.json().get("error") == "PIN_LOCKED"),
            "charges": payments_for(tx), "final_status": final["status"],
            "balance_change_minor": state()["balance_minor"] - before,
        }
        results["passed"] = (
            results["A_confirm_taps"]["charges"] == 1
            and results["A_confirm_taps"]["balance_change_minor"] == -4250
            and results["B_son_approvals"]["charges"] == 1
            and results["B_son_approvals"]["balance_change_minor"] == -12000
            and results["C_wrong_pins"]["charges"] == 0
            and results["C_wrong_pins"]["final_status"] == "REJECTED"
        )
        RESULTS.parent.mkdir(parents=True, exist_ok=True)
        RESULTS.write_text(json.dumps(results, indent=1), encoding="utf-8")
        print(json.dumps(results, indent=1))
    finally:
        server.terminate()
        server.wait(timeout=20)


if __name__ == "__main__":
    main()
