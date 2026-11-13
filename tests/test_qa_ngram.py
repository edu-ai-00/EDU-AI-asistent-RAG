"""Unit tests for the n-gram chip of the curated-QA offer (search/app/nodes/qa_ngram.py).

Pure: no DB (the ranking is stubbed where the database would be read). Run inside
the search image, where the app package lives:

    docker exec -i edu_ai_search python - < tests/test_qa_ngram.py

The test_* functions are also pytest-compatible.

What it protects: with `offer_ngram` off nothing changes; with it on, only the
LAST chip of an offer that replaces an answer (the deferred "did you mean" list or
the dead-end fallback) can change, a confident answer never does; an entry scores
with its best single wording, not the union of its wordings; generic words carry
no weight.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.nodes import qa_ngram as ng  # noqa: E402
from app.nodes import qa_lexical as ql  # noqa: E402

ROWS = [
    # entry, canonical question, norm_text of a variant, eligible (chosen area)
    ("e1", "Kdo je tridni ucitel?", "kdo je tridni ucitel", True),
    ("e2", "Jak omluvit absenci?", "jak omluvit absenci skola", True),
    ("e2", "Jak omluvit absenci?", "omluvenka skola", True),
    ("e3", "Kdy jsou prijimaci zkousky z matematiky?", "kdy jsou prijimaci zkousky matematiky", True),
    ("e4", "Muze mit dite ve skole mobil?", "muze mit dite skole mobil", True),
    ("e5", "Kdy je skolni vylet?", "kdy skolni vylet skola", False),
]
STOP = {"kdo", "jak", "kdy", "je", "jsou"}


def _index(max_df=0.5):
    return ng.NgramIndex(ROWS, STOP, max_df, 3)


def test_grams_mark_word_edges():
    g = ng.grams(["abc"])
    assert {"<ab", "abc", "bc>", "<abc", "abc>", "<abc>"} <= set(g)


def test_derived_form_finds_its_entry():
    # "tridnicke" is not "tridni" for a lemma, but shares most of its letters
    top = _index().rank(["tridnicke"], 0.1)
    assert top and top[0][0] == "e1", top


def test_colloquial_short_form_finds_its_entry():
    top = _index().rank(["matika"], 0.1)
    assert top and top[0][0] == "e3", top


def test_generic_word_carries_nothing():
    # "skola" is in 2 of the 5 entries here (40 %), above a 30 % share: generic
    idx = _index(max_df=0.3)
    assert "skola" in idx.generic
    assert idx.rank(["skola"], 0.0) == []


def test_best_single_wording_not_union():
    # e2 has two wordings; the query matches one of them fully
    top = _index().rank(["omluvenka"], 0.1)
    assert top[0][0] == "e2" and top[0][2] > 0.9, top


def test_ineligible_rows_count_but_are_never_offered():
    idx = _index()
    assert all(eid != "e5" for eid, _q, _s in idx.rank(["vylet"], 0.0))


def test_threshold_and_no_words():
    idx = _index()
    assert idx.rank(["xyzxyz"], 0.3) == []
    assert idx.rank([], 0.0) == []
    assert idx.rank(["12"], 0.0) == []


def test_an_exact_tie_is_settled_by_the_question_not_the_row_order():
    # two entries with the same wording score the same; whichever the database
    # returns first must not decide the chip (ids change with every re-upload)
    rows = [("z9", "Kde najdu pokyny?", "kde najdu pokyny jpz", True),
            ("a1", "Kde pise uchazec?", "kde najdu pokyny jpz", True),
            ("m5", "Jak omluvit absenci?", "omluvit absenci", True)]
    for order in (rows, list(reversed(rows))):
        idx = ng.NgramIndex(order, STOP, 0.9, 3)
        got = [e for e, _q, _s in idx.rank(["najdu", "pokyny"], 0.1)]
        assert got[:2] == ["z9", "a1"], got           # "Kde najdu…" < "Kde pise…"


def _items(*ids):
    return [{"kind": "qa", "entry_id": i, "question": f"q{i}", "score": 0.5} for i in ids]


def test_merge_replaces_only_the_last_chip():
    chips, chip = ng.merge_chip(_items("a", "b", "c"), [("x", "qx", 0.4)], 3)
    assert [c["entry_id"] for c in chips] == ["a", "b", "x"] and chip["entry_id"] == "x"


def test_merge_skips_a_chip_already_shown():
    chips, chip = ng.merge_chip(_items("a", "b", "c"), [("a", "qa", 0.9), ("c", "qc", 0.5)], 3)
    assert [c["entry_id"] for c in chips] == ["a", "b", "c"] and chip["entry_id"] == "c"


def test_merge_dedupes_on_question_text():
    chips, chip = ng.merge_chip(_items("a", "b"), [("z", "QA ", 0.9)], 3)
    assert chip is None and [c["entry_id"] for c in chips] == ["a", "b"]


def test_merge_fills_a_short_or_empty_row():
    chips, _ = ng.merge_chip(_items("a"), [("x", "qx", 0.4)], 3)
    assert [c["entry_id"] for c in chips] == ["a", "x"]
    chips, _ = ng.merge_chip([], [("x", "qx", 0.4)], 3)
    assert [c["entry_id"] for c in chips] == ["x"]


def test_merge_without_ranking_keeps_the_row():
    chips, chip = ng.merge_chip(_items("a", "b", "c", "d"), [], 3)
    assert chip is None and [c["entry_id"] for c in chips] == ["a", "b", "c"]


def _with_stub(out, ranked, params=None):
    real = ng.ranked_for
    ng.ranked_for = lambda cur, state, opts: ranked
    try:
        return ng.with_ngram_chip(out, {}, {"suggest_top_n": 3, "offer_ngram": True, **(params or {})}, None)
    finally:
        ng.ranked_for = real


def test_confident_answer_is_never_touched():
    out = {"resolved": True, "decision": "A", "suggestions": _items("a", "b", "c")}
    assert _with_stub(out, [("x", "qx", 0.9)]) == out


def test_deferred_offer_gets_the_chip():
    out = {"resolved": False, "decision": "B-deferred",
           "deferred_suggest": {"suggestions": _items("a", "b", "c")}, "trace_detail": {}}
    res = _with_stub(out, [("x", "qx", 0.5)])
    assert [c["entry_id"] for c in res["deferred_suggest"]["suggestions"]] == ["a", "b", "x"]
    assert res["trace_detail"]["ngram_chip"]["entry_id"] == "x"
    assert [c["entry_id"] for c in out["deferred_suggest"]["suggestions"]] == ["a", "b", "c"]


def test_fallback_gets_the_chip():
    out = {"resolved": False, "decision": "C", "fallback_suggestions": _items("a")}
    res = _with_stub(out, [("x", "qx", 0.5)])
    assert [c["entry_id"] for c in res["fallback_suggestions"]] == ["a", "x"]


def test_no_offer_slot_no_change():
    out = {"resolved": False, "decision": "C"}
    assert _with_stub(out, [("x", "qx", 0.5)]) == out


def test_options():
    assert ng.options(True) == ng.DEFAULTS
    assert ng.options({"min_sim": 0.4, "junk": 1})["min_sim"] == 0.4


def test_off_by_default_never_reaches_ngrams():
    real_ranked, real_with = ql._qa_lexical_ranked, ng.with_ngram_chip
    fixed = {"resolved": False, "decision": "C", "fallback_suggestions": _items("a")}
    ql._qa_lexical_ranked = lambda state, params=None: fixed

    def boom(*a, **k):
        raise AssertionError("n-grams called while off")
    ng.with_ngram_chip = boom
    try:
        assert ql._qa_lexical_decision({}, {}) is fixed
        assert ql._qa_lexical_decision({}, {"offer_ngram": False}) is fixed
    finally:
        ql._qa_lexical_ranked, ng.with_ngram_chip = real_ranked, real_with


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"Ran {n} tests")
    print("OK")
