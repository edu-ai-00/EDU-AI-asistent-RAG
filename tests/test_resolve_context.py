# last change in code by Jan Drozd (2026-08-03).
"""Unit tests for the resolve_context node (follow-up → standalone query).

Pure: no stack, no DB, no LLM. Imports the node module directly, so it needs the
search package importable — run it inside the search image:

    docker compose run --rm --entrypoint python edu_ai_search tests/test_resolve_context.py

The test_* functions are also pytest-compatible.

NOT covered here (needs a live stack with the multi-entity schema):
  - the entity-scoped turn lookup, incl. "a foreign conversation_id returns nothing"
  - whether a rewritten follow-up actually retrieves the right document
"""

import os
import sys

# the search package sits next to this directory inside the image (/app/app)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.nodes import resolve_context as rc  # noqa: E402
from app.nodes.resolve_context import _needs_context, resolve_context  # noqa: E402


# ── the heuristic that decides whether to spend an LLM call ───
def test_followups_need_context():
    """Short, conjunction-led or anaphoric queries cannot stand on their own."""
    for q in ("a kdy je termín?", "kdo o tom rozhoduje", "odvolání",
              "a co ten druhý termín", "musím to podepsat"):
        assert _needs_context(q), f"expected a follow-up: {q!r}"


def test_standalone_questions_are_left_alone():
    """A self-contained question must not cost a rewrite."""
    for q in ("jaké pomůcky potřebuje prvňáček do školy",
              "do kdy se můžu odvolat proti výsledku přijímaček",
              "kdy začínají jarní prázdniny v roce 2026"):
        assert not _needs_context(q), f"expected standalone: {q!r}"


def test_empty_question_is_not_a_followup():
    """An empty query has nothing to resolve — and must not reach the DB."""
    assert not _needs_context("")
    assert not _needs_context("   ")


# ── the guards that keep the node off the hot path ────────────
def test_disabled_is_a_noop():
    """enabled=false must return {} without touching the DB or the model."""
    state = {"user_question": "a kdy?", "conversation_id": "x"}
    assert resolve_context(state, {"context": {"enabled": False}}) == {}


def test_no_conversation_id_is_a_noop():
    """Without a conversation there is no history to load."""
    assert resolve_context({"user_question": "a kdy?"}, {}) == {}


def test_standalone_question_skips_the_lookup():
    """A self-contained question returns {} even when a conversation exists."""
    state = {"user_question": "jaké pomůcky potřebuje prvňáček do školy",
             "conversation_id": "00000000-0000-0000-0000-000000000001"}
    assert resolve_context(state, {}) == {}


# ── the exact-match probe (a short question can still be complete) ──
def _with_probe(result: bool):
    """Swap the DB probe for a stub; returns a restore callable."""
    original = rc._has_exact_variant
    rc._has_exact_variant = lambda question, cfg, state: result
    return lambda: setattr(rc, "_has_exact_variant", original)


def test_exact_match_skips_the_rewrite():
    """A ≤4-word query that IS a curated variant must not be rewritten.

    A suggestion button is a canonical_q verbatim, so rewriting it destroys the
    very match it was offered for.
    """
    restore = _with_probe(True)
    try:
        state = {"user_question": "V kolik začíná JPZ?",
                 "conversation_id": "00000000-0000-0000-0000-000000000001",
                 "legal_entity": "ANY_ENTITY"}
        out = resolve_context(state, {})
        assert "user_question" not in out, f"must not rewrite: {out}"
        assert out.get("context_skip_reason"), f"skip must say why: {out}"
    finally:
        restore()


def test_real_followup_still_rewrites():
    """With no exact match the node proceeds — the probe must not swallow follow-ups."""
    restore = _with_probe(False)
    try:
        seen = []
        state = {"user_question": "a hodiny?",
                 "conversation_id": "00000000-0000-0000-0000-000000000001",
                 "legal_entity": "ANY_ENTITY"}
        original_turns = rc._load_turns
        rc._load_turns = lambda *a, **k: seen.append(1) or []
        try:
            assert resolve_context(state, {}) == {}
        finally:
            rc._load_turns = original_turns
        assert seen, "the node must go on to load the conversation turns"
    finally:
        restore()


def test_probe_swallows_a_db_failure():
    """An unreachable DB must make the probe say 'no', never raise.

    Tests the guard inside _has_exact_variant itself: the caller has none, so a
    leaking exception here would cost the user their answer.
    """
    def boom(*a, **k):
        raise RuntimeError("db down")

    original = rc.get_cursor
    rc.get_cursor = boom
    try:
        assert rc._has_exact_variant("V kolik začíná JPZ?", {},
                                     {"legal_entity": "ANY_ENTITY"}) is False
    finally:
        rc.get_cursor = original


TESTS = {
    "followup": test_followups_need_context,
    "standalone": test_standalone_questions_are_left_alone,
    "empty": test_empty_question_is_not_a_followup,
    "disabled": test_disabled_is_a_noop,
    "noconv": test_no_conversation_id_is_a_noop,
    "skip": test_standalone_question_skips_the_lookup,
    "exact": test_exact_match_skips_the_rewrite,
    "rewrite": test_real_followup_still_rewrites,
    "probefail": test_probe_swallows_a_db_failure,
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
