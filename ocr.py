"""ocr.py - reads a bill image and pulls out payee, amount and due date.

IMPORTANT
- Everything read from a bill is UNTRUSTED DATA. This module never obeys
  text found on a bill, and it cannot tell whether a bill is genuine.
- It never pays and never calls the bank. It only returns a dictionary.
- A field that is missing or unclear is returned as None, with a reason.
"""

import os
import re
from datetime import date
from decimal import Decimal, InvalidOperation

DEFAULT_WINDOWS_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# STARTER LIST - the names must match Tech 2's whitelist. Ask them.
KNOWN_PAYEES = {
    "TNB": ("tenaga nasional", "tnb"),
}

# Lines containing these words are the ones we trust for the amount / date
AMOUNT_KEYWORDS = ("amount due", "total amount", "amount payable",
                   "total due", "jumlah")
DUE_KEYWORDS = ("due date", "pay by", "tarikh akhir", "bayar sebelum")

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

# "RM 42.50", "RM42.50", "RM 1,234.50". Letters O, I, l are allowed after
# the first digit because OCR often confuses them with 0 and 1.
AMOUNT_PATTERN = re.compile(
    r"\bRM\s*([0-9][0-9OoIl,]*(?:\.[0-9OoIl]{1,2})?)", re.IGNORECASE)
DATE_NUMERIC = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b")
DATE_WORDS = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
PAYEE_LABEL = re.compile(
    r"\b(?:pay\s*to|payee|biller)\b\s*[:\-]\s*(.+)", re.IGNORECASE)
OCR_DIGIT_FIXES = str.maketrans("OoIl", "0011")


def _build_result(payee, amount, due_date, problems, raw_text, status=None):
    """Every function returns the same dictionary shape."""
    return {
        "payee": payee,
        "amount": amount,        # a string like "42.50", or None
        "due_date": due_date,    # "YYYY-MM-DD", or None
        "status": status or ("ok" if not problems else "needs_check"),
        "problems": problems,    # plain-English reasons
        "raw_text": raw_text,    # untrusted text, kept for Tech 2's scam scan
        "source": "ocr",
        "trusted": False,        # OCR output is never trusted by default
    }


def _known_payee(text):
    """Return the standard name if the text mentions a known payee."""
    lowered = text.lower()
    for name, keywords in KNOWN_PAYEES.items():
        for keyword in keywords:
            if re.search(r"\b" + re.escape(keyword) + r"\b", lowered):
                return name
    return None


def find_payee(text):
    candidates = set()
    known = _known_payee(text)
    if known:
        candidates.add(known)
    for line in text.splitlines():
        match = PAYEE_LABEL.search(line)
        if match:
            value = match.group(1).strip()[:60]
            if value:
                candidates.add(_known_payee(value) or value)
    if len(candidates) == 1:
        return candidates.pop(), []
    if not candidates:
        return None, ["payee not found"]
    return None, ["more than one payee name found: "
                  + ", ".join(sorted(candidates))]


def _clean_amount(token):
    """'42.5O' -> ('42.50', True). Returns (None, False) if not a number."""
    fixed = token.translate(OCR_DIGIT_FIXES).replace(",", "")
    try:
        value = Decimal(fixed)
    except InvalidOperation:
        return None, False
    return f"{value:.2f}", fixed != token.replace(",", "")


def find_amount(text):
    keyword_values, all_values, fixed_values = set(), set(), set()
    for line in text.splitlines():
        has_keyword = any(k in line.lower() for k in AMOUNT_KEYWORDS)
        for token in AMOUNT_PATTERN.findall(line):
            cleaned, was_fixed = _clean_amount(token)
            if cleaned is None:
                continue
            all_values.add(cleaned)
            if has_keyword:
                keyword_values.add(cleaned)
            if was_fixed:
                fixed_values.add(cleaned)
    # Prefer amounts on a line like "Amount Due"; otherwise use any amount
    candidates = keyword_values or all_values
    if not candidates:
        return None, ["amount not found"]
    if len(candidates) > 1:
        return None, ["more than one amount found: "
                      + ", ".join(sorted(candidates))]
    amount = next(iter(candidates))
    problems = []
    if amount in fixed_values:
        problems.append("amount had letters that look like digits "
                        "(O, I, l); please check it")
    return amount, problems


def _make_iso(day, month, year):
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None   # e.g. 32/13/2026 is not a real date


def find_due_date(text):
    valid, invalid = set(), []
    for line in text.splitlines():
        if not any(k in line.lower() for k in DUE_KEYWORDS):
            continue   # we never use the "Bill Date" as the due date
        for d, m, y in DATE_NUMERIC.findall(line):
            iso = _make_iso(d, m, y)
            if iso:
                valid.add(iso)
            else:
                invalid.append(f"{d}/{m}/{y}")
        for d, word, y in DATE_WORDS.findall(line):
            month = MONTHS.get(word[:3].lower())
            if month:
                iso = _make_iso(d, month, y)
                if iso:
                    valid.add(iso)
                else:
                    invalid.append(f"{d} {word} {y}")
    if len(valid) == 1:
        return next(iter(valid)), []
    if len(valid) > 1:
        return None, ["more than one due date found: "
                      + ", ".join(sorted(valid))]
    if invalid:
        return None, ["due date is not a real date: " + ", ".join(invalid)]
    return None, ["due date not found"]


def extract_bill_fields(text):
    """Text -> dictionary. Works on text only, so it is easy to test."""
    if not isinstance(text, str) or not text.strip():
        return _build_result(None, None, None, ["no text was found"], "",
                             status="error")
    payee, p1 = find_payee(text)
    amount, p2 = find_amount(text)
    due_date, p3 = find_due_date(text)
    return _build_result(payee, amount, due_date, p1 + p2 + p3, text)


def setup_tesseract(pytesseract):
    """Find tesseract.exe: environment variable first, then the usual folder."""
    path = os.environ.get("TESSERACT_CMD")
    if not path and os.path.exists(DEFAULT_WINDOWS_TESSERACT):
        path = DEFAULT_WINDOWS_TESSERACT
    if path:
        pytesseract.pytesseract.tesseract_cmd = path


def read_bill_image(path):
    """Image file -> dictionary. Any failure becomes status "error"."""
    if not os.path.exists(path):
        return _build_result(None, None, None, ["image file not found"], "",
                             status="error")
    try:
        import pytesseract
        from PIL import Image
        setup_tesseract(pytesseract)
        with Image.open(path) as image:
            text = pytesseract.image_to_string(image)
    except Exception as e:
        return _build_result(None, None, None,
                             [f"could not read the image: {e}"], "",
                             status="error")
    return extract_bill_fields(text)

# ---------------------------------------------------------------
# Part 2: preset (demo) bills and the ONE function the backend calls
# ---------------------------------------------------------------
import json

BILLS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "bills.json")


def load_preset_bills(path=BILLS_FILE):
    """Read data/bills.json. Raises ValueError with a clear reason if broken."""
    try:
        with open(path, encoding="utf-8") as f:
            bills = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"preset bills file not found: {path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"preset bills file is not valid JSON: {e}")
    if not isinstance(bills, list):
        raise ValueError("preset bills file must contain a list")
    for bill in bills:
        if (not isinstance(bill, dict) or "id" not in bill
                or not isinstance(bill.get("lines"), list)):
            raise ValueError("every preset bill needs an 'id' and a 'lines' list")
    return bills


def get_preset_bill(preset_id, path=BILLS_FILE):
    """Preset id -> the same dictionary as OCR, labelled as a preset."""
    try:
        bills = load_preset_bills(path)
    except ValueError as e:
        return _build_result(None, None, None, [str(e)], "", status="error")
    for bill in bills:
        if bill["id"] == preset_id:
            result = extract_bill_fields("\n".join(bill["lines"]))
            result["source"] = "preset"
            return result
    return _build_result(None, None, None,
                         [f"unknown preset id: {preset_id}"], "",
                         status="error")


def analyze_bill(image_path=None, preset_id=None, fallback_preset_id=None):
    """The ONE function the backend should call. Always returns 10 keys.

    - preset_id: use that preset on purpose (for the demo).
    - image_path: read the image with OCR.
    - fallback_preset_id: ONLY if OCR fails AND this is given, return that
      preset, clearly labelled as a fallback. Otherwise OCR failure is
      returned as an error. A sample bill is never swapped in silently.
    """
    def finish(result, source, used_preset=None, ocr_failed=False):
        out = dict(result)
        out["source"] = source
        out["preset_id"] = used_preset
        out["ocr_failed"] = ocr_failed
        return out

    if preset_id:
        return finish(get_preset_bill(preset_id), "preset", preset_id)

    if not image_path:
        no_input = _build_result(None, None, None,
                                 ["no image or preset id was given"], "",
                                 status="error")
        return finish(no_input, "none")

    result = read_bill_image(image_path)
    nothing_read = not any((result["payee"], result["amount"],
                            result["due_date"]))
    if result["status"] != "error" and not nothing_read:
        return finish(result, "ocr")          # read at least something

    # OCR failed. Use a sample ONLY if the caller allowed it.
    if fallback_preset_id:
        sample = get_preset_bill(fallback_preset_id)
        if sample["status"] != "error":
            sample["status"] = "fallback"
            sample["problems"] = (
                ["we could not read your photo; these are SAMPLE bill "
                 "details, not your bill"] + result["problems"])
            return finish(sample, "preset_fallback", fallback_preset_id,
                          ocr_failed=True)
    return finish(result, "ocr", ocr_failed=True)