import pytest
from metrics import cer, edit_distance


def test_identical_text_has_zero_error():
    assert cer("abc", "abc") == 0


def test_one_wrong_letter():
    assert cer("abc", "abd") == pytest.approx(1 / 3)


def test_one_missing_character():
    assert cer("你好", "你") == pytest.approx(0.5)


def test_empty_hypothesis_is_total_error():
    assert cer("abc", "") == 1.0


def test_punctuation_and_spaces_are_ignored():
    assert cer("確認！", " 確認 ") == 0


def test_empty_reference_is_rejected():
    with pytest.raises(ValueError):
        cer("", "abc")


def test_edit_distance_basic():
    assert edit_distance("kitten", "sitting") == 3
    