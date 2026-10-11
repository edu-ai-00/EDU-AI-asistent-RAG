# last change in code by Jan Drozd (2026-09-03).
"""Unit tests for generate_variants — the LLM call on the no-answer path.

Pure: no stack, no DB, no LLM. get_chat and the prompt loader are stubbed, so
what is asserted is which parameters the node resolves out of the settings
block. Run it inside the search image:

    docker compose run --rm --entrypoint python edu_ai_search tests/test_llm_variants.py

The test_* functions are also pytest-compatible.

NOT covered here: whether the model's variants are any good (that is the eval
set), and the latency of a reasoning tier at its default effort.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.nodes import answer as a  # noqa: E402

# what prod runs on the answer step (migration 156 + 203 + this one)
PARAMS = {"model": "gpt-5.6-luna", "max_tokens": 2000, "temperature": 0,
          "reasoning_effort": "medium"}
STATE = {"user_question": "Kdy začíná nábor do družiny?", "query_language": "cs"}


class _Stub:
    """Records the get_chat arguments, answers with the no-answer protocol."""

    def __init__(self):
        self.calls = []

    def get_chat(self, model, max_tokens=None, reasoning_effort=None,
                 temperature=None):
        self.calls.append({"model": model, "max_tokens": max_tokens,
                           "reasoning_effort": reasoning_effort,
                           "temperature": temperature})
        return self

    def invoke(self, _messages):
        return type("R", (), {"content": "NO_ANSWER\nVARIANT: Kdy je zápis do "
                                         "družiny?\nVARIANT: Jak přihlásit "
                                         "dítě do družiny?"})()


# Which curated entry each rephrasing lands on, and what that entry is called.
# The real probe runs prepare -> route -> qa_exact -> qa_lexical against the
# database; here it is a lookup, so these tests keep asserting what they are
# about — which parameters come out of the settings block.
PROBE_HITS = {"Kdy je zápis do družiny?": "E-druzina"}
ENTRY_Q = {"E-druzina": "Jak se dítě přihlašuje do školní družiny?"}


def _patch(get_chat):
    """Swap the things that would leave the process — returns the undo."""
    orig = (a.get_chat, a._load_system_prompt, a.language_name,
            a._probe_variant, a.get_cursor)
    a.get_chat = get_chat
    a._load_system_prompt = lambda: "SYSTEM"
    a.language_name = lambda _code: "Czech"
    a._probe_variant = lambda _state, variant: PROBE_HITS.get(variant)

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def execute(self, _sql, params):
            self._rows = [(e, ENTRY_Q[e]) for e in params[0] if e in ENTRY_Q]

        def fetchall(self):
            return self._rows

    a.get_cursor = lambda: _Cur()

    def undo():
        (a.get_chat, a._load_system_prompt, a.language_name,
         a._probe_variant, a.get_cursor) = orig

    return undo


def _run(params, state=None):
    stub = _Stub()
    undo = _patch(stub.get_chat)
    try:
        out = a.generate_variants(state or STATE, params)
    finally:
        undo()
    return out, stub.calls


# ── the switch ────────────────────────────────────────────────
def test_disabled_spends_no_call():
    """enabled:false must return before the LLM, not just drop the result."""
    out, calls = _run({**PARAMS, "llm_variants": {"enabled": False}})
    assert out == [], out
    assert calls == [], calls


def test_enabled_by_default_without_a_block():
    """No llm_variants key at all — the historical behaviour is on."""
    out, calls = _run(PARAMS)
    assert len(calls) == 1
    # Only the rephrasing that reached a curated entry survives, and it is offered
    # as THAT ENTRY'S question — the model's wording never reaches the user, since
    # a chip promising one thing and answering another is the reported harm.
    assert [s["kind"] for s in out] == ["qa"]
    assert out[0]["question"] == "Jak se dítě přihlašuje do školní družiny?"
    assert out[0]["entry_id"] == "E-druzina"


def test_a_rephrasing_that_reaches_nothing_is_not_offered():
    """The probe is the whole point: an unverified variant led users in circles."""
    _saved = dict(PROBE_HITS)
    PROBE_HITS.clear()
    try:
        out, calls = _run(PARAMS)
        assert len(calls) == 1 and out == []
    finally:
        PROBE_HITS.update(_saved)


# ── what the four inheriting keys resolve to ──────────────────
def test_defaults_inherit_the_answer_step():
    """Empty block: model, effort and temperature come from the answer step,
    the output cap does NOT (200, not the step's 2000)."""
    _out, calls = _run({**PARAMS, "llm_variants": {"enabled": True}})
    assert calls[0] == {"model": "gpt-5.6-luna", "max_tokens": 200,
                        "reasoning_effort": "medium", "temperature": 0}


def test_the_block_overrides_every_key():
    """A cheaper model for suggestions, decoupled from the answer."""
    _out, calls = _run({**PARAMS, "llm_variants": {
        "enabled": True, "model": "gpt-4.1-mini", "max_tokens": 120,
        "temperature": 0.3, "reasoning_effort": "low"}})
    assert calls[0] == {"model": "gpt-4.1-mini", "max_tokens": 120,
                        "reasoning_effort": "low", "temperature": 0.3}


def test_empty_effort_drops_it_so_temperature_survives():
    """llm.py drops temperature whenever an effort is passed, so an explicitly
    empty effort is the way to get a temperature-driven call back."""
    _out, calls = _run({**PARAMS, "llm_variants": {"reasoning_effort": ""}})
    assert calls[0]["reasoning_effort"] is None
    assert calls[0]["temperature"] == 0


# ── failure is never a condition ──────────────────────────────
def test_a_broken_block_does_not_disable_the_call():
    """A scalar where an object belongs must not silently turn the call off."""
    _out, calls = _run({**PARAMS, "llm_variants": True})
    assert len(calls) == 1


def test_an_llm_failure_returns_no_variants():
    """Any exception is an empty offer, never a raised request."""
    def boom(*_args, **_kw):
        raise RuntimeError("openai down")

    undo = _patch(boom)
    try:
        assert a.generate_variants(STATE, PARAMS) == []
    finally:
        undo()


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'FAILED' if fails else 'OK'} — {fails} failure(s)")
    sys.exit(1 if fails else 0)
