# last change in code by Martin Safranek (2026-08-07).
"""Node — prepare: ensure the query is available as norm + lemma + stem tokens.

Deterministic, no LLM. The client (device) may send the token forms computed
on-device (shipped simplemma dict) for parity with the index; whichever forms
are missing (web / online fast path) are derived here with the exact same
contract as the index, driven by the active setting's normalization config
(search-parity requirement, see text_norm.py). Three PURE forms — norm / lemma /
stem — are produced; the pipeline picks which the lexical steps match on.
"""

import logging
import re

from ..state import SearchState
from ..languages import active_languages, languages_for_entity
from ..text_norm import build_forms, normalize_tokens
from eduai_normalize import detect_lang, strip_diacritics

logger = logging.getLogger(__name__)


# ── deterministic grade extraction from the question text ────────────────────
# The grade arrives EITHER in the API field OR inside the question ("jaké
# pomůcky potřebuju pro 6. třídu", "co má umět šesťák"). Matched over the
# FOLDED text (lower, no diacritics), so "tridu" and "třídu" read the same.
# Anchored on třída/ročník or the -ák pupil nouns — a bare digit ("strana 6",
# "za 3 dny") must never become a grade.
# ── grade-detection vocabulary: DB-first (edu_ai.grade_term, init 104) ──────
# The anchors, ordinal stems, pupil nouns and gymnasium years are DATA, per
# language — adding a language is INSERTs, not code. The tables below are only
# the code fallback for an unreachable DB (same guard-never-crashes contract
# as system_message). The digit patterns are language-neutral and stay code.
# Terms are FOLDED; `prefix` matches \bterm\w* (stems — Czech inflection),
# `exact` matches \bterm\b (an open stem "prim" would match "primarni").
_FALLBACK_TERMS = (
    [("anchor", w, "prefix", None) for w in
     ("trid", "rocnik", "grade", "year", "class", "klasse", "jahrgang",
      "curso", "grado", "clase")]
    + [("ordinal", w, "prefix", g) for w, g in {
        "prvni": 1, "druh": 2, "tret": 3, "ctvrt": 4, "pat": 5, "sest": 6,
        "sedm": 7, "osm": 8, "devat": 9,
        "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
        "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
        "erst": 1, "zweit": 2, "dritt": 3, "viert": 4, "funft": 5,
        "sechst": 6, "siebt": 7, "siebent": 7, "acht": 8, "neunt": 9,
        "primer": 1, "segund": 2, "tercer": 3, "cuart": 4, "quint": 5,
        "sext": 6, "septim": 7, "octav": 8, "noven": 9}.items()]
    + [("pupil", w, "prefix", g) for w, g in {
        "prvnac": 1, "prvnak": 1, "druhak": 2, "tretak": 3, "ctvrtak": 4,
        "patak": 5, "sestak": 6, "sedmak": 7, "osmak": 8, "devatak": 9}.items()]
    + [("year", w, "exact", g) for w, g in {
        "prima": 6, "primy": 6, "prime": 6, "primu": 6, "primou": 6,
        "sekunda": 7, "sekundy": 7, "sekunde": 7, "sekundu": 7, "sekundou": 7,
        "tercie": 8, "tercii": 8, "kvarta": 9, "kvarty": 9, "kvarte": 9,
        "kvartu": 9, "kvartou": 9}.items()]
)

_grade_cache: dict = {"at": 0.0, "pats": None}
_GRADE_TTL_S = 60.0


def _term_alt(term: str, match: str) -> str:
    return re.escape(term) + (r"\w*" if match == "prefix" else r"\b")


_ENUM_SEP = (r"(?:\s*(?:,|[–—-]|\ba\b|\baz\b|\bi\b|\bnebo\b|\band\b|\bor\b"
             r"|\bto\b|\bund\b|\boder\b|\bbis\b|\by\b|\bo\b|\bhasta\b)\s*)+")


def _enum_grades(seg: str) -> set[str]:
    """Grades in an enumeration segment; range separators (až/–/to/bis) fill in."""
    out: set[str] = set()
    nums = list(re.finditer(r"[1-9]", seg))
    for i, m in enumerate(nums):
        out.add(m.group(0))
        if i + 1 < len(nums):
            between = seg[m.end():nums[i + 1].start()]
            if re.search(r"az|to|bis|hasta|[–—-]", between):
                lo, hi = sorted((int(m.group(0)), int(nums[i + 1].group(0))))
                out.update(str(g) for g in range(lo + 1, hi))
    return out


def _compile_grade_patterns(terms):
    """[(compiled regex, {matched term -> grade} | None | 'enum')] from číselník rows."""
    anchors = [_term_alt(t, m) for k, t, m, g in terms if k == "anchor"]
    if not anchors:
        return []
    anchor = "(?:" + "|".join(anchors) + ")"
    num = r"[1-9]\s*(?:st|nd|rd|th|\.|º|°)*"
    pats = [
        # "3., 5. a 7. třída", "1.–5. ročník", "1st and 2nd grade"
        (re.compile(r"\b((?:" + num + _ENUM_SEP + r")+" + num + r")\s*" + anchor),
         "enum"),
        # "6. třída", "6th grade", "6.º curso"
        (re.compile(r"\b([1-9])\s*(?:st|nd|rd|th|\.|º|°)*\s*" + anchor), None),
        # "třída – 6", "grade 6", "Klasse 6"
        (re.compile(anchor + r"\s*[–—-]?\s*([1-9])\b"), None),
    ]
    for kind, standalone in (("ordinal", False), ("pupil", True), ("year", True)):
        table = {t: g for k, t, m, g in terms if k == kind}
        alts = [_term_alt(t, m) for k, t, m, g in terms if k == kind]
        if not table:
            continue
        body = r"\b(" + "|".join(sorted(alts, key=len, reverse=True)) + r")"
        rx = re.compile(body + ("" if standalone else r"\s+" + anchor))
        pats.append((rx, table))
    return pats


def _grade_patterns():
    """Compiled patterns from edu_ai.grade_term, cached briefly; code fallback."""
    import time
    now = time.monotonic()
    if _grade_cache["pats"] is not None and now - _grade_cache["at"] < _GRADE_TTL_S:
        return _grade_cache["pats"]
    terms = None
    try:
        from ..db import get_cursor
        from .. import config as _cfg
        with get_cursor() as cur:
            cur.execute(f"SELECT kind, term, match, grade "
                        f"FROM {_cfg.PG_SCHEMA}.grade_term WHERE is_active")
            rows = cur.fetchall()
        if rows:
            terms = [(r[0], r[1], r[2], r[3]) for r in rows]
    except Exception as e:
        logger.warning("grade_term load failed (%s) — code fallback", e)
    pats = _compile_grade_patterns(terms or _FALLBACK_TERMS)
    _grade_cache.update(at=now, pats=pats)
    return pats


def grade_from_text(question: str) -> str | None:
    """code_list('school_grade') code ('1'..'9') found in the question, or None."""
    # lower + no diacritics, but KEEP spacing and punctuation — fold() is a
    # per-word normaliser that strips both, and "6. tridu" needs its dot/space
    folded = strip_diacritics((question or "").lower())
    for rx, table in _grade_patterns():
        m = rx.search(folded)
        if not m:
            continue
        if table == "enum":
            grades = _enum_grades(m.group(1))
            # several grades named -> no single caller grade; returning one of
            # them (or letting the single-number pattern pick one) would be a
            # guess, and this value drives the ranking bonus
            return grades.pop() if len(grades) == 1 else None
        if table is None:
            return m.group(1)
        hit = m.group(1)
        for term, g in sorted(table.items(), key=lambda kv: -len(kv[0])):
            if hit.startswith(term):
                return str(g)
    return None


# ── time-span detection ──────────────────────────────────────────────────────
# Duration questions ("uvolnění na 14 dní / na týden") FORCE the semantic step:
# they phrase the answer differently ("uvolnění" ↔ heading "Docházka do školy")
# AND the number pollutes the lexical rank. Deterministic: a number + a time unit,
# a duration preposition + unit, or a bare week/month/term word. Folded (lower, no
# diacritics) so "dní"/"dni", "týden"/"tyden" read the same. cs + en/de/es units.
_TIMESPAN_RX = re.compile(
    r"\b\d+\s*(den|dnu|dni|dnech|dny|tydn\w*|tyden|hodin\w*|mesic\w*"
    r"|days?|weeks?|hours?|months?"
    r"|tage?|wochen?|stunden?|monate?"
    r"|dias?|semanas?|horas?|mes|meses)\b"
    r"|\bna\s+(den|tyden|mesic|pololeti|prazdniny|vikend|week|month|woche|monat|semana|mes)\b"
    r"|\b(tyden|tydne|mesic|mesice|pololeti|prazdniny|vikend)\b")


def timespan_from_text(question: str) -> bool:
    """True when the question names a time span (duration). Forces the semantic
    retrieval step regardless of how strong the lexical pool looks."""
    return bool(_TIMESPAN_RX.search(strip_diacritics((question or "").lower())))


def _union_forms(question: str, cfg, langs) -> dict[str, list[str]]:
    """Query forms reduced in EVERY candidate language, tokens unioned.

    Used when detection cannot tell (a one-word query, a bare number, an
    acronym). Picking a language there would be a guess, and the guess used to
    be Czech — a silent head start for one language on every ambiguous query.
    Unioning instead lets the INDEX decide: a token only matches where it was
    genuinely produced by the same reducer, so the wrong language's forms simply
    find nothing. A query is a handful of words, so reducing it N times costs
    nothing measurable, unlike doing this per chunk on the index side.

    Order is preserved and duplicates dropped — several languages agree on most
    surface tokens, and a repeated term would skew ts_rank.
    """
    out: dict[str, list[str]] = {"norm": [], "lemma": [], "stem": []}
    seen: dict[str, set[str]] = {k: set() for k in out}
    for lang in langs:
        forms = build_forms(question, cfg, lang)
        for key in out:
            for tok in forms.get(key, []):
                if tok not in seen[key]:
                    seen[key].add(tok)
                    out[key].append(tok)
    return out


def prepare(state: SearchState, cfg: dict | None = None) -> dict:
    question = state.get("user_question", "") or ""
    norm = state.get("query_norm_tokens")
    lemma = state.get("query_lemma_tokens")
    stem = state.get("query_stem_tokens")

    # query language: the validated /search `language` filter wins; otherwise
    # deterministic detection over the languages the REGISTRY has active (the
    # číselník is the single source of what languages exist — never a list in
    # code). detect_lang returns None for "cannot tell" and we do NOT guess:
    # the query side unions instead, see _union_forms. The registry's
    # is_processing_fallback is deliberately NOT consulted here — that flag is
    # for the ingest side, which has to commit a single language per stored row.
    # the school's language selection (init 109) — a cs-only school never
    # builds de/es query forms nor detects them
    langs = languages_for_entity(state.get("legal_entity"))
    lang = state.get("language_filter") or detect_lang(question, allowed=tuple(langs))
    build_langs = (lang,) if lang else tuple(langs)

    if not (norm and lemma and stem):
        forms = _union_forms(question, cfg, build_langs)
        norm = norm or forms["norm"]
        lemma = lemma or forms["lemma"]
        stem = stem or forms["stem"]
        source = "server-derived"
    else:
        # normalize client tokens defensively (per active cfg) so a sloppy client
        # can't poison the tsquery; keeps parity with the index tokenizer.
        def _renorm(tokens):
            out, seen = [], set()
            for bl in build_langs:
                for tok in tokens:
                    for t in normalize_tokens(tok, cfg, bl):
                        if t not in seen:
                            seen.add(t)
                            out.append(t)
            return out
        norm, lemma, stem = _renorm(norm), _renorm(lemma), _renorm(stem)
        source = "client"

    logger.info("prepare: %d norm / %d lemma / %d stem tokens (%s, lang=%s)",
                len(norm), len(lemma), len(stem), source,
                lang or f"undecided→{'+'.join(build_langs)}")
    # query_language stays None when undecided: downstream steps treat that as
    # "every language" (load_synonym_groups(None) loads all groups) and the
    # answer step omits the deterministic language instruction rather than
    # ordering the model into a language nothing pointed to.
    # grade: the API field wins; otherwise derived from the question text.
    # (The field was normalised to a číselník code at the API boundary.)
    out = {"query_norm_tokens": norm, "query_lemma_tokens": lemma,
           "query_stem_tokens": stem, "query_language": lang,
           # duration in the question forces the semantic step (executor gate)
           "query_has_timespan": timespan_from_text(question)}
    g = grade_from_text(question)
    if g:
        # ALWAYS exposed separately: the answer step compares the grade the
        # QUESTION asks about with the caller's profile grade and says so when
        # they differ — collapsing both into user_school_grade lost that.
        out["question_grade"] = g
    if not state.get("user_school_grade") and g:
        logger.info("prepare: grade %s derived from the question text", g)
        out["user_school_grade"] = g
    return out
