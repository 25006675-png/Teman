"""Load curve: response time and charges as simultaneous requests grow (real server).

Starts its own server (temporary database, AI off). For each level N it sends N
simultaneous "confirm" taps on one fresh bill, and N simultaneous new bill checks
(/analyze, the full rules path), and records each request's response time and the
number of charges. Run from the repo root (about a minute):

    python eval/run_load_curve.py      ->  eval/results/load_curve_results.json
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "eval" / "results" / "load_curve_results.json"
PORT = 8778
BASE = f"http://127.0.0.1:{PORT}"
LEVELS = [1, 5, 10, 25, 50, 100, 200]
ROUNDS = 3  # repeat each level, pool the timings


def timed_burst(n: int, path: str, body: dict) -> list[tuple[float, httpx.Response]]:
    def one(client: httpx.Client) -> tuple[float, httpx.Response]:
        t0 = time.perf_counter()
        r = client.post(path, json=body)
        return (time.perf_counter() - t0) * 1000, r

    limits = httpx.Limits(max_connections=n, max_keepalive_connections=n)
    with httpx.Client(base_url=BASE, timeout=120, limits=limits) as client, ThreadPoolExecutor(n) as pool:
        return list(pool.map(lambda _: one(client), range(n)))


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def main() -> None:
    env = {**os.environ, "DIALECTGUARD_DB_PATH": str(Path(tempfile.mkdtemp(prefix="teman_load_")) / "l.sqlite3"),
           "DIALECTGUARD_AI": "off", "DIALECTGUARD_WHISPER_PRELOAD": "off", "DIALECTGUARD_SCHEDULER": "off"}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(PORT),
                               "--log-level", "warning"], cwd=ROOT, env=env)
    try:
        for _ in range(60):
            try:
                httpx.get(f"{BASE}/state", timeout=2)
                break
            except httpx.HTTPError:
                time.sleep(0.5)
        levels = []
        for n in LEVELS:
            confirm_ms, analyze_ms, charges, errors = [], [], [], 0
            for _ in range(ROUNDS):
                httpx.post(f"{BASE}/reset", timeout=60)
                tx = httpx.post(f"{BASE}/analyze", json={"preset_id": "tnb_normal"}, timeout=60).json()["tx_id"]
                replies = timed_burst(n, f"/tx/{tx}/confirm", {"method": "tap"})
                confirm_ms += [ms for ms, _ in replies]
                errors += sum(1 for _, r in replies if r.status_code >= 500)
                state = httpx.get(f"{BASE}/state", timeout=60).json()
                charges.append(sum(1 for p in state["payments"] if p["tx_id"] == tx))
                replies = timed_burst(n, "/analyze", {"preset_id": "clinic_new_payee"})
                analyze_ms += [ms for ms, _ in replies]
                errors += sum(1 for _, r in replies if r.status_code >= 500)
            levels.append({
                "simultaneous": n, "rounds": ROUNDS,
                "confirm_p50_ms": round(statistics.median(confirm_ms), 1), "confirm_p95_ms": round(pct(confirm_ms, 0.95), 1),
                "analyze_p50_ms": round(statistics.median(analyze_ms), 1), "analyze_p95_ms": round(pct(analyze_ms, 0.95), 1),
                "charges_per_round": charges, "server_errors": errors,
            })
            print(levels[-1])
        result = {
            "machine": f"{platform.processor() or platform.machine()}, {os.cpu_count()} logical CPUs, {platform.system()} "
                       f"{platform.release()}, one uvicorn process, SQLite",
            "levels": levels,
            "passed": all(c == 1 for lv in levels for c in lv["charges_per_round"]) and not any(lv["server_errors"] for lv in levels),
        }
        RESULTS.parent.mkdir(parents=True, exist_ok=True)
        RESULTS.write_text(json.dumps(result, indent=1), encoding="utf-8")
        print("passed:", result["passed"])
    finally:
        server.terminate()
        server.wait(timeout=20)


if __name__ == "__main__":
    main()
