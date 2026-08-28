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

# ── Czech stopwords (normalized: lower, no diacritics) — built-in FALLBACK only;
#    the authoritative list is the qa_stopword číselník, injected via cfg. ──
STOPWORDS = {
    "od", "do", "na", "v", "ve", "z", "ze", "s", "se", "k", "ke", "u", "o",
    "po", "za", "pro", "pri", "nad", "pod", "pred", "mezi", "bez", "kolem",
    "podle", "okolo", "kvuli", "vedle", "behem", "skrz", "krom", "vuci",
    "a", "i", "ale", "nebo", "ci", "ze", "aby", "kdyz", "protoze", "jelikoz",
    "takze", "tedy", "tak", "tez", "take", "ani", "nebot", "avsak", "vsak",
    "proto", "pak", "uz", "jiz", "jen", "jenom", "snad", "asi", "prece",
    "vlastne", "prave", "jeste", "no", "ano", "ne", "ja", "totiz",
    "ty", "on", "ona", "ono", "my", "vy", "oni", "ony", "si", "me", "mne", "mi",
    "te", "tebe", "ti", "tobe", "ho", "jej", "jeho", "ji", "jeji", "jich", "jim",
    "jimi", "nam", "nas", "vam", "vas", "svuj", "sve", "svych", "svym", "muj",
    "moje", "meho", "mych", "tvuj", "tve", "ten", "ta", "to", "tento", "tato",
    "toto", "tyto", "tom", "tomto", "tim", "teto", "tech", "sam", "sama", "samo",
    "vsechno", "vse", "kazdy", "nejaky", "nejaka",
    "je", "jsou", "jsem", "jsi", "jsme", "jste", "byl", "byla", "bylo", "byli",
    "byly", "byt", "bude", "budou", "budu", "budes", "budeme", "budete",
    "bych", "bys", "by", "bychom", "byste", "sme",
    "tady", "tam", "sem", "tu", "zde", "ted",
    "the",
}

# ── English stopwords (built-in FALLBACK; authoritative list = qa_stopword
#    číselník lang='en'). Interrogatives (what/which/who/when/where/why/how)
#    are intentionally NOT here — same design as Czech: they carry intent and
#    are excluded from coverage via the interrogative synonym groups instead. ──
STOPWORDS_EN = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "so", "than",
    "of", "at", "by", "for", "with", "about", "against", "between", "into",
    "through", "during", "before", "after", "above", "below", "to", "from",
    "up", "down", "in", "out", "on", "off", "over", "under", "again", "once",
    "here", "there", "all", "any", "both", "each", "few", "more", "most",
    "other", "some", "such", "no", "nor", "not", "only", "own", "same", "too",
    "very", "can", "will", "just", "should", "would", "could", "may", "might",
    "must", "shall", "now",
    "is", "are", "was", "were", "be", "been", "being", "am",
    "have", "has", "had", "having", "do", "does", "did", "doing",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us",
    "them", "my", "your", "his", "its", "our", "their", "mine", "yours",
    "this", "that", "these", "those", "as",
    "s", "t",   # possessive 's / n't tokenization leftovers
}

_BUILTIN_STOPWORDS = {"cs": STOPWORDS, "en": STOPWORDS_EN}

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


def _langdetect_probe(text: str, allowed: tuple) -> str | None:
    """Best language among `allowed` per langdetect, or None if it abstains."""
    try:
        from langdetect import DetectorFactory, detect_langs
    except ImportError:
        return None
    DetectorFactory.seed = _LANGDETECT_SEED
    try:
        for p in detect_langs(text):
            if p.lang in allowed:
                return p.lang if p.prob >= _LANGDETECT_MIN_PROB else None
    except Exception:
        return None
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
    """Stoplist for `lang` from cfg (qa_stopword číselník injected by
    settings.py and by the ingest components); built-in list as the fallback.

    cfg normalization.stopwords_by_lang = {"cs": [...], "en": [...], …}. The
    legacy flat `stopwords` key (which was the Czech list by construction) is
    gone: it made Czech the only language served from a differently-shaped key,
    and the built-in Czech list is a superset of the číselník's, so removing it
    cannot change any token.

    A language with no list anywhere gets an EMPTY set, not the Czech one. The
    fallback used to be `_BUILTIN_STOPWORDS.get(lang, STOPWORDS)`, i.e. Czech
    stopwords over any unregistered language — which then deletes the German or
    Polish words that happen to spell a Czech function word (die/do/on/to/no/je).
    Dropping nothing is the neutral behaviour: recall widens slightly, no content
    word is destroyed, and the missing list shows up in the readiness check.
    """
    lang = require_lang(lang)
    by_lang = (cfg or {}).get("normalization", {}).get("stopwords_by_lang")
    if by_lang and by_lang.get(lang):
        return set(by_lang[lang])
    return _BUILTIN_STOPWORDS.get(lang, set())


# Which reducer implements which language when the CONFIG says nothing. Only a
# fallback — settings.config.engines is authoritative and always present in
# the running services. This exists for the cfg-less paths: the parity vectors
# (deliberately pinned to library behaviour, not to DB data) and any caller that
# reduces a single string without loading the setting. Same role as
# _BUILTIN_STOPWORDS. A new language needs a config row, not an entry here.
_DEFAULT_ENGINE = {
    ("lemmatizer", "cs"): "simplemma",
    ("lemmatizer", "en"): "simplemma",
    ("stemmer", "cs"): "dolamic_savoy_light",
    ("stemmer", "en"): "snowball_english",
}


def engine_for(cfg, kind: str, lang: str | None = None) -> dict:
    """The `engines` entry of this kind for this language, from settings.

    Language→reducer is DATA: config.engines already carries
    {"kind":"stemmer","name":"snowball_english","language":"en"}. Entries with no
    `language` are treated as applying to any (that is how the pre-multilingual
    configs looked).
    """
    lang = require_lang(lang)
    generic = None
    for e in (cfg or {}).get("engines", []):
        if e.get("kind") != kind:
            continue
        el = e.get("language")
        if el == lang:
            return e
        if el is None and generic is None:
            generic = e
    return generic or {}


def engine_name(cfg, kind: str, lang: str | None = None) -> str:
    """Engine name for (kind, language) — config first, built-in default second."""
    lang = require_lang(lang)
    name = (engine_for(cfg, kind, lang) or {}).get("name")
    return name or _DEFAULT_ENGINE.get((kind, lang), "")


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
