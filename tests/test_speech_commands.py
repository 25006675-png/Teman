import pytest
from speech import (match_command, CONFIRM_PHRASES, CANCEL_PHRASES,
                    REPEAT_PHRASES)


@pytest.mark.parametrize("text", [
    "確認", "确认", "確認！", " 確認 ", "確認呀", "Confirm", "係，畀錢", "yes, pay it",
])
def test_confirm(text):
    assert match_command(text) == "confirm"


@pytest.mark.parametrize("text", [
    "取消", "唔好", "唔好畀錢", "Cancel", "No.", "don't pay",
])
def test_cancel(text):
    assert match_command(text) == "cancel"


@pytest.mark.parametrize("text", [
    "再講一次", "再说一次", "Say again", "repeat",
])
def test_repeat(text):
    assert match_command(text) == "repeat"


@pytest.mark.parametrize("text", [
    "",                    # nothing was said
    "   ",                 # only spaces
    None,                  # no text at all
    123,                   # not text
    "我唔愛畀你",           # real Whisper output from yue_04 (smoke test)
    "咁我哋哋哋哋",         # real Whisper output from yue_01 (smoke test)
    "今日我可以用幾多類",    # real Whisper output from yue_02 (smoke test)
    "係你妹",               # real Whisper output from yue_03 - has 係 but must NOT confirm
    "唔好確認",             # "don't confirm" - must NOT confirm
    "確認取消",             # both words in one sentence
    "maybe",                # unknown word
])
def test_unclear_is_never_a_command(text):
    assert match_command(text) == "unclear"


def test_phrase_lists_do_not_overlap():
    assert not (CONFIRM_PHRASES & CANCEL_PHRASES)
    assert not (CONFIRM_PHRASES & REPEAT_PHRASES)
    assert not (CANCEL_PHRASES & REPEAT_PHRASES)

@pytest.mark.parametrize("text", ["唔好付款", "唔好付錢", "唔付款", "未付款"])
def test_negated_pay_phrases_never_confirm(text):
    assert match_command(text) != "confirm"