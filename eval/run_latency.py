"""How long each step takes, so we can say honestly whether it feels usable.

Times the spoken reply (edge-tts) here, and collects the other timings from the
photo, speech and stress results. Run the other three evaluations first.

Run from the repo root:
    python eval/run_latency.py
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tts  # noqa: E402

RESULTS_DIR = ROOT / "eval" / "results"
REPLIES = [  # typical answers on Ah Por's phone
    "你戶口有 850蚊。到月尾仲有 13 日，每日用 26蚊92仙 以內就夠使。",
    "要唔要我幫你交呢張單？",
    "好，5 秒後交 42蚊50仙。想停就撳「撤回」。",
    "呢筆錢我確認唔到，已經幫你問咗阿明。佢唔覆，就唔會交錢。",
    "呢筆付款因為符合安全警示，已經停止。請唔好付款，先用官方渠道查證。",
    "阿明批准咗。Klinik Baru 120蚊 交咗喇。",
]


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


async def time_tts() -> dict:
    out = tempfile.mkdtemp(prefix="teman_tts_")
    first, repeat = [], []
    for text in REPLIES:
        t0 = time.perf_counter()
        result = await tts.make_speech_async(text, output_dir=out)
        first.append(time.perf_counter() - t0)
        if result["status"] != "ok":
            raise SystemExit(f"edge-tts failed (internet?): {result['problems']}")
        t0 = time.perf_counter()
        await tts.make_speech_async(text, output_dir=out)
        repeat.append(time.perf_counter() - t0)
    return {"first_time": {"median": round(statistics.median(first), 2), "p95": round(_p95(first), 2)},
            "repeat_cached": {"median": round(statistics.median(repeat), 3), "p95": round(_p95(repeat), 3)}}


def main() -> None:
    photo = json.loads((RESULTS_DIR / "photo_eval_results.json").read_text(encoding="utf-8"))["summary"]
    speech = json.loads((RESULTS_DIR / "speech_eval_results.json").read_text(encoding="utf-8"))["summary"]
    stress = json.loads((RESULTS_DIR / "stress_test_results.json").read_text(encoding="utf-8"))
    voice = asyncio.run(time_tts())
    steps = [
        {"step": "Rules decision (engine)", "median_s": photo["decision_ms"]["median"] / 1000, "p95_s": photo["decision_ms"]["p95"] / 1000},
        {"step": "Read a bill photo (OCR)", "median_s": photo["ocr_seconds"]["median"], "p95_s": photo["ocr_seconds"]["p95"]},
        {"step": "Speak a reply, repeated (cached)", "median_s": voice["repeat_cached"]["median"], "p95_s": voice["repeat_cached"]["p95"]},
        {"step": "Speak a reply, first time (edge-tts)", "median_s": voice["first_time"]["median"], "p95_s": voice["first_time"]["p95"]},
        {"step": "Understand Cantonese speech (Whisper, CPU)", "median_s": speech["seconds"]["median"], "p95_s": speech["seconds"]["p95"]},
    ]
    result = {"steps": steps, "stress": {"100 simultaneous taps (s)": stress["A_confirm_taps"]["seconds"],
                                          "50 simultaneous approvals (s)": stress["B_son_approvals"]["seconds"]},
              "machine": "the team laptop (CPU only, no graphics card)"}
    (RESULTS_DIR / "latency_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
