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
import app.languages as languages  # noqa: E402

_REAL_KNOWN = qr._known
_REAL_VOCAB = qr._vocab

VOCAB = {"absence", "omluvit", "prijimaci", "zkouska", "termin", "ctvrtleti", "tridni", "ucitel"}


def _state(norm, lemma=None):
    return {"query_norm_tokens": list(norm), "query_lemma_tokens": list(lemma or norm), "pack_ids": ["p"],
            "query_language": "cs"}


def _repair(norm, lemma=None, known=False, vocab=VOCAB):
    qr._vocab = lambda state, pack_ids: set(vocab)
    qr._known = lambda word, langs: known
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
    qr._vocab = lambda state, pack_ids: set(VOCAB)
    qr._known = lambda word, langs: False
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
    qr._vocab = lambda state, pack_ids: set(VOCAB)
    qr._known = lambda word, langs: False
    s = _state(["abesnce"])
    assert qr.repair_state(s, ["colloquial"], ["p"]) is s


def test_the_input_state_is_not_mutated():
    s = _state(["abesnce"])
    qr._vocab = lambda state, pack_ids: set(VOCAB)
    qr._known = lambda word, langs: False
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


def _real_repair(norm, language, school=("cs",), vocab=VOCAB):
    """The real dictionary check (simplemma in the image), stubbed vocabulary."""
    qr._vocab = lambda state, pack_ids: set(vocab)
    qr._known = _REAL_KNOWN
    languages.languages_for_entity = lambda entity: tuple(school)
    st = {**_state(norm), "query_language": language}
    return qr.repair_state(st, ["typo"], ["p"])


def test_the_query_language_picks_the_dictionary():
    # "late" is an English word: an English query keeps it, a Czech one does not know it
    assert "query_repair" not in _real_repair(["late"], "en", vocab={"date"})
    assert _real_repair(["late"], "cs", vocab={"date"})["query_norm_tokens"] == ["date"]
    assert _real_repair(["abesnce"], "cs")["query_norm_tokens"] == ["absence"]


def test_an_undecided_query_is_judged_by_the_schools_languages():
    # no language decided: a cs-only school repairs as before, a school that also
    # teaches English leaves a word that English knows
    assert _real_repair(["abesnce"], None, school=("cs",))["query_norm_tokens"] == ["absence"]
    assert _real_repair(["late"], None, school=("cs",), vocab={"date"})["query_norm_tokens"] == ["date"]
    assert "query_repair" not in _real_repair(["late"], None, school=("cs", "en"), vocab={"date"})


class _Cur:
    """Records the vocabulary query; returns one row."""
    def __init__(self, rows):
        self.rows, self.sql, self.params = rows, [], []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        self.params.append(params)

    def fetchall(self):
        return self.rows


def _vocab_with(state, rows=(("absence", "absence"),)):
    from contextlib import contextmanager
    cur = _Cur(list(rows))

    @contextmanager
    def fake_cursor():
        yield cur
    qr.get_cursor = fake_cursor
    qr._VOCAB.clear()
    return _REAL_VOCAB(state, ["p"]), cur


def test_the_vocabulary_is_read_through_scope_and_areas():
    st = {"entity_scope": ["E"], "organization_entities": ["E"], "user_role": "STUDENT",
          "domain_ids": [7]}
    words, cur = _vocab_with(st)
    assert words == {"absence"}
    sql, params = cur.sql[0], cur.params[0]
    assert "legal_entity_code" in sql and "visibility" in sql    # scope.where
    assert "valid_" in sql                                       # validity
    assert "domain_ids" in sql                                   # the chosen areas
    assert ["E"] in params and ["STUDENT"] in params


def test_two_scopes_never_share_a_cached_vocabulary():
    a = {"entity_scope": ["E"], "organization_entities": ["E"], "user_role": "STUDENT"}
    b = {**a, "user_role": "TEACHER"}
    _, cur = _vocab_with(a)
    _REAL_VOCAB(a, ["p"])                                        # cached: no new query
    _REAL_VOCAB(b, ["p"])                                        # another scope: a query
    assert len(cur.sql) == 2, cur.sql


def test_a_failing_vocabulary_leaves_the_query_alone():
    def boom(state, pack_ids):
        raise RuntimeError("db down")
    qr._vocab = boom
    st = _state(["abesnce"])
    assert qr.repair_state(st, ["typo"], ["p"]) is st


def test_a_word_no_language_can_judge_counts_as_known():
    assert _REAL_KNOWN("abesnce", ()) is True
    assert _REAL_KNOWN("abesnce", ("xx",)) is True      # no dictionary for it
    assert _REAL_KNOWN("abesnce", ("cs",)) is False


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
