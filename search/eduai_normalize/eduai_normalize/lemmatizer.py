# last change in code by Martin Safranek (2026-07-29).
"""Lemma form — simplemma dictionary lemmatizer. The dictionary is keyed on real
Czech words WITH diacritics (`přihláška`), so lemmatization runs on the
diacritics-intact word and the fold to ASCII happens AFTER (see
project_normalization_order_deviation).

Per-word memoized: a lemma is deterministic per surface word, and queries +
ingestion batches repeat vocabulary heavily. Keep output-identical to the lemma
branch of the ChunkLemmatizer ingestion component (code in
langflow_flows/ingestion/*.json) (query↔index parity).
"""

from __future__ import annotations

import logging
from functools import lru_cache

from .normalize import (tokenize, fold, surface_cfg, stopwords_of, drop,
                        normalize_tokens, require_lang, content_words)

logger = logging.getLogger(__name__)


# Negation as a word PREFIX, per language. Czech negates a verb or adjective by
# "ne-" and simplemma 2.0.0 drops it: "nesmím" and "smím" both became `smět`, so
# a question asked in the negative matched — exactly, as qa_exact — the curated
# answer to the positive one (measured 2026-10-21 with tools/lab/variants: 39 of
# 39 flipped JPZ questions). English/German/Spanish negate with a separate word,
# which the lemmatizer does not touch. The implementation of a language lives
# in code like its stemmer; route.py's guard reads the same table.
NEGATION_PREFIX: dict[str, tuple[str, ...]] = {"cs": ("ne",), "de": (), "en": (), "es": ()}

# Negated forms the prefix test cannot recognise: "není" minus "ne" is the
# pronoun "ní", not a form of "být". Lowercase surface → negated lemma.
_NEGATION_EXCEPTIONS: dict[str, dict[str, str]] = {"cs": {"není": "nebýt"}}


@lru_cache(maxsize=100_000)
def _base_lemma(wl: str, lang: str) -> str:
    from simplemma import lemmatize
    try:
        return lemmatize(wl, lang=lang)
    except Exception:
        return wl


@lru_cache(maxsize=100_000)
def lemma_word(word: str, lang: str = "") -> str:
    """Memoized simplemma lemma (diacritics-intact result; caller folds).

    simplemma is multilingual — `lang` selects the dictionary (cs + en dicts
    ship in both images).

    A negation the library dropped is put back: when the word carries the
    language's negation prefix, its lemma does not, and the word WITHOUT the
    prefix reduces to that same lemma, the prefix is negation and stays —
    "nesmím" → nesmět, "nejde" → nejít. The second test is what keeps words
    that merely start with the letters out: "nejlepší" (superlative; "jlepší"
    is no word), "neděle" and "nemocný" (the library keeps them whole)."""
    wl = word.lower()
    lemma = _base_lemma(wl, lang)
    exc = _NEGATION_EXCEPTIONS.get(lang, {}).get(wl)
    if exc:
        return exc
    for p in NEGATION_PREFIX.get(lang, ()):
        if (len(wl) > len(p) + 1 and wl.startswith(p)
                and not lemma.lower().startswith(p)
                and fold(_base_lemma(wl[len(p):], lang)) == fold(lemma)):
            return p + lemma
    return lemma


def lemma_tokens(text: str, cfg: dict | None = None, lang: str | None = None) -> list[str]:
    """Pure simplemma LEMMA per word (no stemmer). Falls back to surface on error."""
    lang = require_lang(lang)
    lc, strip, drop_sw = surface_cfg(cfg)
    stops = stopwords_of(cfg, lang)
    try:
        out = [fold(lemma_word(w, lang), lc, strip) for w in content_words(text, cfg, lang)]
    except Exception as e:
        logger.warning("lemma_tokens fell back to norm tokens: %s", e)
        return normalize_tokens(text, cfg, lang)
    return drop(out, drop_sw, stops)
