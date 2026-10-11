# last change in code by Jan Drozd (2026-09-03).
"""Unit tests for the two halves of "chips that do not repeat and are not junk".

Pure: no stack, no DB, no LLM — both functions under test take their inputs as
arguments. Run it inside the search image, like the other node tests:

    docker compose run --rm -v /opt/edu_ai/tests:/app/tests \
        --entrypoint python edu_ai_search tests/test_related_chips.py

  _covers_rare_concept   the admission gate: did a DISCRIMINATIVE concept match,
                         or only words every entry in the pack shares
  drop_repeats           the per-conversation dedupe: asked once, never offered
                         back; offered and ignored, demoted rather than binned

NOT covered here (needs a live stack): the log read behind drop_repeats — its
entity+caller scoping is the part that matters and it is exercised end to end.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.conversation import drop_repeats, unique_questions, _key  # noqa: E402
from app.nodes.qa_lexical import _covers_rare_concept, _qa_items  # noqa: E402

# "sikana" is in 2 of 100 entries, "skola" in 40 — the real shape of a school FAQ
_CONCEPTS = [{"sikana", "posmivat"}, {"skola"}, {"jak"}]
_DFS = [2, 40, 60]
_N = 100
_MAX_DF = 0.25


# ── the admission gate ────────────────────────────────────────
def test_a_rare_concept_admits():
    """An entry matching the rare word is admitted — that word IS the reason."""
    assert _covers_rare_concept(_CONCEPTS, {"sikana", "skola"}, _DFS, _N, _MAX_DF)


def test_generic_concepts_alone_do_not_admit():
    """Sharing only "škola" and "jak" is what the junk chips had in common.

    Measured 2026-09-03: "Jak se omlouvá absence?" was offered on "někdo se mi
    posmívá" at 0.3482 — the identical score as the RIGHT chip, which is why a
    higher floor could not tell them apart.
    """
    assert not _covers_rare_concept(_CONCEPTS, {"skola", "jak"}, _DFS, _N, _MAX_DF)


def test_a_missing_df_neither_admits_nor_blocks():
    """A concept with no corpus stats cannot argue either way, so it is skipped.

    Deliberately conservative: unknown rarity does not admit, because a chip is
    optional and an unprovable one is not worth a slot. It must not veto either.
    """
    # the only matched concept has no df — nothing proves it rare, so no admission
    assert not _covers_rare_concept(_CONCEPTS, {"sikana"}, [None, 40, 60], _N, _MAX_DF)
    # …but it does not stop a DIFFERENT rare concept from admitting
    assert _covers_rare_concept(_CONCEPTS, {"sikana", "skola"}, [None, 2, 60], _N, _MAX_DF)


# ── the per-conversation dedupe ───────────────────────────────
def _chips(*questions):
    return [{"kind": "qa", "entry_id": f"E{i}", "question": q}
            for i, q in enumerate(questions)]


def test_an_asked_question_is_never_offered_back():
    """The worst case in the report: a chip pointing at the previous turn."""
    out = drop_repeats(_chips("Kde najdu cvičné testy JPZ?", "Co dělat při nemoci?"),
                       asked={_key("kde najdu cvičné testy jpz?")}, offered=set())
    assert [c["question"] for c in out] == ["Co dělat při nemoci?"]


def test_an_offered_chip_is_demoted_not_dropped():
    """Ignoring a chip is not refusing it — "not yet" reads the same as "no"."""
    out = drop_repeats(_chips("Už nabídnutá?", "Nová?"),
                       asked=set(), offered={_key("Už nabídnutá?")})
    assert [c["question"] for c in out] == ["Nová?", "Už nabídnutá?"]


def test_a_list_of_only_stale_chips_survives():
    """Dropping them would turn "it repeats" into "it offers nothing".

    The gate above can already cut a list to one item; binning that one because it
    was shown last turn is the failure this ordering exists to prevent.
    """
    out = drop_repeats(_chips("Jediná"), asked=set(), offered={_key("Jediná")})
    assert [c["question"] for c in out] == ["Jediná"]


def test_clarify_is_left_alone():
    """Its `question` is the asking sentence, not a query — it cannot repeat."""
    items = [{"kind": "clarify", "question": "Vaše otázka může mířit do více oblastí.",
              "options": [{"label": "A", "question": "a?"}]}]
    assert drop_repeats(items, asked={_key(items[0]["question"])}, offered=set()) == items


def test_nothing_seen_is_a_noop():
    """A first turn, or any caller without a conversation_id, pays nothing."""
    items = _chips("A?", "B?")
    assert drop_repeats(items, asked=set(), offered=set()) == items


def test_matching_ignores_case_and_padding():
    """A typed question and the chip's own text must count as the same question."""
    out = drop_repeats(_chips("  KDE je jídelna? "),
                       asked={_key("Kde je jídelna?")}, offered=set())
    assert out == []


# ── one button per question ───────────────────────────────────
def test_the_same_question_under_two_entries_is_one_chip():
    """31 of 33 questions in provoz-zs also exist in provoz-org and provoz-sos.

    Reported from production 2026-09-04: two identical buttons reading "Jak požádat
    o individuální vzdělávací plán?", one the school's entry, one the organisation's.
    Entries arrive best-first, so the school's own is the one that survives.
    """
    items = _qa_items([("E-own", "Jak požádat o individuální vzdělávací plán?", 0.6),
                       ("E-org", "Jak požádat o individuální vzdělávací plán?", 0.6),
                       ("E-other", "Kdo schvaluje IVP?", 0.4)], 3)
    assert [i["entry_id"] for i in items] == ["E-own", "E-other"]


def test_the_cap_counts_what_survives_the_dedupe():
    """A duplicate must not eat a slot — two of three asked for, two distinct back."""
    items = _qa_items([("A", "Stejná?", 0.6), ("B", "Stejná?", 0.6),
                       ("C", "Jiná?", 0.5), ("D", "Třetí?", 0.4)], 2)
    assert [i["question"] for i in items] == ["Stejná?", "Jiná?"]


def test_the_boundary_dedupes_across_builders():
    """Each builder dedupes only within itself; they meet only at the response."""
    items = [{"kind": "qa", "question": "Jak požádat o IVP?"},
             {"kind": "other_grade", "question": "  jak POŽÁDAT o IVP? "},
             {"kind": "related", "question": "Něco jiného"}]
    assert [i["kind"] for i in unique_questions(items)] == ["qa", "related"]


TESTS = {
    "rare": test_a_rare_concept_admits,
    "generic": test_generic_concepts_alone_do_not_admit,
    "nodf": test_a_missing_df_neither_admits_nor_blocks,
    "asked": test_an_asked_question_is_never_offered_back,
    "demote": test_an_offered_chip_is_demoted_not_dropped,
    "stale": test_a_list_of_only_stale_chips_survives,
    "clarify": test_clarify_is_left_alone,
    "noop": test_nothing_seen_is_a_noop,
    "casefold": test_matching_ignores_case_and_padding,
    "twoentries": test_the_same_question_under_two_entries_is_one_chip,
    "cap": test_the_cap_counts_what_survives_the_dedupe,
    "boundary": test_the_boundary_dedupes_across_builders,
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
