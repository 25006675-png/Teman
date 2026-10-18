"""tts.py - turns a message into a Cantonese MP3 with edge-tts.

RULE: the text is ALWAYS returned, even when the audio fails. The screen
must keep showing the text. Audio is only a helper.
edge-tts needs an internet connection and is an unofficial route, so it
can fail at any time. A failure is normal and must never crash the app.
"""

import asyncio
import hashlib
import os

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "tts_output")
DEFAULT_VOICE = "zh-HK-HiuGaaiNeural"   # change after you choose by ear
DEFAULT_RATE = "-10%"                   # a little slower; test by ear
TIMEOUT_SECONDS = 20


def _result(text, audio_path, status, problems, voice):
    return {
        "text": text,              # show this on screen, always
        "audio_path": audio_path,  # None when there is no audio
        "status": status,          # "ok", "text_only" or "error"
        "problems": problems,
        "voice": voice,
    }


async def _save_audio(text, voice, rate, path):
    """Ask edge-tts to write the MP3. Separate so tests can replace it."""
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await asyncio.wait_for(communicate.save(path), timeout=TIMEOUT_SECONDS)


async def make_speech_async(text, voice=DEFAULT_VOICE, rate=DEFAULT_RATE,
                            output_dir=OUTPUT_DIR):
    """Use this version inside an async app (for example FastAPI)."""
    if not isinstance(text, str) or not text.strip():
        shown = text if isinstance(text, str) else ""
        return _result(shown, None, "error", ["no text to read"], voice)

    # Same message + voice + speed -> same file name, so we can reuse it
    key = hashlib.sha1(f"{voice}|{rate}|{text}".encode("utf-8")).hexdigest()[:16]
    path = os.path.join(output_dir, f"{key}.mp3")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return _result(text, path, "ok", [], voice)

    try:
        os.makedirs(output_dir, exist_ok=True)
        await _save_audio(text, voice, rate, path)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise RuntimeError("audio file is empty")
    except Exception as e:
        try:
            if os.path.exists(path):
                os.remove(path)        # never keep a half-written file
        except OSError:
            pass
        reason = f"{type(e).__name__}: {e}"
        return _result(text, None, "text_only", [f"audio failed: {reason}"], voice)

    return _result(text, path, "ok", [], voice)


def make_speech(text, **options):
    """Normal (non-async) version, for scripts and tests."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make_speech_async(text, **options))
    # Already inside an async app: do not start a second loop
    shown = text if isinstance(text, str) else ""
    return _result(shown, None, "text_only",
                   ["make_speech cannot run inside an async app; "
                    "use make_speech_async"],
                   options.get("voice", DEFAULT_VOICE))
