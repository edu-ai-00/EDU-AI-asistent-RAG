# last change in code by Martin Safranek (2026-08-02).
"""eduai_normalize — the ONE shared CS+EN normalization / stemmer / lemmatizer.

Used by BOTH sides of the pipeline so query↔index parity is structural (not a
hand-synced copy):
  * search service (query side)  — search/app/text_norm.py re-exports from here
  * ingestion (index side)       — the Langflow ChunkLemmatizer component imports
                                    `build_forms` from here

Three PURE per-word forms, config-driven by the active settings.config:
    normalize   tokenize + fold (surface) + stopwords
    stemmer     Dolamic-Savoy light stem
    lemmatizer  simplemma lemma
Order matters: reduce on the diacritics-intact word, ASCII-fold LAST (simplemma's
dictionary and the CzechStemmer rules both need the diacritics).

The canonical output is pinned by the parity vectors in
contracts/parity/<lang>-normalization-vectors.json — regenerate + both
suites consume them on any change here, and check_vectors.py must pass before
AND after (drift there means the stored index needs re-ingesting).
"""

from .normalize import (  # noqa: F401
    normalize_tokens, detect_lang, langdetect_scores, fold, tokenize,
    strip_diacritics, surface_cfg, stopwords_of, stem_min_len, drop,
    require_lang, engine_for, engine_name,
    DIACRITICS_RESTORE, diacritic_candidates, warm_diacritics,
)
from .stemmer import stem_tokens, cz_stem_light, stem_word  # noqa: F401
from .lemmatizer import lemma_tokens, lemma_word, NEGATION_PREFIX  # noqa: F401

# In step with pyproject on purpose. This said 1.5.0 while pyproject said
# 2.2.0, so the one number a RUNNING process can read was three releases
# stale -- and the point of the version is that different code must not
# appear under the same one. Nothing reads it today, which is exactly why
# it drifted (found 2026-09-04).
# 3.0.0 BREAKING (2026-09-05): both in-code fallback tables are gone.
#   STOPWORDS / STOPWORDS_EN  -> stopwords_of() raises without stopwords_by_lang
#   _DEFAULT_ENGINE           -> engine_name() raises without engines
# Both HAPPENED to equal the configuration for cs/en, so a caller that omitted
# cfg looked correct, and both were absent for de/es, so that caller silently
# dropped nothing and stemmed nothing. Language->reducer and the stoplist are
# DATA (settings.config.engines, edu_ai.qa_stopword); the IMPLEMENTATIONS stay
# in code (_STEMMERS, snowball_<lang>) — that is the line.
# 3.1.0 (2026-10-21): lemma_word keeps a Czech negation ("nesmím" → nesmět,
#   was smět). The lemma of every negated word changes, so an index built with
#   3.0.0 must be re-ingested (QA packs and documents) to match 3.1.0 queries.
# 3.2.0 (2026-10-21): the lemma and stem branches judge a stop-word by its
#   SURFACE form before reducing (normalize.content_words) — "jsou" no longer
#   leaks as the stem `jso`, "během" no longer collides with "běh". Stem and
#   lemma tokens change, so an index built before must be re-ingested.
# 3.3.0 (2026-10-21): a Czech word typed without diacritics that the
#   dictionary does not know is lemmatized through its forms with them
#   ("zacina" → začínat; its spellings with diacritics are generated and looked
#   up in the dictionary), and the dictionary vote of detect_lang counts it.
#   Lemmas of such words change, on the index side too (re-ingest).
__version__ = "3.3.0"


def build_forms(s: str, cfg: dict | None = None, lang: str | None = None) -> dict[str, list[str]]:
    """All three pure forms at once: {'norm': [...], 'lemma': [...], 'stem': [...]}.

    `lang` = číselník language code: selects the simplemma dictionary, the
    stemmer and the stoplist. REQUIRED — there is no default. It used to default
    to "cs", so a caller that forgot it silently reduced any language as Czech,
    and nothing in the output revealed which reducer had run. Which reducer
    implements a language is DATA (settings.config.engines), resolved via
    normalize.engine_name."""
    return {
        "norm": normalize_tokens(s, cfg, lang),
        "lemma": lemma_tokens(s, cfg, lang),
        "stem": stem_tokens(s, cfg, lang),
    }


__all__ = [
    "build_forms",
    "normalize_tokens", "lemma_tokens", "stem_tokens",
    "detect_lang", "langdetect_scores", "fold", "tokenize", "strip_diacritics",
    "cz_stem_light", "stem_word", "lemma_word", "NEGATION_PREFIX",
    "surface_cfg", "stopwords_of", "stem_min_len", "drop",
    "require_lang", "engine_for", "engine_name",
    "DIACRITICS_RESTORE", "diacritic_candidates", "warm_diacritics",
]
