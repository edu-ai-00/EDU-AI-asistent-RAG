"""Unit tests for the typo repair of the query (search/app/nodes/query_repair.py).

Pure: the vocabulary and the dictionary are stubbed, no DB. Run inside the
search image, where the app package and simplemma live:

    docker exec -i edu_ai_search python - < tests/test_query_repair.py

The test_* functions are also pytest-compatible.

What it protects: a word the lemmatizer leaves as it is AND the dictionary does
not know becomes the ONE vocabulary word of the school's curated questions
within edit distance 1 (2 from 8 letters). A tie, nothing near, a known word, a
short word, a number: the query stays as it is. Measured 2026-10-26 on the
independent set B (1 240 queries): +37 confident right answers, 0 new wrong.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.nodes import query_repair as qr  # noqa: E402

VOCAB = {"absence", "omluvit", "prijimaci", "zkouska", "termin", "ctvrtleti", "tridni", "ucitel"}


def _state(norm, lemma=None):
    return {"query_norm_tokens": list(norm), "query_lemma_tokens": list(lemma or norm), "pack_ids": ["p"]}


def _repair(norm, lemma=None, known=False, vocab=VOCAB):
    qr._vocab = lambda pack_ids: set(vocab)
    qr._known = lambda word: known
    return qr.repair_state(_state(norm, lemma), ["typo"], ["p"])


def test_distance_counts_a_swap_as_one():
    assert qr._dist("abesnce", "absence", 2) == 1        # swapped neighbours
    assert qr._dist("zkouska", "zkouska", 2) == 0
    assert qr._dist("zkouska", "zkouka", 2) == 1
    assert qr._dist("kocka", "pes", 1) > 1                # early exit above the cap


def test_typo_becomes_the_one_vocabulary_word():
    out = _repair(["abesnce", "omluvit"])
    assert out["query_lemma_tokens"] == ["absence", "omluvit"]
    assert out["query_norm_tokens"] == ["absence", "omluvit"]
    assert out["query_repair"] == ["abesnce→absence"]


def test_long_word_allows_two_edits():
    assert _repair(["prijimacci"])["query_lemma_tokens"] == ["prijimaci"]       # 10 letters, 1 edit
    assert _repair(["prijimcai"])["query_lemma_tokens"] == ["prijimaci"]        # 9 letters, a swap
    assert _repair(["ctvrtlet"])["query_lemma_tokens"] == ["ctvrtleti"]         # 8 letters, 1 edit


def test_word_in_the_vocabulary_or_known_to_the_dictionary_is_left_alone():
    s = _state(["termin"])
    qr._vocab = lambda pack_ids: set(VOCAB)
    qr._known = lambda word: False
    assert qr.repair_state(s, ["typo"], ["p"]) is s                              # nothing changed
    assert _repair(["tridnim"], known=True) == _state(["tridnim"])               # a real word, not a typo


def test_a_changed_lemma_is_not_a_typo():
    # the lemmatizer already did something with the word: it is not unknown
    assert _repair(["ucitelu"], lemma=["ucitel"]) == _state(["ucitelu"], ["ucitel"])


def test_a_tie_leaves_the_query_alone():
    # "termix" is one edit from both "termin" and "termit"
    out = _repair(["termix"], vocab={"termin", "termit"})
    assert out == _state(["termix"])


def test_short_words_and_numbers_are_never_touched():
    assert _repair(["abc"]) == _state(["abc"])
    assert _repair(["2026"]) == _state(["2026"])


def test_nothing_near_means_no_repair():
    assert _repair(["klavesnice"]) == _state(["klavesnice"])


def test_misaligned_tokens_or_empty_vocabulary_leave_the_query_alone():
    s = {"query_norm_tokens": ["abesnce"], "query_lemma_tokens": [], "pack_ids": ["p"]}
    assert qr.repair_state(s, ["typo"], ["p"]) is s
    assert _repair(["abesnce"], vocab=set()) == _state(["abesnce"])


def test_unknown_mode_does_nothing():
    qr._vocab = lambda pack_ids: set(VOCAB)
    qr._known = lambda word: False
    s = _state(["abesnce"])
    assert qr.repair_state(s, ["colloquial"], ["p"]) is s


def test_the_input_state_is_not_mutated():
    s = _state(["abesnce"])
    qr._vocab = lambda pack_ids: set(VOCAB)
    qr._known = lambda word: False
    out = qr.repair_state(s, ["typo"], ["p"])
    assert out is not s and s["query_lemma_tokens"] == ["abesnce"]


def test_the_log_holds_no_words_of_the_question():
    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = H()
    qr.logger.addHandler(h)
    qr.logger.setLevel(logging.INFO)
    try:
        _repair(["abesnce"])
    finally:
        qr.logger.removeHandler(h)
    assert records and all("abesnce" not in m and "absence" not in m for m in records), records


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    print("FAILED" if fails else "OK")
    sys.exit(1 if fails else 0)
