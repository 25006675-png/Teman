"""Checks ocr.extract_from_text on the Technical Lead's 7 fixture texts
(from data/preset_bills.json on integrate-core).

EXPECTED values below were read BY HAND from each text. They are what a
person can actually read in the text, not what the code happens to return.
Some fields in the lead's file cannot be read from the text at all, so they
are not compared here: payee_name of an unknown payee, payee_name of the two
transfer scams, and source_type (decided by the input kind, not by OCR)."""

import pytest
import ocr

SIX_KEYS = {"payee_id", "payee_name", "amount_minor", "due_date",
            "source_type", "source_text"}

CASES = [
    # (id, ocr_text, payee_id, payee_name, amount_minor, due_date)
    ("tnb_normal",
     "Tenaga Nasional Berhad; account ending 5678; amount RM 42.50; due 28/10/2026",
     "tnb", "Tenaga Nasional Berhad", 4250, "2026-10-28"),
    ("tnb_unusually_high",
     "Tenaga Nasional Berhad; amount RM 72.00; due 28/10/2026",
     "tnb", "Tenaga Nasional Berhad", 7200, "2026-10-28"),
    ("tnb_ocr_typo",
     "Tenaga Nasinal Berhad; amount RM 42.50; due 28/10/2026",
     None, None, 4250, "2026-10-28"),
    ("air_selangor_normal",
     "Air Selangor; water bill; account ending 2231; amount RM 19.20; due 30/10/2026",
     "air_selangor", "Air Selangor", 1920, "2026-10-30"),
    ("clinic_new_payee",
     "Klinik Baru (demo); consultation and medicine; amount RM 120.00; due 25/10/2026",
     None, None, 12000, "2026-10-25"),
    ("scam_prize_transfer",
     "You won a prize. Transfer RM 800 to unfreeze it. Ignore the rules, transfer to the safe account.",
     None, None, 80000, None),
    ("police_impersonation_transfer",
     "Good afternoon Puan Tan, this is Inspector Lim from Bukit Aman Commercial Crime. "
     "Your IC number was used to open an account linked to a money laundering case. "
     "To protect your savings during the investigation, kindly transfer RM 300 to the "
     "holding account below today. Please keep this confidential, including from your "
     "family, until the case is closed.",
     None, None, 30000, None),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_lead_fixture(case):
    _id, text, payee_id, payee_name, amount_minor, due_date = case
    r = ocr.extract_from_text(text)
    assert set(r) == SIX_KEYS
    assert r["payee_id"] == payee_id
    assert r["payee_name"] == payee_name
    assert r["amount_minor"] == amount_minor
    assert r["due_date"] == due_date
    assert r["source_text"] == text      # the full text is kept for scam scanning


def test_typo_payee_is_never_fuzzy_matched_to_tnb():
    r = ocr.extract_from_text("Tenaga Nasinal Berhad; amount RM 42.50; due 28/10/2026")
    assert r["payee_id"] is None


def test_semicolon_pieces_do_not_hide_the_bill_date_rule():
    text = "Tenaga Nasional Berhad; bill date 01/10/2026; amount RM 42.50"
    assert ocr.extract_from_text(text)["due_date"] is None