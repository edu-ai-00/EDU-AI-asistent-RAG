# last change in code by Martin Safranek (2026-08-02).
"""Base normalization (RAG redesign §2.2) — the surface layer the reducers build on.

Split out of the old monolithic text_norm.py so each concern is its own file:
    normalize.py   this — tokenize + fold (surface) + stopwords, config-driven
    stemmer.py     Dolamic-Savoy light stem (needs diacritics → folds AFTER)
    lemmatizer.py  simplemma lemma        (needs diacritics → folds AFTER)

Order (important, see project_normalization_order_deviation): tokenize FIRST,
then per word the reducer runs on the diacritics-intact word, and `fold` (strip
diacritics + ASCII) is the LAST step — because simplemma's dictionary and the
CzechStemmer rules both need the diacritics. `cfg` is the active
settings.config; None → v1 defaults. Must stay output-identical to
the ChunkLemmatizer ingestion component (code in
langflow_flows/ingestion/*.json) (query↔index parity).
"""

from __future__ import annotations

import re
import unicodedata

_ASCII = re.compile(r"[a-z0-9]+")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)

# ── deterministic language detection (shared by BOTH sides so index and query
#    resolve the same language for the same text) ──
# PRIMARY detector = langdetect (Nakatani/Google port, 55 languages, character
# n-gram Bayesian). It is NOT deterministic out of the box — detector.py runs
# n_trial=7 passes, perturbs its smoothing alpha with random.gauss and samples
# n-grams with random.choice, seeded from system entropy because
# DetectorFactory.seed defaults to None. Long text converges anyway (many
# n-grams), short text can flip. Pinning the seed HERE, once, at import, is what
# makes it usable for us at all: index and query must resolve the same text to
# the same language or the stored tokens stop matching the query's.
#
# It replaced a hand-written marker heuristic: two function-word sets plus an
# unconditional "+2 per diacritic" credited to Czech. That heuristic only had
# data for two languages, so any third language could only LOSE at that stage,
# and a single shared function word decided it: measured 2026-07-29, "Das war in
# Ordnung" and "Was ist das" came back as English because "in"/"was" were in the
# English set. Over 21 labelled cases the heuristic made 4 confidently WRONG
# calls; langdetect made 0 (it abstains instead), which matters because a wrong
# call silently picks the wrong stemmer while an abstention triggers the
# documented fallback chain and is recorded in langdetect_result_score.
_LANGDETECT_SEED = 0
_LANGDETECT_MIN_PROB = 0.70   # measured: 0 errors at any threshold; 0.90 loses
                              # correct short calls like "Anmeldung" (0.71)
# ALL-CAPS defeats langdetect on short text: its n-gram profiles are built from
# lowercase, so a Czech heading in capitals loses its diacritics-plus-case
# signature and comes back German. Measured over 1348 production chunks:
# lowercasing below this length fixes 7 (5x de->cs on Czech headings such as
# "HODNOCENÍ NA VYSVĚDČENÍCH…", 2x None->cs) and breaks nothing. Lowercasing
# UNCONDITIONALLY breaks 2 (en->de on a longer mixed passage), which is why this
# is a length bound and not a plain .lower().
_LANGDETECT_LOWER_BELOW = 200


def langdetect_scores(text: str) -> list[tuple[str, float]]:
    """Ranked (language, probability) from langdetect, with OUR preprocessing.

    PUBLIC because the ingest quality gate needs the raw probability, which
    detect_lang() deliberately hides behind `allowed` and a threshold. That gate
    used to call langdetect itself, so it did not get the ALL-CAPS handling below
    and could judge a document's language differently from the reduction that
    indexed it. One function, one preprocessing — that is the whole point.

    Returns [] instead of raising: nothing in this module may take the pipeline
    down over a detection. Callers that distinguished LangDetectException from
    other failures therefore see both as [].
    """
    try:
        from langdetect import DetectorFactory, detect_langs
    except ImportError:
        return []
    DetectorFactory.seed = _LANGDETECT_SEED
    if len(text) < _LANGDETECT_LOWER_BELOW:
        text = text.lower()
    try:
        return [(p.lang, float(p.prob)) for p in detect_langs(text)]
    except Exception:
        return []


def _langdetect_probe(text: str, allowed: tuple) -> str | None:
    """Best language among `allowed` per langdetect, or None if it abstains."""
    for lang, prob in langdetect_scores(text):
        if lang in allowed:
            return lang if prob >= _LANGDETECT_MIN_PROB else None
    return None


def _dictionary_vote(words: list[str], allowed: tuple) -> str | None:
    """Which language's dictionary knows more of these words (min. margin 2)?

    The only signal that works on FRAGMENTS carrying no function words at all:
    table rows ("Source system type: SQL / Source Technology: athena"), keyword
    lists, two-word queries. Marker scoring returns 0:0 on those, so without
    this every English table row fell to the default and landed in the Czech
    bucket (found on the Lakebridge docs, 2026-07-28). Returns None when the
    dictionaries disagree too weakly to decide.
    """
    try:
        from simplemma import is_known
    except ImportError:
        return None
    scores: dict[str, int] = {}
    for lang in allowed:
        try:
            scores[lang] = sum(1 for w in words if is_known(w, lang=lang))
        except Exception:
            return None
    if len(scores) < 2:
        return None
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return ranked[0][0] if ranked[0][1] - ranked[1][1] >= 2 else None


def detect_lang(text: str, allowed: tuple = ("cs", "en")) -> str | None:
    """Deterministic language detection — same text always resolves to the same
    language on the index and on the query side (both call this function).

    Returns None for "cannot tell", NEVER a guess. Deciding what to do with
    "unknown" is the CALLER's business, and the two callers want opposite things:

      * ingest  — fall back along a chain ending at the registry's
                  is_processing_fallback language, recording that it did
                  (langdetect_result_score), so those rows stay findable.
      * query   — do not pick at all; build the query forms in EVERY active
                  language and let the index decide. A query is a few words, so
                  reducing it N times is free, and no language gets a silent
                  head start.

    `allowed` comes from the registry (code_list('language') WHERE is_active),
    never from a list in code, so detection can only propose a language the
    deployment actually has active.

    Two stages:
      1. langdetect, pinned seed, restricted to `allowed` and to probabilities
         >= _LANGDETECT_MIN_PROB. Decides the large majority.
      2. simplemma dictionary vote (margin >= 2) — the FRAGMENT case where
         langdetect abstains: table rows, keyword lists, and Czech typed without
         diacritics ("Matematika a jeji aplikace"), which langdetect reads as
         Slovene.
    """
    lang = _langdetect_probe(text, allowed)
    if lang:
        return lang

    toks = [t.lower() for t in tokenize(text)][:400]
    words = [t for t in toks if len(t) > 2 and not t.isdigit()]
    if words:
        vote = _dictionary_vote(words, allowed)
        if vote in allowed:
            return vote
    return None


def strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))


def tokenize(text: str) -> list[str]:
    """FIRST step: NFC then split on non-word chars. Diacritics kept for reducers."""
    return _WORD.findall(unicodedata.normalize("NFC", text or ""))


def fold(word: str, lowercase: bool = True, strip: bool = True) -> str:
    """LAST step: lowercase + strip diacritics + ASCII-fold a (possibly reduced) word."""
    if lowercase:
        word = word.lower()
    if strip:
        return "".join(_ASCII.findall(strip_diacritics(word)))
    return word


# ── config extraction (from settings.config) ────────────
def surface_cfg(cfg) -> tuple[bool, bool, bool]:
    """(lowercase, strip_diacritics, drop_stopwords) — v1 defaults all True."""
    n = (cfg or {}).get("normalization", {})
    return (n.get("lowercase", True), n.get("strip_diacritics", True), n.get("drop_stopwords", True))


def require_lang(lang: str | None) -> str:
    """The language must be decided by the CALLER — there is no default.

    Every reduction entry point used to carry `lang: str = "cs"`, so any caller
    that forgot to pass one silently got Czech: the quietest possible form of a
    hardcoded language, since nothing in the output says which reducer ran. The
    callers all know their language (the /search filter or detect_lang on the
    query side, the registry-backed chain on the ingest side), so requiring it
    costs nothing and turns a silent wrong index into an immediate error.
    """
    lang = (lang or "").strip().lower()
    if not lang:
        raise ValueError(
            "language is required: it selects the stoplist, the lemmatizer and "
            "the stemmer. Resolve it from code_list('language') (query: the "
            "/search filter or detect_lang; ingest: user code > detect_lang > "
            "is_processing_fallback) and pass it explicitly.")
    return lang


def stopwords_of(cfg, lang: str | None = None) -> set:
    """Stoplist for `lang`, taken ONLY from cfg — there is no built-in list.

    cfg normalization.stopwords_by_lang = {"cs": [...], "en": [...], …}, loaded
    from the qa_stopword číselník by settings.py and by the ingest components.

    A cfg carrying no stopwords_by_lang AT ALL is a caller bug, not a data
    state, and raises. The built-in lists this replaced (155 cs words, 112 en)
    bought no availability — without the database there are no settings, no
    packs and no chunks to search — while they did buy a divergence nobody
    could see: they happened to EQUAL the číselník for cs and en, so those two
    agreed by coincidence, and de/es had no built-in list at all, so
    route.py:94 matched blocklist phrases against German and Spanish tokens
    that were never stopword-filtered. Same rule as require_lang: the caller
    decides, there is no default.

    A language PRESENT in the config but with no list of its own gets an EMPTY
    set. That is a real data state ("this language declares no stopwords") and
    dropping nothing is the neutral behaviour: recall widens slightly and no
    content word is destroyed. The old fallback was
    `_BUILTIN_STOPWORDS.get(lang, STOPWORDS)`, i.e. Czech stopwords over any
    unregistered language, which deleted the German or Polish words that happen
    to spell a Czech function word (die/do/on/to/no/je).

    Offline callers with no database — the parity generator and checker — pass
    contracts/parity/stoplist.json, which is that snapshot as DATA.
    """
    lang = require_lang(lang)
    by_lang = (cfg or {}).get("normalization", {}).get("stopwords_by_lang")
    if by_lang is None:
        raise ValueError(
            "stopwords_by_lang missing from cfg: the stoplist is DATA "
            "(edu_ai.qa_stopword) and must be loaded and passed by the caller. "
            "There is no built-in list any more. Offline tools and tests: pass "
            "contracts/parity/stoplist.json.")
    return set(by_lang.get(lang) or ())


def engine_for(cfg, kind: str, lang: str | None = None) -> dict:
    """The `engines` entry of this kind for THIS language, from settings.

    Language→reducer is DATA: config.engines carries
    {"kind":"stemmer","name":"snowball_english","language":"en"}.

    An entry with NO `language` matches nothing. It used to match every language
    ("that is how the pre-multilingual configs looked"), which meant the two
    language-less entries in settings._FALLBACK resolved for en, de and es as
    well — all three would have been reduced by the CZECH stemmer, mangling
    profesores->profeso instead of profesor and leaving schools/Schulen
    unstemmed. No settings row has carried a language-less entry for a long time
    (0 of 48, active and inactive alike), so nothing legitimate depended on it.
    """
    lang = require_lang(lang)
    for e in (cfg or {}).get("engines", []):
        if e.get("kind") == kind and e.get("language") == lang:
            return e
    return {}


def engine_name(cfg, kind: str, lang: str | None = None) -> str:
    """Engine name for (kind, language), taken ONLY from cfg — no built-in table.

    A cfg carrying no `engines` key AT ALL is a caller bug and raises, exactly
    as stopwords_of does for the stoplist. The table this replaced held cs and
    en only, and both halves of it were wrong in their own way:

      * cs/en AGREED with settings, so the duplicate was invisible — until
        someone changed a language's stemmer in the config, after which the
        cfg-less path would keep serving the previous one. Measured on the two
        Czech stemmers: 1 word in 14 differs (rýsovac vs rýsovak), i.e. the
        index and the query would disagree on SOME queries and nothing would
        error.
      * de/es were ABSENT, so engine_name returned "" and the word passed
        through unstemmed — that is how 66 rows of migration 211 got a wrong
        term_stem.

    A language PRESENT in the config but with no entry of this kind gets "",
    and stem_word deliberately returns the word unstemmed rather than reaching
    for another language's reducer. That is a real data state; a missing
    `engines` key is not.
    """
    lang = require_lang(lang)
    if (cfg or {}).get("engines") is None:
        raise ValueError(
            f"engines missing from cfg (kind={kind!r}, lang={lang!r}): which "
            "reducer implements which language is DATA "
            "(settings.config.engines) and must be loaded and passed by the "
            "caller. There is no built-in table any more. Offline tools and "
            "tests: pass contracts/parity/stoplist.json.")
    return (engine_for(cfg, kind, lang) or {}).get("name") or ""


def stem_min_len(cfg, lang: str | None = None) -> int:
    """min_len of THIS language's stemmer.

    Used to scan `engines` for the first entry of kind 'stemmer' regardless of
    language, so with cs and en both configured the English stemmer silently
    took the Czech min_len — the same defect class as the QA thresholds that were
    read with LIMIT 1. Both currently declare 3, so this was latent, not live.
    """
    params = (engine_for(cfg, "stemmer", lang) or {}).get("params") or {}
    return int(params.get("min_len", 3))


def drop(tokens, drop_sw: bool, stops: set) -> list[str]:
    if drop_sw:
        return [t for t in tokens if t and t not in stops]
    return [t for t in tokens if t]


def normalize_tokens(text: str, cfg: dict | None = None, lang: str | None = None) -> list[str]:
    """Surface form: tokenize → fold → drop stopwords. Language affects only
    the stoplist (folding is language-neutral) — but it is still required, so a
    caller cannot drop a Czech stoplist over English text by omission."""
    lang = require_lang(lang)
    lc, strip, drop_sw = surface_cfg(cfg)
    stops = stopwords_of(cfg, lang)
    return drop((fold(w, lc, strip) for w in tokenize(text)), drop_sw, stops)
