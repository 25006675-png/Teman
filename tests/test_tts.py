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

# ---- POST /speak: the page's read-aloud ----------------------------------------

def _speak_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import app as app_module
    monkeypatch.setattr(tts, "OUTPUT_DIR", str(tmp_path))
    return TestClient(app_module.app)  # no lifespan: /speak never touches the database


def test_speak_returns_cantonese_audio(monkeypatch, tmp_path):
    async def fake(text, voice, rate, path):
        with open(path, "wb") as f:
            f.write(b"fake mp3 bytes")
    monkeypatch.setattr(tts, "_save_audio", fake)
    response = _speak_client(monkeypatch, tmp_path).post("/speak", json={"text": "你戶口有 807蚊50仙。"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"fake mp3 bytes"


def test_speak_without_internet_says_text_only(monkeypatch, tmp_path):
    async def fake(text, voice, rate, path):
        raise ConnectionError("no internet")
    monkeypatch.setattr(tts, "_save_audio", fake)
    response = _speak_client(monkeypatch, tmp_path).post("/speak", json={"text": "阿婆，你好"})
    assert response.status_code == 503
    assert response.json()["status"] == "text_only"


def test_speak_refuses_empty_or_long_text(monkeypatch, tmp_path):
    client = _speak_client(monkeypatch, tmp_path)
    assert client.post("/speak", json={"text": "  "}).status_code == 422
    assert client.post("/speak", json={"text": "好" * 401}).status_code == 422
