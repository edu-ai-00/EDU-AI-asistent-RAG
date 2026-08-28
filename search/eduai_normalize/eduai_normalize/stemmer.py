# last change in code by Martin Safranek (2026-08-02).
"""Stem form — Dolamic & Savoy LIGHT Czech stemmer (= Lucene CzechStemmer, pure
Python, no JVM). Rules operate on the diacritics-intact lowercased word (they
reference ě/í/ů/č/š/ž + palatalisation), so the fold to ASCII happens AFTER.

The rule set is intentionally UNCHANGED from the original library code (see
feedback_config_data_placement — we don't touch the stemmer algorithm); this file
only relocates it and adds per-word memoization. Keep output-identical to the
stem branch of the ChunkLemmatizer ingestion component (code in
langflow_flows/ingestion/*.json) (query↔index parity).
"""

from __future__ import annotations

from functools import lru_cache

from .normalize import (tokenize, fold, surface_cfg, stopwords_of, stem_min_len,
                        drop, require_lang, engine_name)


def _palatalise(w: str) -> str:
    if w[-2:] in ("ci", "ce", "či", "če"):
        return w[:-2] + "k"
    if w[-2:] in ("zi", "ze", "ži", "že"):
        return w[:-2] + "h"
    if w[-3:] in ("čtě", "čti", "čtí"):
        return w[:-3] + "ck"
    if w[-3:] in ("ště", "šti", "ští"):
        return w[:-3] + "sk"
    return w[:-1]


def _remove_case(w: str) -> str:
    if len(w) > 7 and w.endswith("atech"):
        return w[:-5]
    if len(w) > 6:
        if w.endswith("ětem"):
            return _palatalise(w[:-3])
        if w.endswith("atům"):
            return w[:-4]
    if len(w) > 5:
        if w[-3:] in ("ech", "ich", "ích", "ého", "ěmi", "emi", "ému",
                      "ěte", "ěti", "iho", "ího", "ěmu", "imu"):
            return _palatalise(w[:-2])
        if w[-3:] in ("ách", "ata", "aty", "ých", "ama", "ami", "ové", "ovi", "ými"):
            return w[:-3]
    if len(w) > 4:
        if w.endswith("em"):
            return _palatalise(w[:-1])
        if w[-2:] in ("es", "ém", "ím"):
            return _palatalise(w[:-2])
        if w[-2:] in ("ům", "at", "ám", "os", "us", "ým", "mi", "ou"):
            return w[:-2]
    if len(w) > 3:
        if w[-1] in "eiíě":
            return _palatalise(w)
        if w[-1] in "uyůaoáéý":
            return w[:-1]
    return w


def _remove_possessives(w: str) -> str:
    if len(w) > 5:
        if w[-2:] in ("ov", "ův"):
            return w[:-2]
        if w.endswith("in"):
            return _palatalise(w[:-1])
    return w


def cz_stem_light(word: str, min_len: int = 3) -> str:
    """Light Czech stemmer (Dolamic & Savoy). Input: lowercased, diacritics-intact."""
    if len(word) < min_len:
        return word
    return _remove_possessives(_remove_case(word))


_SNOWBALL: dict[str, object] = {}


def _snowball(snowball_lang: str):
    """Snowball stemmer per language — one cached instance each, lazy import.

    snowballstemmer ships ~30 languages, so a new language is a NAME, not code.
    """
    st = _SNOWBALL.get(snowball_lang)
    if st is None:
        import snowballstemmer
        st = _SNOWBALL[snowball_lang] = snowballstemmer.stemmer(snowball_lang)
    return st


# Implementation registry — keyed by IMPLEMENTATION, never by language. The
# language→implementation mapping is DATA: settings.config.engines already
# declares {"kind":"stemmer","name":"snowball_english","language":"en"} and
# {"kind":"stemmer","name":"dolamic_savoy_light","language":"cs"}. The code used
# to hardcode `if lang == "en"` next to that config, i.e. the same fact stored
# twice — the duplication that lets one copy drift from the other.
#
# `snowball_<language>` is parsed, so German/French/Polish need only a config
# row. Only a genuinely new ALGORITHM (a hand-written light stemmer for another
# language, as `dolamic_savoy_light` is for Czech) needs code here.
_STEM_IMPL = {
    "dolamic_savoy_light": lambda wl, ml: cz_stem_light(wl, ml),
}


def _resolve_stemmer(name: str):
    """Callable (word, min_len) -> stem for an engine name, or None if unknown."""
    impl = _STEM_IMPL.get(name)
    if impl is not None:
        return impl
    if name.startswith("snowball_"):
        sb = name[len("snowball_"):]

        def _stem(wl, ml, _sb=sb):
            if len(wl) < ml:
                return wl
            try:
                return _snowball(_sb).stemWord(wl)
            except Exception:
                return wl
        return _stem
    return None


@lru_cache(maxsize=100_000)
def stem_word(word: str, min_len: int = 3, lang: str = "cs", engine: str = "") -> str:
    """Memoized per-word stem (diacritics-intact result; caller folds). Cheap but
    the cache pays off across a query's + a batch's repeated vocabulary.

    `engine` is the settings engine name for this language, resolved by
    stem_tokens. It is part of the cache key on purpose: switching a language's
    stemmer in the config must not serve stems produced by the previous one.
    """
    wl = word.lower()
    impl = _resolve_stemmer(engine) if engine else None
    if impl is None:
        # No engine declared for this language. Not silently Czech-stemmed —
        # that is how an English chunk ended up with Czech stems (2026-07-29).
        # The surface form still matches the norm branch, so retrieval degrades
        # instead of breaking, and the missing engine is reported by the
        # readiness check.
        return wl
    return impl(wl, min_len)


def stem_tokens(text: str, cfg: dict | None = None, lang: str | None = None) -> list[str]:
    lang = require_lang(lang)
    lc, strip, drop_sw = surface_cfg(cfg)
    stops = stopwords_of(cfg, lang)
    ml = stem_min_len(cfg, lang)
    engine = engine_name(cfg, "stemmer", lang)
    return drop([fold(stem_word(w, ml, lang, engine), lc, strip) for w in tokenize(text)],
                drop_sw, stops)
