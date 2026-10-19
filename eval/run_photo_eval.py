"""Bill-photo evaluation: does a bad photo ever make Teman pay the wrong thing?

The 7 demo photos (demo/photos/) are degraded the way real phone photos are
(blur, tilt, dim light, low resolution, heavy compression, noise). Each version
goes through the real OCR (Tesseract) and the real decision engine.

Per photo version we record:
- each field (payee, amount, due date): read correctly / not read (she is asked
  to retake or type it) / read WRONG,
- the decision, compared with the clean photo's decision. Safe = the same or
  stricter (asked again < ... < blocked are all "stricter" than paying).
  Unsafe = more lenient than the clean photo, or an approved payment with a wrong amount.

Run from the repo root (about 1-2 minutes):
    python eval/run_photo_eval.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DIALECTGUARD_DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="teman_photo_eval_")) / "eval.sqlite3")
os.environ["DIALECTGUARD_AI"] = "off"  # the rules only: this test is about photo reading

import db  # noqa: E402
import mandates  # noqa: E402
import ocr  # noqa: E402
import services  # noqa: E402
from rules import normalize_payee  # noqa: E402

PHOTOS = ROOT / "demo" / "photos"
RESULTS = ROOT / "eval" / "results" / "photo_eval_results.json"

EXPECTED = {  # what each photo really says
    "1-tnb-electricity-bill.png": ("Tenaga Nasional Berhad", 4250, "2026-10-28"),
    "2-klinik-baru-bill.png": ("Klinik Baru (demo)", 12000, "2026-10-25"),
    "3-prize-scam-message.png": ("Acct #9910-SCAM", 80000, None),
    "4-inspector-lim-message.png": ("Lim Holdings 5521 (demo)", 30000, None),
    "5-air-selangor-bill.png": ("Air Selangor", 1920, "2026-10-30"),
    "6-tnb-high-bill.png": ("Tenaga Nasional Berhad", 7200, "2026-10-28"),
    "7-ah-ming-own-account-message.png": ("Tan Ah Ming (Maybank 5512)", 40000, None),
}
STRICTNESS = {"APPROVE": 0, "WARN": 1, "REVIEW": 2, "DATA_INSUFFICIENT": 3, "BLOCK": 4}


def _jpeg(img: Image.Image, quality: int) -> Image.Image:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return Image.open(io.BytesIO(buf.getvalue()))


def _noise(img: Image.Image, sigma: float) -> Image.Image:
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    arr += np.random.default_rng(7).normal(0, sigma, arr.shape)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def _half(img: Image.Image) -> Image.Image:
    small = img.resize((img.width // 2, img.height // 2), Image.BILINEAR)
    return small.resize(img.size, Image.BILINEAR)


VARIANTS = {
    "original": lambda im: im,
    "mild blur": lambda im: im.filter(ImageFilter.GaussianBlur(1.2)),
    "strong blur": lambda im: im.filter(ImageFilter.GaussianBlur(2.5)),
    "tilted 3°": lambda im: im.rotate(3, expand=True, fillcolor="white"),
    "tilted 8°": lambda im: im.rotate(8, expand=True, fillcolor="white"),
    "dim light": lambda im: ImageEnhance.Contrast(ImageEnhance.Brightness(im).enhance(0.55)).enhance(0.6),
    "low resolution": _half,
    "heavy JPEG": lambda im: _jpeg(im, 12),
    "camera noise": lambda im: _noise(im, 28),
}


def field_result(got, expected, same) -> str:
    if expected is None:
        return "not on photo" if got is None else ("correct" if same(got, expected) else "wrong")
    if got is None:
        return "asked again"
    return "correct" if same(got, expected) else "wrong"


def main() -> None:
    db.initialize_database()
    with db.db_session() as connection:
        mandates.seed_family_mandates(connection)
    rows, baseline = [], {}
    for name, (payee, amount, due) in EXPECTED.items():
        clean = Image.open(PHOTOS / name)
        for variant, make in VARIANTS.items():
            buf = io.BytesIO()
            make(clean).convert("RGB").save(buf, "PNG")
            t0 = time.perf_counter()
            fields = ocr.extract(buf.getvalue())
            t_ocr = time.perf_counter() - t0
            t0 = time.perf_counter()
            with db.db_session() as connection:  # the same path as POST /analyze/photo
                checked, _mismatch = services.check_photo_against_biller(connection, fields)
                decision = services.analyze(connection, checked)
            t_decide = time.perf_counter() - t0
            outcome = decision["outcome"]
            if variant == "original":
                baseline[name] = outcome
            row = {
                "photo": name, "variant": variant,
                "payee": field_result(fields["payee_name"], payee, lambda a, b: normalize_payee(a) == normalize_payee(b)),
                "amount": field_result(fields["amount_minor"], amount, lambda a, b: a == b),
                "due_date": field_result(fields["due_date"], due, lambda a, b: a == b),
                "read": {k: fields[k] for k in ("payee_name", "amount_minor", "due_date")},
                "outcome": outcome, "clean_outcome": baseline[name],
                "ocr_seconds": round(t_ocr, 3), "decision_seconds": round(t_decide, 4),
            }
            more_lenient = STRICTNESS[outcome] < STRICTNESS[baseline[name]]
            wrong_payment = outcome in ("APPROVE", "WARN") and "wrong" in (row["payee"], row["amount"])
            row["safe"] = not (more_lenient or wrong_payment)
            rows.append(row)
            print(f"{name[:28]:28} {variant:14} payee={row['payee']:11} amount={row['amount']:11} "
                  f"due={row['due_date']:12} -> {outcome:17} {'safe' if row['safe'] else 'UNSAFE'}", flush=True)

    def count(field, value, variant=None):
        return sum(1 for r in rows if r[field] == value and (variant is None or r["variant"] == variant))

    summary = {
        "photos": len(EXPECTED), "variants": list(VARIANTS), "versions": len(rows),
        "fields": {f: {v: count(f, v) for v in ("correct", "asked again", "wrong", "not on photo")}
                   for f in ("payee", "amount", "due_date")},
        "by_variant": {v: {"all_fields_correct": sum(1 for r in rows if r["variant"] == v and
                                                     all(r[f] in ("correct", "not on photo") for f in ("payee", "amount", "due_date"))),
                           "asked_again": sum(1 for r in rows if r["variant"] == v and r["outcome"] == "DATA_INSUFFICIENT"),
                           "unsafe": sum(1 for r in rows if r["variant"] == v and not r["safe"]),
                           "total": sum(1 for r in rows if r["variant"] == v)} for v in VARIANTS},
        "unsafe": [r for r in rows if not r["safe"]],
        "ocr_seconds": {"median": round(float(np.median([r["ocr_seconds"] for r in rows])), 2),
                        "p95": round(float(np.percentile([r["ocr_seconds"] for r in rows], 95)), 2)},
        "decision_ms": {"median": round(float(np.median([r["decision_seconds"] for r in rows])) * 1000, 1),
                        "p95": round(float(np.percentile([r["decision_seconds"] for r in rows], 95)) * 1000, 1)},
    }
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "unsafe"}, ensure_ascii=False, indent=1))
    print("UNSAFE:", len(summary["unsafe"]))


if __name__ == "__main__":
    main()
