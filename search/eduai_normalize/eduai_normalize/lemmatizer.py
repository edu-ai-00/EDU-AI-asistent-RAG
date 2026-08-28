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
                        normalize_tokens, require_lang)

logger = logging.getLogger(__name__)


@lru_cache(maxsize=100_000)
def lemma_word(word: str, lang: str = "") -> str:
    """Memoized simplemma lemma (diacritics-intact result; caller folds).

    simplemma is multilingual — `lang` selects the dictionary (cs + en dicts
    ship in both images)."""
    from simplemma import lemmatize
    wl = word.lower()
    try:
        return lemmatize(wl, lang=lang)
    except Exception:
        return wl


def lemma_tokens(text: str, cfg: dict | None = None, lang: str | None = None) -> list[str]:
    """Pure simplemma LEMMA per word (no stemmer). Falls back to surface on error."""
    lang = require_lang(lang)
    lc, strip, drop_sw = surface_cfg(cfg)
    stops = stopwords_of(cfg, lang)
    try:
        out = [fold(lemma_word(w, lang), lc, strip) for w in tokenize(text)]
    except Exception as e:
        logger.warning("lemma_tokens fell back to norm tokens: %s", e)
        return normalize_tokens(text, cfg, lang)
    return drop(out, drop_sw, stops)
