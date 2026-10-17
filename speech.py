"""speech.py - turns what the user said (as text) into a safe command.

This module NEVER pays. It only returns a label:
"confirm", "cancel", "repeat" or "unclear".
The backend (app.py) decides what happens next.
"""

import unicodedata

CONFIRM = "confirm"
CANCEL = "cancel"
REPEAT = "repeat"
UNCLEAR = "unclear"

# Polite sounds at the end of a Cantonese sentence, e.g. 確認呀
TRAILING_PARTICLES = "呀啊啦喇囉哦喔吖啫"


def normalize(text):
    """Make different spellings of the same sentence look the same."""
    # Full-width letters become normal letters, and capitals become lowercase
    text = unicodedata.normalize("NFKC", text).lower()
    # Remove spaces, punctuation, symbols and control characters
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] not in "PZSC")
    # Remove polite sounds from the end
    while text and text[-1] in TRAILING_PARTICLES:
        text = text[:-1]
    return text


def _make_set(phrases):
    """Normalize every phrase the same way as the user's text."""
    return {normalize(p) for p in phrases}


# STARTER LISTS - a Cantonese speaker must review these before the demo
CONFIRM_PHRASES = _make_set([
    "確認", "确认", "確定", "确定", "確認付款", "确认付款",
    "係，畀錢", "係，俾錢", "畀錢", "俾錢",
    "confirm", "yes, pay it",
])

CANCEL_PHRASES = _make_set([
    "取消", "唔好", "唔好畀", "唔好畀錢", "唔好俾錢", "唔使", "唔要",
    "唔畀", "唔付", "停", "不要", "不付",
    "cancel", "no", "don't pay", "stop",
])

REPEAT_PHRASES = _make_set([
    "再講一次", "再讲一次", "再講", "再讲", "再說一次", "再说一次",
    "再嚟一次", "重複", "重复", "聽唔到", "聽唔清楚", "听不清楚",
    "repeat", "say again", "again",
])


def match_command(text):
    """Return "confirm", "cancel", "repeat" or "unclear". Never anything else."""
    # Not text at all (None, a number, ...) -> unclear
    if not isinstance(text, str):
        return UNCLEAR

    cleaned = normalize(text)
    if not cleaned:
        return UNCLEAR

    # Collect every command that matches the WHOLE sentence
    hits = []
    if cleaned in CONFIRM_PHRASES:
        hits.append(CONFIRM)
    if cleaned in CANCEL_PHRASES:
        hits.append(CANCEL)
    if cleaned in REPEAT_PHRASES:
        hits.append(REPEAT)

    # Exactly one match -> that command. Zero or several -> unclear
    if len(hits) == 1:
        return hits[0]
    return UNCLEAR

# ---------------------------------------------------------------
# Part 2: from an audio file to a safe command
# ---------------------------------------------------------------
import os
import re

# STARTING GUESSES - tune them with real recordings, never to look good
LOW_CONFIDENCE = -1.0
NO_SPEECH_MAX = 0.6
REPEATED_CHAR = re.compile(r"(.)\1{3,}")   # the same character 4+ times

_models = {}   # loaded Whisper models, so we load each one only once


def get_model(model_size="large-v3", device="cpu", compute_type="int8"):
    """Load Whisper the first time it is needed, then reuse it."""
    key = (model_size, device, compute_type)
    if key not in _models:
        from faster_whisper import WhisperModel
        _models[key] = WhisperModel(model_size, device=device,
                                    compute_type=compute_type)
    return _models[key]


def transcribe_audio(path, language="yue", model_size="large-v3",
                     device="cpu", compute_type="int8"):
    """Audio file -> {"text", "status", "problems", "avg_logprob"}.
    status is "ok", "unreliable" or "error". "ok" only means no warning
    sign was found. It does NOT mean the text is correct."""
    result = {"text": "", "status": "ok", "problems": [], "avg_logprob": None}

    if not os.path.exists(path):
        result["status"] = "error"
        result["problems"].append("audio file not found")
        return result

    try:
        model = get_model(model_size, device, compute_type)
        segments, _info = model.transcribe(path, language=language)
        segments = list(segments)   # the work happens while we loop
    except Exception as e:
        result["status"] = "error"
        result["problems"].append(f"transcription failed: {e}")
        return result

    result["text"] = "".join(s.text for s in segments).strip()
    if not result["text"]:
        result["problems"].append("empty transcript")
    else:
        if REPEATED_CHAR.search(result["text"]):
            result["problems"].append("same character repeated 4+ times")
        logprobs = [s.avg_logprob for s in segments]
        result["avg_logprob"] = round(sum(logprobs) / len(logprobs), 2)
        if result["avg_logprob"] < LOW_CONFIDENCE:
            result["problems"].append("low confidence")
        if max(s.no_speech_prob for s in segments) > NO_SPEECH_MAX:
            result["problems"].append("probably not speech")

    if result["problems"]:
        result["status"] = "unreliable"
    return result


def listen_for_command(path, **options):
    """Audio file -> a safe command. This NEVER pays and never calls the bank.
    Only a clean transcript can produce a command. Everything else is "unclear"."""
    heard = transcribe_audio(path, **options)
    if heard["status"] == "ok":
        command = match_command(heard["text"])
    else:
        command = UNCLEAR
    return {
        "command": command,                 # confirm / cancel / repeat / unclear
        "text": heard["text"],              # what Whisper wrote (for the log)
        "status": heard["status"],
        "problems": heard["problems"],
        "avg_logprob": heard["avg_logprob"],
    }