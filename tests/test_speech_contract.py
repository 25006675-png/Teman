import pytest
import speech


def fake_result(text, status="ok", problems=None):
    return lambda source, **options: {
        "text": text, "status": status,
        "problems": problems or [], "avg_logprob": -0.3,
    }


def test_confirm(monkeypatch):
    monkeypatch.setattr(speech, "transcribe_audio", fake_result("確認"))
    assert speech.match_keyword(b"audio") == "confirm"


def test_cancel(monkeypatch):
    monkeypatch.setattr(speech, "transcribe_audio", fake_result("取消"))
    assert speech.match_keyword(b"audio") == "cancel"


def test_repeat_request_is_none(monkeypatch):
    monkeypatch.setattr(speech, "transcribe_audio", fake_result("再講一次"))
    assert speech.match_keyword(b"audio") is None


def test_unclear_is_none(monkeypatch):
    monkeypatch.setattr(speech, "transcribe_audio", fake_result("係你妹"))
    assert speech.match_keyword(b"audio") is None


def test_unreliable_confirm_is_none(monkeypatch):
    monkeypatch.setattr(speech, "transcribe_audio",
                        fake_result("確認", "unreliable", ["low confidence"]))
    assert speech.match_keyword(b"audio") is None


def test_error_is_none(monkeypatch):
    monkeypatch.setattr(speech, "transcribe_audio",
                        fake_result("", "error", ["transcription failed"]))
    assert speech.match_keyword(b"audio") is None


@pytest.mark.parametrize("bad", [None, "確認", 123, b""])
def test_not_audio_is_none(bad):
    assert speech.match_keyword(bad) is None


def test_crash_inside_is_none(monkeypatch):
    def boom(source, **options):
        raise RuntimeError("model crashed")
    monkeypatch.setattr(speech, "transcribe_audio", boom)
    assert speech.match_keyword(b"audio") is None


@pytest.mark.parametrize("text", ["確認", "取消", "再講一次", "maybe", "係你妹"])
def test_only_three_possible_values(monkeypatch, text):
    monkeypatch.setattr(speech, "transcribe_audio", fake_result(text))
    assert speech.match_keyword(b"audio") in ("confirm", "cancel", None)


def test_audio_is_passed_as_a_memory_file(monkeypatch):
    seen = {}

    def fake(source, **options):
        seen["data"] = source.read()
        return {"text": "確認", "status": "ok", "problems": [], "avg_logprob": -0.3}
    monkeypatch.setattr(speech, "transcribe_audio", fake)
    assert speech.match_keyword(b"abc") == "confirm"
    assert seen["data"] == b"abc"