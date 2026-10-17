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