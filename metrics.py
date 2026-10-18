"""metrics.py - scoring functions for the speech evaluation."""

import unicodedata


def normalize_for_scoring(text):
    """Same cleaning for the reference and for Whisper's text:
    full-width -> normal, lowercase, no spaces/punctuation/symbols."""
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in text if unicodedata.category(ch)[0] not in "PZSC")


def edit_distance(a, b):
    """Smallest number of single-character changes (insert, delete,
    replace) that turn string a into string b."""
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current.append(min(previous[j] + 1,        # delete
                               current[j - 1] + 1,     # insert
                               previous[j - 1] + cost))  # replace or keep
        previous = current
    return previous[-1]


def cer(reference, hypothesis):
    """Character error rate = edits / length of the reference."""
    ref = normalize_for_scoring(reference)
    hyp = normalize_for_scoring(hypothesis)
    if not ref:
        raise ValueError("reference text is empty")
    return edit_distance(ref, hyp) / len(ref)