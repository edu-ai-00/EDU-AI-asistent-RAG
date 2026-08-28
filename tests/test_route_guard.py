# last change in code by Jan Drozd (2026-08-11).
"""Unit tests for the route guard's blocklist phrase matching.

Pure: no stack, no DB, no LLM — the phrase matcher is fed blocklist terms
directly, so this covers the matching logic, not the rows in qa_blocklist.
Run it inside the search image:

    docker compose run --rm --entrypoint python edu_ai_search tests/test_route_guard.py

Every FAILING case below was a live production answer on 2026-08-04: a child
writing "chtěl bych se zabít" got three paragraphs about the DiPSy application
system instead of Linka bezpečí.

NOT covered here (needs the DB): that qa_blocklist actually holds these terms.
The 17 self_harm phrases seeded by init_clean are what this matcher revives —
before it, no multi-word row could fire at all. Widening that list is separate.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.nodes.route import token_forms, _phrase_hit  # noqa: E402


def _hit(term: str, question: str, lang: str = "cs") -> bool:
    return _phrase_hit(token_forms(term, lang), token_forms(question, lang))


# ── what the old substring matcher already caught (must not regress) ──
def test_literal_phrases_still_match():
    assert _hit("nechci žít", "nechci žít")
    assert _hit("nechci žít", "Nechci už žít")
    assert _hit("chci umřít", "Chci umřít, co mám dělat?")


# ── the live misses: inflection and word order ──
def test_reflexive_order_is_ignored():
    """"zabít se" must fire on "se ... zabít" — Czech reorders the clitic."""
    assert _hit("zabít se", "Chtěl bych se zabít")
    assert _hit("zabít se", "Přemýšlím o tom se zabít")


def test_inflected_verb_matches_its_infinitive_term():
    """"ublížit si" vs "ublížím" — different simplemma lemmas, same verb."""
    assert _hit("ublížit si", "Přemýšlím, že si ublížím")
    assert _hit("řezat se", "Někdy se řežu, když je mi zle")


def test_intervening_words_within_the_window():
    assert _hit("chci to skončit", "Nemá to cenu, chci to skončit")
    assert _hit("nechce se mi žít", "Nechce se mi žít, přijde mi to bez smyslu")


# ── precision: the reflexive is what separates these ──
def test_phrase_without_its_reflexive_does_not_fire():
    """"zabít se" must not fire on "zabít čas" — that is why "se" is kept."""
    assert not _hit("zabít se", "Jak zabít čas o přestávce?")
    assert not _hit("ublížit si", "Kdo ublížil spolužákovi, jde k řediteli?")


def test_ordinary_school_questions_stay_clean():
    for q in ("Kdy jsou přijímací zkoušky na střední školu?",
              "Do kdy musím podat přihlášku?",
              "Můžu mít ve škole mobil?",
              "Jaká jsou pravidla pro omlouvání absence?",
              "Kolik přihlášek můžu podat v prvním kole?"):
        for term in ("zabít se", "nechci žít", "ublížit si", "chci umřít",
                     "chci to skončit", "skočit z"):
            assert not _hit(term, q), f"{term!r} false-positived on {q!r}"


def test_window_does_not_span_the_whole_query():
    """Tokens scattered across unrelated clauses are not one phrase."""
    assert not _hit(
        "chci to skončit",
        "Chci se zeptat, jestli to bude v dubnu, a kdy má škola skončit v červnu",
    )


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as e:
                failed += 1
                print(f"  FAIL {name}: {e}")
    print("FAILED" if failed else "all guard tests passed")
    sys.exit(1 if failed else 0)
