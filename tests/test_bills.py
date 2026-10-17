import ocr

KEYS = {"payee", "amount", "due_date", "status", "problems", "raw_text",
        "source", "trusted", "preset_id", "ocr_failed"}


def test_file_has_the_four_presets():
    ids = [b["id"] for b in ocr.load_preset_bills()]
    assert ids == ["normal_tnb", "high_tnb", "typo_tnb", "scam"]


def test_normal_preset():
    r = ocr.analyze_bill(preset_id="normal_tnb")
    assert (r["payee"], r["amount"], r["due_date"]) == ("TNB", "42.50", "2026-10-25")
    assert r["status"] == "ok"
    assert r["source"] == "preset" and r["preset_id"] == "normal_tnb"
    assert r["ocr_failed"] is False and r["trusted"] is False
    assert set(r) == KEYS


def test_high_preset():
    r = ocr.analyze_bill(preset_id="high_tnb")
    assert (r["payee"], r["amount"]) == ("TNB", "389.90")
    assert r["status"] == "ok"


def test_typo_preset_is_flagged_not_trusted():
    r = ocr.analyze_bill(preset_id="typo_tnb")
    assert r["amount"] == "42.50"          # 42.5O repaired, but flagged
    assert r["due_date"] is None           # 25/1O/2026 is not read
    assert r["status"] == "needs_check"
    assert any("letters" in p for p in r["problems"])
    assert "due date not found" in r["problems"]


def test_scam_preset_is_only_data():
    r = ocr.analyze_bill(preset_id="scam")
    assert "safe account" in r["raw_text"].lower()
    assert r["trusted"] is False
    # ocr.py does NOT detect scams. It reads fields and keeps the text.
    # Tech 2's rules.py must scan raw_text and decide SCAM_SIGNAL.
    assert r["status"] == "ok"


def test_unreadable_image_without_fallback_is_an_error():
    r = ocr.analyze_bill(image_path="no_such_file.png")
    assert r["status"] == "error"
    assert r["source"] == "ocr"
    assert r["ocr_failed"] is True
    assert r["amount"] is None and r["preset_id"] is None


def test_fallback_is_clearly_labelled():
    r = ocr.analyze_bill(image_path="no_such_file.png",
                         fallback_preset_id="normal_tnb")
    assert r["status"] == "fallback"
    assert r["source"] == "preset_fallback"
    assert r["ocr_failed"] is True
    assert r["preset_id"] == "normal_tnb"
    assert r["trusted"] is False
    assert any("SAMPLE" in p for p in r["problems"])
    assert set(r) == KEYS


def test_no_input_is_an_error():
    assert ocr.analyze_bill()["status"] == "error"


def test_unknown_preset_is_an_error():
    r = ocr.analyze_bill(preset_id="does_not_exist")
    assert r["status"] == "error"


def test_broken_json_file_is_reported(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    r = ocr.get_preset_bill("normal_tnb", path=str(bad))
    assert r["status"] == "error"
    assert any("not valid JSON" in p for p in r["problems"])