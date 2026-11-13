"""Unit tests for the answer step without a chat model (search/app/nodes/answer.py, `skip_without_key`).

Pure: no DB, no model, no network (the generation and the gateway are stubbed). Run inside the
search image:

    docker exec -i edu_ai_search python - < tests/test_answer_without_key.py

What it protects: "no model" means no real OPENAI_API_KEY AND a gateway (Bifrost, which holds the
provider key) that lists no model. The production setup — placeholder key, Bifrost with models —
must keep generating. With `skip_without_key` and no model the step ends like an empty passage pool
(the parked offer, else no_answer); a gateway that cannot be asked is a fault, not a missing key.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, llm  # noqa: E402
from app.nodes import answer as an  # noqa: E402

GATEWAY = "http://bifrost:8080/openai/v1"


class _Called(Exception):
    pass


def _with(key, fn, base=None, models=None, probes=None):
    """Run fn with OPENAI_API_KEY=key, OPENAI_BASE_URL=base and a gateway listing `models`."""
    old_key, old_base = config.OPENAI_API_KEY, os.environ.get("OPENAI_BASE_URL")
    old_probe, old_prep, old_unans = llm._gateway_models, an.prepare_generation, an.resolve_unanswered
    config.OPENAI_API_KEY = key
    if base is None:
        os.environ.pop("OPENAI_BASE_URL", None)
    else:
        os.environ["OPENAI_BASE_URL"] = base

    def probe(url):
        if probes is not None:
            probes.append(url)
        return models
    llm._gateway_models = probe
    llm._gateway.update(at=float("-inf"), ok=True)

    def prep(*a, **k):
        raise _Called("generation prepared")
    an.prepare_generation = prep
    an.resolve_unanswered = lambda state: {"resolved": True, "outcome": "no_answer",
                                           "suggestions": state.get("fallback_suggestions") or []}
    try:
        return fn()
    finally:
        config.OPENAI_API_KEY = old_key
        if old_base is None:
            os.environ.pop("OPENAI_BASE_URL", None)
        else:
            os.environ["OPENAI_BASE_URL"] = old_base
        llm._gateway_models, an.prepare_generation, an.resolve_unanswered = old_probe, old_prep, old_unans
        llm._gateway.update(at=float("-inf"), ok=True)


STATE = {"user_question": "q", "fallback_suggestions": [{"kind": "qa", "entry_id": "e1", "question": "Q?"}]}


def _generates(key, **kw):
    try:
        _with(key, lambda: an.answer(dict(STATE), {"skip_without_key": True}), **kw)
    except _Called:
        return True
    return False


def test_without_a_gateway_placeholder_and_empty_mean_no_model():
    for key, want in (("", False), ("  ", False), ("CHANGE_ME", False), ("CHANGE_ME_please", False),
                      ("sk-real", True)):
        assert _with(key, llm.chat_configured) is want, key


def test_a_real_key_never_asks_the_gateway():
    probes = []
    assert _with("sk-real", llm.chat_configured, base=GATEWAY, models=0, probes=probes) is True
    assert probes == []


def test_gateway_decides_for_a_placeholder_key():
    assert _with("CHANGE_ME_openai_api_key", llm.chat_configured, base=GATEWAY, models=98) is True
    assert _with("CHANGE_ME_openai_api_key", llm.chat_configured, base=GATEWAY, models=0) is False
    # cannot be asked: a fault, so the generation is tried and reports llm_unavailable
    assert _with("CHANGE_ME_openai_api_key", llm.chat_configured, base=GATEWAY, models=None) is True


def test_gateway_answer_is_cached():
    probes = []

    def twice():
        return llm.chat_configured(), llm.chat_configured()
    assert _with("CHANGE_ME", twice, base=GATEWAY, models=0, probes=probes) == (False, False)
    assert probes == [GATEWAY]


def test_production_setup_keeps_generating():
    # placeholder in .env, provider key in Bifrost: the generation must run
    assert _generates("CHANGE_ME_openai_api_key", base=GATEWAY, models=98)


def test_no_model_and_on_skips_the_generation():
    out = _with("CHANGE_ME", lambda: an.answer(dict(STATE), {"skip_without_key": True}),
                base=GATEWAY, models=0)
    assert out["outcome"] == "no_answer" and out["suggestions"][0]["entry_id"] == "e1"
    assert not _generates("CHANGE_ME")                      # no key, no gateway


def test_no_model_and_off_still_tries_the_generation():
    try:
        _with("CHANGE_ME", lambda: an.answer(dict(STATE), {}), base=GATEWAY, models=0)
    except _Called:
        return
    raise AssertionError("the generation was not prepared with the parameter off")


def test_key_and_on_still_generates():
    assert _generates("sk-real")


def test_resolved_state_is_left_alone():
    assert _with("CHANGE_ME", lambda: an.answer({"resolved": True}, {"skip_without_key": True})) == {}


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"Ran {n} tests")
    print("OK")
