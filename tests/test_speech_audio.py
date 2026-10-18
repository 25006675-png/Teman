import speech


def fake_result(text, status, problems=None):
    """Build a pretend Whisper result, so we test our rules, not Whisper."""
    return lambda path, **options: {
        "text": text, "status": status,
        "problems": problems or [], "avg_logprob": -0.3,
    }


def test_missing_file_is_unclear():
    r = speech.listen_for_command("this_file_does_not_exist.m4a")
    assert r["command"] == "unclear"
    assert r["status"] == "error"


def test_clean_confirm_gives_confirm(monkeypatch):
    monkeypatch.setattr(speech, "transcribe_audio", fake_result("確認", "ok"))
    assert speech.listen_for_command("x.m4a")["command"] == "confirm"


def test_clean_cancel_gives_cancel(monkeypatch):
    monkeypatch.setattr(speech, "transcribe_audio", fake_result("取消", "ok"))
    assert speech.listen_for_command("x.m4a")["command"] == "cancel"


def test_unreliable_confirm_is_unclear(monkeypatch):
    monkeypatch.setattr(speech, "transcribe_audio",
                        fake_result("確認", "unreliable", ["low confidence"]))
    assert speech.listen_for_command("x.m4a")["command"] == "unclear"


def test_error_is_unclear(monkeypatch):
    monkeypatch.setattr(speech, "transcribe_audio",
                        fake_result("", "error", ["transcription failed"]))
    assert speech.listen_for_command("x.m4a")["command"] == "unclear"


def test_clean_but_unknown_text_is_unclear(monkeypatch):
    monkeypatch.setattr(speech, "transcribe_audio", fake_result("係你妹", "ok"))
    assert speech.listen_for_command("x.m4a")["command"] == "unclear"