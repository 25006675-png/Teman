"""Cantonese speech evaluation (SYNTHETIC voices): can Whisper understand Ah Por?

Each test sentence is spoken by Microsoft's three Hong Kong Cantonese voices
(edge-tts), once clean and once with background noise (about 10 dB SNR,
like a busy room), then transcribed by the same Whisper model the app uses.

Measured per clip:
- character error rate (CER) against the sentence,
- voice commands: does 「確認」/「取消」 map to the right command (speech.match_command)?
- her phone's instant requests: is "how much do I have" / "pay electricity" recognised,
  and are look-alike questions left alone? (mirrors handleSpoken in static/index.html)
- amounts: is the spoken amount read back correctly?

LIMIT: computer voices are clearer than a 72-year-old speaker. This is a lower bound
on difficulty, not a result with real elders. Real recordings go in eval/references.csv.

Run from the repo root (takes ~15 minutes on CPU; audio is cached in eval/speech_audio/):
    python eval/run_speech_eval.py             # full run
    python eval/run_speech_eval.py --rescore   # re-score saved transcripts after a matching-rule change
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import metrics  # noqa: E402
import speech  # noqa: E402

AUDIO_DIR = ROOT / "eval" / "speech_audio"
RESULTS = ROOT / "eval" / "results" / "speech_eval_results.json"
VOICES = ["zh-HK-HiuGaaiNeural", "zh-HK-HiuMaanNeural", "zh-HK-WanLungNeural"]
RATE = "-10%"          # a little slower, like the app's own voice
NOISE_SNR_DB = 10.0
SAMPLE_RATE = 16000

# kind: command (expected match_command) · intent (expected balance / pay_electricity / other)
#       · amount (expected ringgit amount in sen)
SENTENCES = [
    ("c1", "確認", "command", "confirm"),
    ("c2", "取消", "command", "cancel"),
    ("c3", "確定", "command", "confirm"),
    ("c4", "唔好", "command", "cancel"),
    ("c5", "俾錢", "command", "confirm"),
    ("i1", "我仲有幾多錢？", "intent", "balance"),
    ("i2", "我戶口仲剩幾多錢？", "intent", "balance"),
    ("i3", "幫我交電費", "intent", "pay_electricity"),
    ("i4", "幫我交埋電費啦", "intent", "pay_electricity"),
    ("i5", "電費交咗未呀？", "intent", "other"),          # a question, not a payment request
    ("i6", "今個星期用咗幾多錢？", "intent", "weekly_report"),  # spending, not balance
    ("i7", "我啲單交咗未？", "intent", "other"),
    ("a1", "診所張單一百二十蚊", "amount", 12000),
    ("a2", "電費四十二蚊五毫", "amount", 4250),
    ("a3", "佢叫我轉三百蚊", "amount", 30000),
    ("a4", "水費十九蚊二毫", "amount", 1920),
    ("a5", "話我中咗獎，要俾八百蚊", "amount", 80000),
    ("a6", "我想俾阿明四百蚊", "amount", 40000),
]


# ---- mirrors handleSpoken() in static/index.html --------------------------------
def spoken_intent(text: str) -> str:
    t = re.sub(r"\s+", "", text)
    if re.search(r"(仲有|仲剩|仲淨|剩返|淨返|淨低|剩|戶口有|有)幾多錢|餘額", t) and not re.search(r"用咗|使咗|洗咗", t):
        return "balance"
    if re.search(r"(今個|呢個|上個)?(星期|禮拜|週).*(用咗|使咗|洗咗)|(用咗|使咗|洗咗)幾多", t):
        return "weekly_report"
    if re.search(r"電費", t) and re.search(r"交|俾|畀|付|找", t) and not re.search(r"咗未|未呀|未啊|未交|有冇交", t):
        return "pay_electricity"
    return "other"


# ---- amounts: digits or Cantonese number words -> sen -------------------------
DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9}
UNITS = {"十": 10, "百": 100, "千": 1000}


def _words_to_int(words: str) -> int | None:
    total, current = 0, 0
    for ch in words:
        if ch in DIGITS:
            current = DIGITS[ch]
        elif ch in UNITS:
            total += (current or 1) * UNITS[ch]
            current = 0
        else:
            return None
    return total + current


def spoken_amount(text: str) -> int | None:
    """'四十二蚊五毫' -> 4250, '120蚊' -> 12000, 'RM 42.50' -> 4250. None if no amount."""
    t = text.replace(",", "")
    m = re.search(r"(\d+)(?:\.(\d{1,2}))?\s*(?:蚊|元|塊|令吉|ringgit)?", t)
    if m and (m.group(2) or re.search(r"\d+\s*(?:蚊|元|塊|令吉)", t)):
        sen = int(m.group(1)) * 100 + (int(m.group(2).ljust(2, "0")) if m.group(2) else 0)
        cents = re.search(r"\d+\s*蚊\s*([0-9一二兩两三四五六七八九])\s*毫", t)
        return sen + ((int(cents.group(1)) if cents.group(1).isdigit() else DIGITS[cents.group(1)]) * 10 if cents else 0)
    m = re.search(r"([零〇一二兩两三四五六七八九十百千]+)\s*(?:蚊|元|塊)(?:\s*([一二兩两三四五六七八九])\s*毫)?", t)
    if m:
        whole = _words_to_int(m.group(1))
        if whole is None:
            return None
        return whole * 100 + (DIGITS[m.group(2)] * 10 if m.group(2) else 0)
    return None


# ---- audio ---------------------------------------------------------------------
async def _speak(text: str, voice: str, path: Path) -> None:
    import edge_tts
    await edge_tts.Communicate(text, voice, rate=RATE).save(str(path))


def _decode(mp3: Path) -> np.ndarray:
    import av
    out = []
    with av.open(str(mp3)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(audio=0):
            for o in resampler.resample(frame):
                out.append(o.to_ndarray().reshape(-1))
    return np.concatenate(out).astype(np.float32) / 32768.0


def _write_wav(samples: np.ndarray, path: Path) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE); w.writeframes(pcm.tobytes())


def _with_noise(clean: np.ndarray, seed: int) -> np.ndarray:
    """Add pink-ish background noise at NOISE_SNR_DB (a busy room, not silence)."""
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(len(clean) + 64)
    pink = np.convolve(white, np.ones(64) / 64, mode="valid")[: len(clean)]
    signal_power = np.mean(clean ** 2)
    noise_power = np.mean(pink ** 2)
    scale = np.sqrt(signal_power / (noise_power * 10 ** (NOISE_SNR_DB / 10)))
    return clean + pink * scale


def make_clips() -> list[dict]:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    clips = []
    for vi, voice in enumerate(VOICES):
        for si, (sid, text, kind, expected) in enumerate(SENTENCES):
            mp3 = AUDIO_DIR / f"{sid}_{voice}.mp3"
            if not mp3.exists():
                asyncio.run(_speak(text, voice, mp3))
            clean = _decode(mp3)
            for condition in ("clean", "noisy"):
                wav = AUDIO_DIR / f"{sid}_{voice}_{condition}.wav"
                if not wav.exists():
                    _write_wav(clean if condition == "clean" else _with_noise(clean, vi * 100 + si), wav)
                clips.append({"id": sid, "text": text, "kind": kind, "expected": expected,
                              "voice": voice, "condition": condition, "wav": wav})
    return clips


def main() -> None:
    clips = make_clips()
    print(f"{len(clips)} clips; loading Whisper large-v3 ...", flush=True)
    speech.get_model("large-v3")
    rows = []
    for n, c in enumerate(clips, 1):
        t0 = time.perf_counter()
        heard = speech.transcribe_audio(str(c["wav"]), model_size="large-v3")
        seconds = round(time.perf_counter() - t0, 2)
        text = heard["text"]
        row = {k: v for k, v in c.items() if k != "wav"}
        row.update(heard=text, whisper_status=heard["status"], seconds=seconds,
                   cer=round(metrics.cer(c["text"], text), 3) if text else 1.0)
        rows.append(score(row))
        print(f"[{n}/{len(clips)}] {c['voice'][6:-6]:8} {c['condition']:5} {c['text']} -> {text} "
              f"| {row['got']} {'OK' if row['correct'] else 'WRONG'} ({seconds}s)", flush=True)

    write_results(rows)


def score(row: dict) -> dict:
    """Score one transcript against its sentence (no audio needed)."""
    text = row["heard"]
    if row["kind"] == "command":
        row["got"] = speech.match_command(text)
    elif row["kind"] == "intent":
        row["got"] = spoken_intent(text)
    else:
        row["got"] = spoken_amount(text)
    row["correct"] = row["got"] == row["expected"]
    return row


def write_results(rows: list[dict]) -> None:
    def share(sel):
        sel = list(sel)
        return {"correct": sum(r["correct"] for r in sel), "total": len(sel)}

    summary = {
        "clips": len(rows),
        "voices": VOICES,
        "noise_snr_db": NOISE_SNR_DB,
        "model": "whisper large-v3 (faster-whisper, CPU, int8, language yue)",
        "mean_cer": {cond: round(float(np.mean([r["cer"] for r in rows if r["condition"] == cond])), 3)
                     for cond in ("clean", "noisy")},
        "by_kind": {kind: {cond: share(r for r in rows if r["kind"] == kind and r["condition"] == cond)
                           for cond in ("clean", "noisy")} for kind in ("command", "intent", "amount")},
        "seconds": {"median": round(float(np.median([r["seconds"] for r in rows])), 2),
                    "p95": round(float(np.percentile([r["seconds"] for r in rows], 95)), 2)},
        "wrong_actions": [r for r in rows if not r["correct"] and r["kind"] in ("command", "intent")
                          and r["got"] not in ("unclear", "other")],
    }
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def rescore() -> None:
    """Re-score saved transcripts after a change to the matching rules (no Whisper run)."""
    saved = json.loads(RESULTS.read_text(encoding="utf-8"))["rows"]
    expected = {sid: exp for sid, _text, _kind, exp in SENTENCES}
    write_results([score({**row, "expected": expected[row["id"]]}) for row in saved])


if __name__ == "__main__":
    rescore() if "--rescore" in sys.argv else main()
