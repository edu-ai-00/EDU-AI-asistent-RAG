# last change in code by Jan Drozd and Martin Safranek (2026-08-19).
"""Shared, process-lifetime OpenAI clients.

Constructing ChatOpenAI/OpenAIEmbeddings inside a node pays a fresh client
setup + TLS handshake on every request (~100–200 ms). These factories build
one client per model and reuse its pooled HTTP connection for the life of
the worker process.
"""

from functools import lru_cache

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from . import config


# Fallback when settings carry no answer.temperature. Low but not zero: the
# answers are grounded in retrieved text, so sampling adds nothing but noise.
_DEFAULT_TEMPERATURE = 0.1


@lru_cache(maxsize=8)
def get_embedder(model: str) -> OpenAIEmbeddings:
    return OpenAIEmbeddings(model=model, api_key=config.OPENAI_API_KEY)


@lru_cache(maxsize=8)
def get_chat(model: str, max_tokens: int | None = None,
             reasoning_effort: str | None = None,
             temperature: float | None = None) -> ChatOpenAI:
    """One cached client per (model, cap, effort, temperature).

    reasoning_effort is what makes the GPT-5.6 tier usable: its default effort
    spends minutes and reasoning tokens on a grounded 350-token answer. Passing
    it also means temperature must go, because those models reject it — and
    answer.py swallows LLM errors into no_answer, so that would fail silently.

    `temperature` joins the cache key, and that is the whole reason it is an
    argument rather than a module constant: the clients live for the life of the
    worker, so a temperature read from per-school settings and applied inside
    the factory would let whichever school called FIRST fix the temperature for
    every school afterwards.
    """
    kwargs: dict = {"model": model, "api_key": config.OPENAI_API_KEY}
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    else:
        kwargs["temperature"] = _DEFAULT_TEMPERATURE if temperature is None \
            else float(temperature)
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    return ChatOpenAI(**kwargs)
