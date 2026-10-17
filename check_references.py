"""check_references.py - checks the evaluation reference file BEFORE scoring.
It checks the file format only. It does not score any model."""

import csv
import os
import sys

REQUIRED = ["clip_id", "file", "speaker_id", "reference_text",
            "expected_amount", "expected_payee"]


def check_references(csv_path, audio_dir):
    """Return {"rows": n, "ok_rows": n, "problems": [...]}."""
    try:
        # utf-8-sig also accepts the extra mark that Excel adds
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            columns = reader.fieldnames or []
    except FileNotFoundError:
        return {"rows": 0, "ok_rows": 0,
                "problems": [f"reference file not found: {csv_path}"]}

    missing = [c for c in REQUIRED if c not in columns]
    if missing:
        return {"rows": len(rows), "ok_rows": 0,
                "problems": ["missing columns: " + ", ".join(missing)]}

    problems, ok_rows = [], 0
    seen_ids, seen_files = set(), set()
    for line, row in enumerate(rows, start=2):   # line 1 is the header
        before = len(problems)
        clip_id = (row.get("clip_id") or "").strip()
        file = (row.get("file") or "").strip()
        reference = (row.get("reference_text") or "").strip()

        if not clip_id:
            problems.append(f"line {line}: clip_id is empty")
        elif clip_id in seen_ids:
            problems.append(f"line {line}: duplicate clip_id '{clip_id}'")
        seen_ids.add(clip_id)

        if not file:
            problems.append(f"line {line}: file is empty")
        else:
            if file in seen_files:
                problems.append(f"line {line}: duplicate file '{file}'")
            seen_files.add(file)
            if not os.path.exists(os.path.join(audio_dir, file)):
                problems.append(
                    f"line {line}: audio file not found in {audio_dir}: {file}")

        if not reference:
            problems.append(f"line {line}: reference_text is empty")

        if len(problems) == before:
            ok_rows += 1
    return {"rows": len(rows), "ok_rows": ok_rows, "problems": problems}


if __name__ == "__main__":
    csv_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join("eval", "references.csv")
    audio_dir = sys.argv[2] if len(sys.argv) > 2 else "audio_samples"
    result = check_references(csv_path, audio_dir)
    print(f"Rows: {result['rows']}  |  Rows with no problem: {result['ok_rows']}")
    for p in result["problems"]:
        print(" -", p)
    if not result["problems"]:
        print("File format looks fine (this does not check that the references are correct).")