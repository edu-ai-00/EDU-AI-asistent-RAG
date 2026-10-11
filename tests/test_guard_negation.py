# last change in code by Jan Drozd (2026-09-03).
"""Unit tests for the guard's phrase matching around NEGATION.

Pure: no stack, no DB — exercises token_forms/_phrase_hit directly. Run inside
the search image, which is where the app package lives:

    docker exec -i edu_ai_search python - < tests/test_guard_negation.py

The test_* functions are also pytest-compatible.

Why this file exists: simplemma 2.0.0 lemmatizes "nechci" to "chtit", dropping
the negation, so the self_harm rule "nechci žít" matched "chci žít" and "chci
žít zdravě" — a pupil asking how to live healthily got the crisis reply.
simplemma 1.2.0 gave "nechtít" and the bridge did not exist, which is why the
guard cannot delegate this to the library. Measured on production 2026-09-03.

NOT covered here: the phrase rules whose second token is a stopword ("zabít se",
"skočit z", "podřezat si") collapse to one common verb and over-match
independently of negation — a separate defect, deliberately not asserted away.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.nodes.route import _PHRASE_SLACK, _phrase_hit, token_forms  # noqa: E402

# The stoplist AND the engines are DATA since eduai_normalize 3.0.0 — there
# are no built-in tables and stopwords_of()/engine_name() raise without them. These tests stay pure (no DB), so they
# read the same snapshot the parity vectors use.
def _cfg() -> dict:
    _p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "contracts", "parity", "stoplist.json")
    with open(_p, encoding="utf-8") as fh:
        _d = json.load(fh)
        return {"normalization": _d["normalization"], "engines": _d["engines"]}


CFG = _cfg()

LANG = "cs"


def _hits(rule: str, query: str) -> bool:
    return _phrase_hit(token_forms(rule, LANG, CFG), token_forms(query, LANG, CFG),
                       _PHRASE_SLACK)


def test_negated_rule_still_matches_itself():
    """The whole point of a rule is to fire on its own wording."""
    assert _hits("nechci žít", "nechci žít")
    assert _hits("nechci žít", "už nechci žít, je to na nic")


def test_negated_rule_does_not_match_the_opposite():
    """"chci žít" is the OPPOSITE of the rule and must not trip it."""
    for query in ("chci žít", "chci žít zdravě", "jak chci žít"):
        assert not _hits("nechci žít", query), f"false positive: {query!r}"


def test_negating_the_query_does_not_trip_a_positive_rule():
    """The reverse direction: "nechci umřít" must not match "chci umřít"."""
    assert _hits("chci umřít", "chci umřít")
    assert not _hits("chci umřít", "nechci umřít")


def test_lemma_kept_when_negation_survives():
    """The fix must only drop a lemma that LOST the negation.

    "nepřítomnost" keeps its "ne" under 2.0.0, so its lemma stays in the set and
    inflections still match — otherwise this would quietly narrow every rule
    whose word merely begins with "ne".
    """
    forms = token_forms("nepřítomnost", LANG, CFG)
    assert len(forms) == 1
    assert any(f.startswith("ne") for f in forms[0]), forms
    assert _hits("nepřítomnost žáka", "nepřítomnosti žáka")


TESTS = {
    "self": test_negated_rule_still_matches_itself,
    "opposite": test_negated_rule_does_not_match_the_opposite,
    "reverse": test_negating_the_query_does_not_trip_a_positive_rule,
    "kept": test_lemma_kept_when_negation_survives,
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
