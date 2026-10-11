# last change in code by Jan Drozd (2026-09-03).
"""Unit tests for the related questions offered beside an exact QA hit.

Pure: no stack, no DB, no LLM — `_exact_match`, `_lexical_step` and `qa_lexical`
are stubbed, so what is under test is the wrapper's own rules. Run it inside the
search image, like the other node tests:

    docker compose run --rm --entrypoint python edu_ai_search tests/test_qa_exact_related.py

The invariant behind every case: the exact answer is what the caller gets. A
suggestion is an offer, so nothing about building one may change, delay or cost
the answer that was already found.

NOT covered here (needs a live stack): that the ranking actually finds the right
related questions, and that scope/ročník filtering holds — both belong to
qa_lexical and are exercised by the eval sets.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.nodes import qa_exact as qe  # noqa: E402

_HIT = {"trace_detail": {"exact_match": True}, "resolved": True, "outcome": "answer",
        "answer_source": "qa_exact", "matched_entry_id": "E1", "match_score": 1.0,
        "match_margin": 1.0, "answer": "Ano.", "sources_json": "[]"}
_MATCHED_Q = "Můžeme ve škole fotit?"


def _stub(lexical, *, hit=True, lex_params={"suggest_top_n": 3}, fill=()):
    """Point the wrapper at a canned exact match, qa_lexical and category fill."""
    # the shared rules live in qa_common.finalize_chips and need a database; what
    # is under test here is qa_exact's own job — harvesting and self-dedupe — so
    # the finalizer is stubbed with just enough of them to assert order
    def _fin(state, params, entry_id, items, top_n):
        out = list(items) + [c for c in fill
                             if c.get("entry_id") not in {i.get("entry_id") for i in items}]
        if out and all(i.get("score") is not None for i in out):
            out.sort(key=lambda i: -float(i["score"]))
        if not params.get("related_category_topup", True) and items:
            out = list(items)
        return out[:top_n]
    qe._finalize = _fin
    qe._exact_match = lambda state, params=None: (
        (dict(_HIT), _MATCHED_Q) if hit
        else ({"trace_detail": {"exact_match": False}, "resolved": False}, None))
    qe._lexical_step = lambda state: lex_params
    qe.qa_lexical = lexical


def _decision_a(state, params):
    return {"resolved": True, "outcome": "answer", "matched_entry_id": "E1",
            "suggestions": [
                {"kind": "qa", "entry_id": "E1", "question": _MATCHED_Q, "score": 1.0},
                {"kind": "qa", "entry_id": "E9", "question": "  MŮŽEME ve škole fotit? ", "score": 0.9},
                {"kind": "qa", "entry_id": "E2", "question": "Kdy je škola v přírodě?", "score": 0.4},
                {"kind": "qa", "entry_id": "E3", "question": "Kde můžeme být o přestávkách?", "score": 0.35},
                {"kind": "qa", "entry_id": "E4", "question": "Co si vzít na výlet?", "score": 0.31}]}


def test_exact_hit_carries_related_questions():
    """The runners-up qa_lexical would have offered ride along with the answer."""
    _stub(_decision_a)
    out = qe.qa_exact({}, {})
    assert out["answer"] == "Ano." and out["answer_source"] == "qa_exact"
    assert [s["entry_id"] for s in out["suggestions"]] == ["E2", "E3", "E4"]


def test_the_answered_question_is_never_offered_back():
    """Its own entry AND the same text under another entry id are both dropped.

    qa_exact picks one of N entries holding the same question (own school before
    the organisation), so the losers are that question under a different id — a
    chip that re-asks what was just answered.
    """
    _stub(_decision_a)
    qs = [s["question"].strip().casefold() for s in qe.qa_exact({}, {})["suggestions"]]
    assert _MATCHED_Q.strip().casefold() not in qs
    assert "E1" not in [s["entry_id"] for s in qe.qa_exact({}, {})["suggestions"]]


def test_other_grade_offers_ride_along():
    """The other-ročník safety net reaches the caller on this path too."""
    _stub(lambda s, p: {"resolved": False, "decision": "B-deferred",
                        "deferred_suggest": {"suggestions": [
                            {"kind": "qa", "entry_id": "E7", "question": "Jiná otázka?"}]},
                        "other_grade_suggestions": [
                            {"kind": "other_grade", "entry_id": "E5",
                             "question": "Telefony v 9. ročníku?"}]})
    assert [s["entry_id"] for s in qe.qa_exact({}, {})["suggestions"]] == ["E7", "E5"]


def test_below_threshold_entries_never_ride_along():
    """Decision C parks entries BELOW t_suggest for a possible no_answer.

    Next to a verbatim curated answer they are junk — production showed chips at
    0.24 against a 0.30 bar, matched on one shared common word.
    """
    _stub(lambda s, p: {"resolved": False, "decision": "C",
                        "fallback_suggestions": [
                            {"kind": "qa", "entry_id": "E8", "question": "Nesouvisí"}]})
    out = qe.qa_exact({}, {})
    assert not out.get("suggestions") and out["answer"] == "Ano."


def test_a_failing_ranking_still_answers():
    """Building an offer must never cost the answer that was already found."""
    def boom(state, params):
        raise RuntimeError("db down")
    _stub(boom)
    out = qe.qa_exact({}, {})
    assert out["resolved"] and out["answer"] == "Ano." and not out.get("suggestions")


def test_the_switch_turns_it_off():
    """related_suggestions=false restores the fast path: one query, no offers."""
    calls = []
    _stub(lambda s, p: calls.append(1) or _decision_a(s, p))
    out = qe.qa_exact({}, {"related_suggestions": False})
    assert not out.get("suggestions") and not calls


def test_a_disabled_qa_lexical_offers_nothing():
    """Turning the ranking off in the setting turns its suggestions off too."""
    _stub(_decision_a, lex_params=None)
    assert not qe.qa_exact({}, {}).get("suggestions")


def test_a_miss_is_unchanged():
    """No exact match: the cascade continues exactly as before."""
    _stub(_decision_a, hit=False)
    assert qe.qa_exact({}, {}) == {"trace_detail": {"exact_match": False},
                                   "resolved": False}


def test_an_empty_harvest_falls_back_to_the_category():
    """The ranking stopping short of decision A must not cost the chips.

    Measured 2026-09-04: the same question returned three chips without a ročník
    and none with one, because a ročník reorders the tier, the ranking never
    reaches its own fill, and qa_exact was reading the fill off THAT. The chips
    have to hang on the entry this step answered.
    """
    _stub(lambda s, p: {"resolved": False, "decision": "C"},
          fill=[{"kind": "qa", "entry_id": "C1", "question": "Ze stejné kategorie?"}])
    assert [s["entry_id"] for s in qe.qa_exact({}, {})["suggestions"]] == ["C1"]


def test_the_category_fills_in_after_a_ranked_chip():
    """A chip that cleared the bar keeps FIRST place; the category takes the rest.

    Since 2026-09-04 a short row is topped up, not only an empty one: one chip
    above the bar used to leave two slots empty while the category held a dozen
    siblings.
    """
    _stub(lambda s, p: {"resolved": True,
                        "suggestions": [{"kind": "qa", "entry_id": "E2",
                                         "question": "Nad prahem?"}]},
          fill=[{"kind": "qa", "entry_id": "C1", "question": "Ze stejné kategorie?"}])
    assert [s["entry_id"] for s in qe.qa_exact({}, {})["suggestions"]] == ["E2", "C1"]


def test_the_topup_switch_restores_fill_only_on_empty():
    """related_category_topup=false: a non-empty row is left exactly as ranked."""
    _stub(lambda s, p: {"resolved": True,
                        "suggestions": [{"kind": "qa", "entry_id": "E2",
                                         "question": "Nad prahem?"}]},
          fill=[{"kind": "qa", "entry_id": "C1", "question": "Ze stejné kategorie?"}])
    out = qe.qa_exact({}, {"related_category_topup": False})
    assert [s["entry_id"] for s in out["suggestions"]] == ["E2"]


def test_the_row_is_ordered_by_the_number_it_shows():
    """A better category chip outranks a weaker ranked one.

    Appending the fill after the ranked chips put a 0.4435 under a 0.3738 and read
    as a bug — a row has to be monotonic in the number printed on it.
    """
    _stub(lambda s, p: {"resolved": True,
                        "suggestions": [{"kind": "qa", "entry_id": "E2",
                                         "question": "Slabší?", "score": 0.37}]},
          fill=[{"kind": "qa", "entry_id": "C1", "question": "Lepší?", "score": 0.44}])
    assert [s["entry_id"] for s in qe.qa_exact({}, {})["suggestions"]] == ["C1", "E2"]


def test_an_unscored_chip_keeps_the_order_it_arrived_in():
    """other_grade and typo chips carry no score; sorting must not drop them."""
    _stub(lambda s, p: {"resolved": True,
                        "suggestions": [{"kind": "qa", "entry_id": "E2",
                                         "question": "Bez skóre?"}]},
          fill=[{"kind": "qa", "entry_id": "C1", "question": "Taky bez skóre?"}])
    assert [s["entry_id"] for s in qe.qa_exact({}, {})["suggestions"]] == ["E2", "C1"]


TESTS = {
    "related": test_exact_hit_carries_related_questions,
    "self": test_the_answered_question_is_never_offered_back,
    "grade": test_other_grade_offers_ride_along,
    "floor": test_below_threshold_entries_never_ride_along,
    "failing": test_a_failing_ranking_still_answers,
    "off": test_the_switch_turns_it_off,
    "lexoff": test_a_disabled_qa_lexical_offers_nothing,
    "miss": test_a_miss_is_unchanged,
    "catfill": test_an_empty_harvest_falls_back_to_the_category,
    "catnodisp": test_the_category_fills_in_after_a_ranked_chip,
    "topupoff": test_the_topup_switch_restores_fill_only_on_empty,
    "onescale": test_the_row_is_ordered_by_the_number_it_shows,
    "unscored": test_an_unscored_chip_keeps_the_order_it_arrived_in,
}


def main(argv):
    keys = argv or list(TESTS)
    results = []
    for key in keys:
        fn = TESTS.get(key)
        if not fn:
            print(f"?? unknown test '{key}' (have: {', '.join(TESTS)})")
            continue
        print(f"\n> {key}: {fn.__doc__.splitlines()[0]}")
        try:
            fn()
            print(f"PASS {key}")
            results.append((key, True))
        except AssertionError as e:
            print(f"FAIL {key}: {e}")
            results.append((key, False))
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {key}: {type(e).__name__}: {e}")
            results.append((key, False))
    ok = sum(1 for _, p in results if p)
    print(f"\n==== {ok}/{len(results)} passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
