import asyncio
import os
import tts


def test_empty_text_is_an_error(tmp_path):
    r = tts.make_speech("", output_dir=str(tmp_path))
    assert r["status"] == "error"
    assert r["audio_path"] is None


def test_success_returns_text_and_audio(monkeypatch, tmp_path):
    async def fake(text, voice, rate, path):
        with open(path, "wb") as f:
            f.write(b"fake mp3 bytes")
    monkeypatch.setattr(tts, "_save_audio", fake)
    r = tts.make_speech("你好", output_dir=str(tmp_path))
    assert r["status"] == "ok"
    assert r["text"] == "你好"
    assert os.path.getsize(r["audio_path"]) > 0


def test_failure_keeps_the_text(monkeypatch, tmp_path):
    async def fake(text, voice, rate, path):
        raise ConnectionError("no internet")
    monkeypatch.setattr(tts, "_save_audio", fake)
    r = tts.make_speech("阿婆，你好", output_dir=str(tmp_path))
    assert r["status"] == "text_only"
    assert r["text"] == "阿婆，你好"
    assert r["audio_path"] is None
    assert any("audio failed" in p for p in r["problems"])


def test_empty_audio_file_counts_as_failure(monkeypatch, tmp_path):
    async def fake(text, voice, rate, path):
        open(path, "wb").close()          # a file with 0 bytes
    monkeypatch.setattr(tts, "_save_audio", fake)
    r = tts.make_speech("你好", output_dir=str(tmp_path))
    assert r["status"] == "text_only"
    assert os.listdir(tmp_path) == []


def test_half_written_file_is_removed(monkeypatch, tmp_path):
    async def fake(text, voice, rate, path):
        with open(path, "wb") as f:
            f.write(b"partial")
        raise ConnectionError("dropped half way")
    monkeypatch.setattr(tts, "_save_audio", fake)
    r = tts.make_speech("你好", output_dir=str(tmp_path))
    assert r["status"] == "text_only"
    assert os.listdir(tmp_path) == []


def test_saved_audio_is_reused(monkeypatch, tmp_path):
    async def good(text, voice, rate, path):
        with open(path, "wb") as f:
            f.write(b"fake mp3 bytes")
    monkeypatch.setattr(tts, "_save_audio", good)
    first = tts.make_speech("你好", output_dir=str(tmp_path))

    async def broken(text, voice, rate, path):
        raise ConnectionError("offline")
    monkeypatch.setattr(tts, "_save_audio", broken)
    second = tts.make_speech("你好", output_dir=str(tmp_path))
    assert second["status"] == "ok"
    assert second["audio_path"] == first["audio_path"]


def test_timeout_is_text_only(monkeypatch, tmp_path):
    async def fake(text, voice, rate, path):
        raise asyncio.TimeoutError()
    monkeypatch.setattr(tts, "_save_audio", fake)
    r = tts.make_speech("你好", output_dir=str(tmp_path))
    assert r["status"] == "text_only"
    assert r["problems"]


def test_inside_a_running_loop_is_text_only():
    async def inner():
        return tts.make_speech("你好")
    r = asyncio.run(inner())
    assert r["status"] == "text_only"
    assert r["text"] == "你好"