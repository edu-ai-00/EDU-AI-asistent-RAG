# last change in code by Jan Drozd and Martin Safranek (2026-08-22).
"""Node — answer: the single LLM on the cascade (RAG generation, terminal).

Reached only when QA (exact/lexical/vector) produced nothing and the RAG
retrievers gathered chunks. Assembles a context string from state["rag_chunks"]
(dedup by file_item_uuid, char budget) and generates a grounded answer with the
answer_generator prompt. Empty pool → a polite no_answer (no LLM call).
"""

import json
import logging
import re
import unicodedata
from urllib.parse import unquote

from ..llm import get_chat
from langchain_core.messages import SystemMessage, HumanMessage

from ..state import SearchState
from ..db import get_cursor
from ..messages import (system_message, resolve_unanswered,
                        resolve_llm_unavailable, subject_absent)
from ..languages import language_name, ui_default_language
from ..settings import terms_for_language
from .. import config
from .. import scope
from .prepare import prepare
from .route import route
from .qa_exact import qa_exact
from .qa_lexical import qa_lexical
from .qa_common import pack_filter
from .. import settings
from .answer_generator import SYSTEM_PROMPT

# appended to the CODE-FALLBACK system prompt: shorter answers = directly
# faster answers (generation is billed & timed per output token). max_tokens is
# the hard stop; this instruction makes the model END naturally before hitting it.
_BREVITY = (
    "\n\nLENGTH\n"
    "Be concise. Answer in the language of the question, in at most ~120 words "
    "(a short paragraph or a compact list). Do not restate the question or "
    "summarize the context; give the answer directly."
)

logger = logging.getLogger(__name__)

# The live prompt is DB-first (edu_ai.prompt_registry, hot-editable per request —
# no restart needed), the code constant is only the fallback. Same DB>code
# pattern as the ingestion components' PROMPT_KEY lookup.
_PROMPT_KEY = "search_answer_generation"


_RE_CELL = re.compile(r"row_\d+_column_\d+:\s*")
_RE_MARKUP = re.compile(r"[#*_\s]+")


def _load_system_prompt() -> str:
    try:
        with get_cursor() as cur:
            cur.execute(
                f"SELECT prompt_text FROM {config.PG_SCHEMA}.prompt_registry "
                "WHERE prompt_key = %s AND is_active ORDER BY version DESC LIMIT 1",
                (_PROMPT_KEY,),
            )
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        logger.warning("answer: prompt_registry lookup failed (%s) — code fallback", e)
    return SYSTEM_PROMPT + _BREVITY

_UUID_PREFIX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_", re.I)
_FILE_EXT = re.compile(r"\.(pdf|docx?|pptx?|xlsx?|md|html?)(\?|#|$)", re.I)


def _clean_file_title(name: str) -> str:
    """UUID prefix off, %-decoded, underscores→spaces, extension dropped."""
    name = unquote(name or "")
    name = _UUID_PREFIX.sub("", name)
    name = _FILE_EXT.sub("", name)
    return name.replace("_", " ").strip()


def source_display(chunk: dict) -> tuple[str, str | None]:
    """
    User-friendly (title, url) for one source chunk:
      * web page (source_url with no file extension)   → title = URL,        link = URL
      * file downloaded from the web (source_url = file)→ title = clean name, link = source_url
      * uploaded file (no source_url)                  → title = original name, no link
    """
    url = chunk.get("source_url") or None
    orig = chunk.get("original_file_name") or ""
    fname = chunk.get("file_name") or ""

    # a "file" if the name OR the url carries a document extension — this also
    # catches extensionless file URLs (e.g. msmt.gov.cz/file/65358_2_1/ → PDF),
    # where only the derived file name has the .pdf.
    is_file = bool(_FILE_EXT.search(orig or fname) or (url and _FILE_EXT.search(url)))

    if url and not is_file:
        return url, url                                   # genuine web page → URL
    if url:
        return (_clean_file_title(orig or fname) or fname), url   # downloaded file
    # uploaded file: keep the user's original name (just drop the extension)
    title = _FILE_EXT.sub("", unquote(orig or fname)).replace("_", " ").strip()
    return (title or fname), None


def _clip(text: str, limit: int, cut_ratio: float = 0.6) -> str:
    """Cut to `limit` characters on a whitespace boundary, marking the cut.

    `cut_ratio` is how far back the last space may sit before losing it costs
    more than a mid-word cut: below it the text is cut hard instead. Czech
    compounds and long URLs are the cases that make the two ends differ.
    """
    t = text or ""
    if limit <= 0 or len(t) <= limit:
        return t
    cut = t[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > limit * cut_ratio else cut).rstrip() + "\n[…]"


_LEAD_IN = re.compile(r"^\s*\*\*(?P<lead>[^*]{1,200}?)\*\*\s*:?\s*", re.S)

# Sentences that only say "this document does not answer the question".
# BUILT-IN FALLBACK only — the live vocabulary is the answer_exclusion_rule
# číselník (migration 133, mirror of chunk_exclusion_rule); this constant is
# used when the table cannot be read. The meta-vocabulary is narrow and
# distinctive (netýká se / neuvádí / není zde uvedeno / nikoli informace o …),
# so it matches without touching content negations like "nesmí"/"není povoleno".
_NOINFO_SENT = re.compile(
    r"netýk|neuvád|neobsahuj|nezmiňuj|nenalez|nevyplýv|neposkytuj"
    r"|nikoli\s+informac"
    r"|nen[ií]\s+(?:zde\s+)?uveden|nejsou\s+(?:zde\s+)?uveden"
    r"|uveden[oay]?\s+(?:zde\s+)?nen[ií]|uvedeny\s+(?:zde\s+)?nejsou",
    re.IGNORECASE)


def _noinfo_matcher(lang: str | None) -> re.Pattern | None:
    """The active meta-sentence matcher: composed from the číselník, or the
    built-in fallback when the table is unreadable. None = backstop OFF
    (no active rows for the language — the configured kill switch)."""
    frags = settings.answer_exclusion_patterns(lang or "cs")
    if frags is None:
        return _NOINFO_SENT
    if not frags:
        return None
    try:
        return re.compile("|".join(f"(?:{f})" for f in frags), re.IGNORECASE)
    except re.error as e:
        logger.warning("answer: answer_exclusion_rule regex invalid (%s) — built-in", e)
        return _NOINFO_SENT


def drop_empty_handed_paragraphs(
        answer: str, noinfo: re.Pattern | None = _NOINFO_SENT,
) -> tuple[str, list[str]]:
    """Deterministic backstop for the prompt's EARNED-paragraph rule.

    The prompt forbids paragraphs whose only content is "this document doesn't
    cover the question", yet the model still emits them occasionally. A
    paragraph is dropped when EVERY sentence in its body is such a meta
    statement — and only when at least one substantive paragraph remains, so a
    legitimate everything-is-negative answer passes through untouched. Italic
    grade-validity notes riding on a dropped paragraph are re-attached to the
    last kept one rather than lost. Returns (answer, dropped lead-ins)."""
    if noinfo is None:
        return answer, []
    paras = [p.strip() for p in re.split(r"\n\s*\n", answer or "") if p.strip()]
    if len(paras) < 2:
        return answer, []
    merged: list[str] = []
    for p in paras:
        if merged and re.fullmatch(r"\*[^*\n]+\*", p):
            merged[-1] = merged[-1] + "\n\n" + p
        else:
            merged.append(p)
    if len(merged) < 2:
        return answer, []
    kept, dropped, orphan_notes = [], [], []
    for p in merged:
        body = _LEAD_IN.sub("", p)
        notes = re.findall(r"(?m)^\*[^*\n]+\*$", body)
        body = re.sub(r"(?m)^\*[^*\n]+\*$", "", body).strip()
        sents = [s for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]
        if sents and all(noinfo.search(s) for s in sents):
            lead = _LEAD_IN.match(p)
            dropped.append(lead.group("lead").strip().rstrip(":").strip()
                           if lead else body[:60])
            orphan_notes.extend(notes)
        else:
            kept.append(p)
    if not dropped or not kept:
        return answer, []
    for n in orphan_notes:
        if n not in "\n".join(kept):
            kept[-1] = kept[-1] + "\n\n" + n
    return "\n\n".join(kept), dropped


def _attr_tokens(text: str) -> set:
    """Inflection-tolerant token fingerprint for paragraph→document attribution:
    diacritics-folded 5-char word prefixes (Czech inflection mostly changes the
    ending) plus whole numbers (percentages, dates, counts survive paraphrase)."""
    t = "".join(ch for ch in unicodedata.normalize("NFD", (text or "").lower())
                if unicodedata.category(ch) != "Mn")
    words = re.findall(r"[a-z]{5,}", t)
    return {w[:5] for w in words} | set(re.findall(r"\d+", t))


def split_answer_parts(answer: str, doc_order: list[dict],
                       attrib_positional_min: float = 0.15,
                       attrib_best_min: float = 0.25) -> list[dict]:
    """The answer as one entry per paragraph, each carrying its source document.

    Attribution is by CONTENT, not position. The old positional rule ("as many
    paragraphs as documents → i-th paragraph belongs to i-th document") broke the
    moment the model wrote two paragraphs from ONE document's two sections — the
    second paragraph (Cermat's exam dates) was linked to the OTHER document
    (eduin), whose displayed excerpt then had nothing to do with the text next to
    it. Position is only a tie-break now: each paragraph is fingerprinted
    (_attr_tokens) and compared with every document's context excerpt; the
    positional candidate is kept only when its own overlap CONFIRMS it, otherwise
    the best-overlapping document wins, and a paragraph nothing vouches for gets
    source = None rather than a guessed one. A wrong link is worse than no link.
    The lead-in ↔ section-heading match remains as a fallback for configurations
    that carry no excerpts (include_excerpt=false).
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", answer or "") if p.strip()]
    # The GRADE VALIDITY note is the answer's trailing LINE, not a paragraph:
    # models emit it blank-line-separated and the splitter then rendered it as
    # its own block with a guessed source link (measured: an orphan '*Text
    # pochází…*' block under the cermat link, read as "an empty paragraph").
    # An italic-only block merges into the paragraph before it.
    _merged: list = []
    for _p in paras:
        if _merged and re.fullmatch(r"\*[^*\n]+\*", _p):
            _merged[-1] = _merged[-1] + "\n\n" + _p
        else:
            _merged.append(_p)
    paras = _merged
    if not paras:
        return []
    docs = list(doc_order or [])

    def _split_lead(text):
        m = _LEAD_IN.match(text)
        if not m:
            return None, text
        return m.group("lead").strip(), text[m.end():].strip()

    doc_toks = [_attr_tokens((d.get("excerpt") or "")
                             + " " + " ".join(d.get("sections") or [])) for d in docs]
    _have_toks = any(doc_toks)
    positional = len(paras) == len(docs) and len(docs) > 0
    out = []
    for i, para in enumerate(paras):
        lead, body = _split_lead(para)
        doc = None
        if _have_toks:
            ptoks = _attr_tokens(para)
            if ptoks:
                ov = [len(ptoks & dt) / len(ptoks) for dt in doc_toks]
                best = max(range(len(docs)), key=lambda j: ov[j], default=None)
                # Two named thresholds since 2026-08-18 — they were bare 0.15 and
                # 0.25 sitting in this condition, the only place in the whole
                # system where those numbers existed, deciding which SOURCE a
                # paragraph of the answer is attributed to. Nothing named them and
                # nothing could tune them: changing the attribution of an answer
                # meant editing Python and rebuilding the image.
                #
                # positional: the paragraph in slot i is credited to document i
                # only when its own words confirm it, at a LOW bar — the order is
                # already evidence, the overlap only has to not contradict it.
                # best: any other document has to earn it on content alone, so the
                # bar is higher. Keeping best > positional is the invariant; equal
                # values make the positional branch redundant, and reversing them
                # lets a weakly-overlapping foreign document outrank the ordering.
                if positional and ov[i] >= attrib_positional_min:
                    doc = docs[i]
                elif best is not None and ov[best] >= attrib_best_min:
                    doc = docs[best]
        elif positional:
            doc = docs[i]      # no excerpts to verify against — legacy behaviour
        if doc is None and lead:
            low = lead.lower()
            for d in docs:
                if any(h and (h.lower() in low or low in h.lower())
                       for h in (d.get("sections") or [])):
                    doc = d
                    break
        out.append({
            # ZERO-BASED, and deliberately out of step with `source_rank` /
            # sources[].rank next to it, which are 1-based. Do not "normalise"
            # this to i + 1: the contract published to app developers shows
            # index 0, clients were written against it, and a silent shift of the
            # base is the kind of change nothing fails on until a client uses it
            # as an offset. `index` is only the paragraph's position in this
            # array, which the client already has from the order — it carries no
            # meaning that the mismatch with source_rank could corrupt.
            "index": i,
            "lead_in": lead,          # topic label, without the ** markers
            "text": body,             # the paragraph without its lead-in
            "markdown": para,         # exactly as generated, lead-in still bold
            "source_rank": (doc or {}).get("rank"),
            "title": (doc or {}).get("title"),
            "url": (doc or {}).get("url"),
            "file_uuid": (doc or {}).get("file_uuid"),
            # the source text this paragraph was written from; None when the
            # `include_excerpt` parameter is off or the paragraph is unattributed
            "excerpt": (doc or {}).get("excerpt"),
        })
    return out

def _detect_query_domains(state: SearchState, max_df_share: float) -> set[str]:
    """Deterministic query→domain mapping over taxonomy_rules.domain_keywords.

    A keyword maps the question onto its DOMAIN when all of its GUARDED tokens
    appear among the query's reduced tokens (norm ∪ lemma ∪ stem — the same
    build_forms contract as everywhere else). Guards, per Martin's condition:
    a keyword token in the coverage stoplist or corpus-generic by df share is
    IGNORED, and a keyword whose tokens are ALL ignored can never fire — a
    generic word must not switch a domain on. Returns DOMAIN type_codes;
    failures return empty (the signal is an optional boost, never a gate)."""
    qtokens = (set(state.get("query_norm_tokens") or [])
               | set(state.get("query_lemma_tokens") or [])
               | set(state.get("query_stem_tokens") or []))
    if not qtokens:
        return set()
    try:
        from .rag_keywords import _kw_tokens
        from ..settings import coverage_stopwords
        from ..languages import active_languages
        langs = ([state.get("query_language")] if state.get("query_language")
                 else list(active_languages()))
        _stop: set = set()
        for _l in langs:
            _stop |= (coverage_stopwords().get(_l or "") or set())
        _form = ("norm", "lemma", "stem")
        with get_cursor() as cur:
            cur.execute(
                f"SELECT type_code, domain_keywords FROM {config.PG_SCHEMA}.taxonomy_rules "
                "WHERE taxonomy_type_code = 'DOMAIN' AND is_active "
                "  AND domain_keywords IS NOT NULL "
                "  AND (legal_entity_code IS NULL OR legal_entity_code = ANY(%s))",
                (list(state.get("organization_entities") or []),))
            rows = cur.fetchall()
            # reduce every keyword once; collect candidate tokens for the df guard
            _kw_reduced: list[tuple[str, list[set]]] = []   # (type_code, [token-sets per word])
            _all_toks: set = set()
            for type_code, kws in rows:
                for kw in (kws or []):
                    words = [w for w in str(kw).split() ]
                    per_word = []
                    for w in words:
                        toks = set()
                        for _l in langs:
                            toks |= set(_kw_tokens(w, _form, _l))
                        toks = {t for t in toks
                                if t not in _stop and not t.strip(".").isdigit()}
                        if toks:
                            per_word.append(toks)
                            _all_toks |= toks
                    if per_word:
                        _kw_reduced.append((type_code, per_word))
            if not _kw_reduced:
                return set()
            # df guard over the ACTIVE corpus: a token in more than max_df_share
            # of chunks is generic and cannot represent a domain
            cur.execute(
                f"SELECT count(*) FROM {config.CHUNKS_TABLE} c "
                f"JOIN {config.METADATA_TABLE} m ON c.file_uuid = m.file_uuid "
                f" AND c.processing_uuid = m.processing_uuid AND m.is_active = true")
            _ntot = cur.fetchone()[0] or 0
            _generic: set = set()
            if _ntot:
                cur.execute(
                    f"SELECT tok, (SELECT count(*) FROM {config.CHUNKS_TABLE} c "
                    f"  JOIN {config.METADATA_TABLE} m ON c.file_uuid = m.file_uuid "
                    f"   AND c.processing_uuid = m.processing_uuid AND m.is_active = true "
                    f"  WHERE c.bm25_text @@@ tok) FROM unnest(%s::text[]) AS tok",
                    (sorted(_all_toks),))
                _generic = {t for t, d in cur.fetchall()
                            if (d or 0) / _ntot > max_df_share}
        detected: set = set()
        for type_code, per_word in _kw_reduced:
            usable = [toks - _generic for toks in per_word]
            usable = [t for t in usable if t]
            # every non-generic word of the keyword must be present in the query
            if usable and all(toks & qtokens for toks in usable):
                detected.add(type_code)
        return detected
    except Exception as e:
        logger.warning("answer: query-domain detection failed (%s) — none", e)
        return set()


# A clarify option SENDS its heading as the next query, so the heading has to
# stand on its own. Two kinds cannot: a bare ordinal ("Článek V" — it matches
# every document that has one, which is how one click returned bezpečnost,
# Wellbeing and přijímačky in one answer), and a file name, which reaches
# _sections via the `or c.get("file_name")` fallback below.
# FALLBACK ONLY — the live list is qa_stopword scope='section_word'
# (migration 155). Section labels are corpus- and language-specific and grow with
# every new document type, so they are data; this set only survives a DB outage.
_SECTION_WORDS = {"článek", "čl", "hlava", "oddíl", "odstavec", "část", "bod",
                  "příloha", "section", "article", "chapter", "annex",
                  "paragraph", "part", "appendix"}
_UUID_RX = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_FILE_RX = re.compile(r"\.(html?|pdf|docx?|pptx?|xlsx?|txt|md)$|_www\.|://", re.I)


# the document's own list numbering ("8. Pravidla, povolené pomůcky a den D") is
# noise both on the button and in the query it sends
_LIST_NUM_RX = re.compile(r"^\d+\s*[.)]\s+")


def _usable_as_query(heading: str | None, lang: str | None = None) -> bool:
    """Can this heading be sent back as a question and still mean something?

    `lang` is the QUERY language, and it is also the heading's: retrieval filters
    chunks on the document/chunk language, so nothing in the pool is in another
    one. Passing it keeps English section labels from being stripped out of Czech
    headings and vice versa.
    """
    h = (heading or "").strip()
    if not h or _UUID_RX.search(h) or _FILE_RX.search(h):
        return False
    # strip section words, numerals and punctuation; what remains is the subject
    _sw = terms_for_language("section_word", lang, _SECTION_WORDS)
    rest = "".join(
        ch for w in re.split(r"[\s.,:;—–-]+", h.lower())
        if w and w.strip(".") not in _sw
        and not w.strip(".").isdigit()
        and not set(w.strip(".")) <= set("ivxlcdm")
        for ch in w if ch.isalpha())
    return len(rest) >= 4


def _as_query(heading: str | None, lang: str | None = None) -> str | None:
    """The heading as a sendable query, or None if it cannot stand alone."""
    h = _LIST_NUM_RX.sub("", (heading or "").strip())
    return h if _usable_as_query(h, lang) else None


def _strip_list_numbering(label: str | None) -> str | None:
    """Same trim across a breadcrumb — the model writes the paragraph lead-in
    from this label, so "6. Kdy a kde…" reached the answer with the number on.
    Per segment, and a segment that is ONLY a number keeps it: dropping it to
    an empty crumb would lose the level entirely."""
    if not label:
        return label
    return " › ".join(_LIST_NUM_RX.sub("", p.strip()) or p.strip()
                      for p in label.split("›"))


def prepare_generation(state: SearchState, params: dict | None = None) -> dict | None:
    """Everything BEFORE the LLM call: sort/dedupe chunks, assemble the context
    and the user-facing sources, resolve model + output cap, build the messages.

    Returns None when the chunk pool is empty (nothing to generate from).
    Shared by `answer` (blocking invoke) and the /search/stream endpoint
    (token streaming) so both paths generate from EXACTLY the same input.
    """
    params = params or {}
    chunks = state.get("rag_chunks", []) or []
    if not chunks:
        return None
    _diag: dict = {}   # diagnostics for the query log (focus decisions + candidate pool)

    # RANK FUSION, not score comparison. Each gatherer scores on its own scale —
    # document_lexical gives coverage 0..1, rag_vector a cosine, rag_keywords a
    # raw overlap count — and sorting those together compares numbers that do not
    # mean the same thing. Positions do: "3rd by meaning" is comparable to "14th
    # by words" whatever the underlying numbers are, and it survives a change of
    # embedding model or of chunk size, which an absolute threshold does not.
    #
    #     fused = sum over the lists a chunk appears in of  1 / (rrf_k + rank)
    #
    # A chunk placed well by BOTH signals therefore outranks one that is first by
    # a single signal — which is the whole point: the chunk answering "Muze se ve
    # skole fotit?" is 3rd by cosine and mid-pack by words, and no single list
    # puts it on top. rrf_k damps the head of each list so one lucky first place
    # cannot outweigh two solid seconds. Falls back to the previous behaviour when
    # nothing carries a rank (a pool gathered by keywords only).
    rrf_k = int(params.get("rrf_k") or 60)
    # keyword matches get the HEAVIEST vote in the fusion. The extracted keywords
    # are distilled from the chunk's real content, so a keyword hit is the most
    # faithful "this chunk is about X" signal we have — more trustworthy than a
    # heading (which can carry boilerplate the body never mentions). At weight 3 a
    # keyword-only match at rank 1 (3/(k+1)) outranks a lexical- or vector-first
    # match (1/(k+1)), yet a chunk agreed on by keyword AND another signal still
    # wins over keyword alone — agreement is never punished. Tunable per settings.
    keyword_weight = float(params.get("keyword_weight", config.KEYWORD_WEIGHT))
    # weight of vector_element_rank (semantic hit on ANOTHER chunk of the same
    # element) in the fusion — 1.0 = element agreement counts like a direct hit
    vector_element_weight = float(params.get("vector_element_weight", 1.0))
    # domain match boost: step param > GLOBAL config->search knob > code default.
    # The global knob (settings row without entity, next to query_log/debug_log)
    # is the one place to tune the influence platform-wide; 0.05 is deliberately
    # small — a tie nudge, ~half of a heading match.
    try:
        from ..settings import get_active_setting
        _dmb_global = (get_active_setting(None)["config"].get("search")
                       or {}).get("domain_match_boost")
    except Exception:
        _dmb_global = None
    domain_match_boost = float(params.get(
        "domain_match_boost", _dmb_global if _dmb_global is not None else 0.05))

    # ── grade preference (never a filter) ────────────────────────────────────
    # The caller's ročník (API field normalised at the boundary, or derived
    # deterministically from the question text in prepare()) prefers chunks
    # whose school_grade matches. NULL/UNKNOWN chunks are neutral — most of the
    # corpus carries no grade, and excluding on it would hide almost everything
    # (the standing decision). Legacy ZS6/SS1 values compare by their number.
    # grade_hard_boost (0.5) and level_bonus (0.03) were read here too and were
    # equally dead — same 2026-08-09 refactor, same reason. All three removed:
    # a settings key that silently does nothing is worse than no key.
    # grade_bonus was read here and used NOWHERE — dead since the 2026-08-09
    # "single score" change moved caller preferences from additive bonuses to the
    # ordering tiers in _sortkey (a constant added to every chunk of a tier orders
    # nothing inside it, and the tier already decides between tiers). Removed rather
    # than left as a settings key that silently does nothing; no active settings row
    # sets it (checked).
    # grade_scope decides what a KNOWN caller grade does to the pool:
    #   "boost"          preference only (bonus), never removes anything
    #   "focus_if_match" (default) — CONDITIONAL: if the asked grade is actually
    #                    represented in the pool (a chunk tagged with it, or with
    #                    9999 = all grades), answer from THOSE chunks ONLY;
    #                    otherwise the grade is a pure boost and nothing is
    #                    dropped. "jaké pomůcky?" as a 6th-grader, when a 6th-
    #                    grade list exists, returns that list alone — but a query
    #                    with no 6th-grade chunk at all still gets an answer.
    grade_scope = str(params.get("grade_scope") or "focus_if_match")
    # the chunk arrays hold ROW IDS of the school's resolved číselník set
    # (init 107) — resolve the caller's grade code onto that set once.
    # 9999 ("Pro všechny ročníky", init 126) matches ANY caller grade.
    _grade_ids: set = set()
    _grade_exact_ids: set = set()   # the caller's grade WITHOUT 9999 — for the near-filter
    # "9999" means ALL ROČNÍKY OF ONE ENTITY — 1..9 under a ZŠ, 10..13 under a SŠ,
    # 1..13 under the umbrella organisation — so it is per-entity, not global.
    # Resolving it only in the caller's own entity (as the query below does) misses
    # the organisation's row, and a chunk carrying THAT row matches neither the
    # grade round nor the grade-less round: no round retrieves it at all. These are
    # every 9999 row whose entity's grade scope CONTAINS the caller's ročník — the
    # organisation's yes, a sibling SŠ's no. Same rule as route._scope().
    _grade_universal_ids: set = set()
    # an EXPLICIT grade named in the question wins over the caller's profile grade
    # for retrieval preference/focus: "pomůcky pro 7. ročník" must anchor on 7 even
    # when the profile says 6 (the profile/question mismatch is still surfaced as a
    # note below, comparing question_grade vs the profile).
    _grade = state.get("question_grade") or state.get("user_school_grade")
    if _grade:
        try:
            with get_cursor() as _c:
                _c.execute(
                    f"SELECT id, code FROM {config.PG_SCHEMA}.resolved_code_list(%s,"
                    f" 'school_grade') WHERE code IN (%s, '9999')",
                    (state.get("legal_entity"), str(_grade)))
                for _rid, _rcode in _c.fetchall():
                    _grade_ids.add(_rid)
                    if str(_rcode) != "9999":
                        _grade_exact_ids.add(_rid)
                # every 9999 row that COVERS this ročník, from the caller's tenant
                _c.execute(
                    f"SELECT c.id FROM {config.PG_SCHEMA}.code_list c"
                    f" WHERE c.list_code = 'school_grade' AND c.is_active"
                    f"   AND c.code = '9999'"
                    f"   AND c.legal_entity_code = ANY(%s)"
                    f"   AND EXISTS (SELECT 1"
                    f"        FROM {config.PG_SCHEMA}.entity_grade_scope(c.legal_entity_code) s"
                    f"        JOIN {config.PG_SCHEMA}.code_list c2 ON c2.id = s.id"
                    f"       WHERE c2.code = %s)",
                    (list(state.get("organization_entities")
                          or [state.get("legal_entity")]), str(_grade)))
                _grade_universal_ids = {r[0] for r in _c.fetchall()}
                _grade_ids |= _grade_universal_ids
        except Exception as e:
            logger.warning("grade id resolution failed (%s) — scope off", e)

    # ── document TAXONOMY lookup: priority (type OR domain) + authority +
    #    binding audience fallback (produktový vlastník 2026-08-09: priorita typu, domény a
    #    bindingu se má zohlednit ve vyhledávání) ─────────────────────────────
    # One query over the pool's documents:
    #  * priority — the TYPE flag OR any DOMAIN flag; taxonomy_rules.is_priority
    #    is one column for both worlds, taxonomy_type_code tells them apart.
    #    Feeds the existing priority tier below, mechanics unchanged.
    #  * authority — the TYPE's sort_order (řád=10 < školní informace=40):
    #    orders WITHIN the priority tier, so two priority documents stop being
    #    a coin flip (the mobily aktualita used to beat the řád inside it).
    #  * binding — domain→(level, grades) AUDIENCE FALLBACK for chunks carrying
    #    no tags of their own; a chunk's own tags always win. Wired now, starts
    #    acting as ingest fills binding_id.
    _fus_all = {str(c.get("file_uuid")) for c in chunks if c.get("file_uuid")}
    _prio: dict = {}
    _authority: dict = {}
    _bind: dict = {}
    _doc_domains: dict = {}
    # DOCUMENT TYPES THAT DO NOT SEARCH (taxonomy_rules.search_role = 'excluded').
    # A rozcestník is the case that forced this: CERMAT's "Specifikace požadavků"
    # page is 970 characters — two paragraphs saying such documents exist and two
    # links to PDFs the corpus does not hold. On "Co se hodnotí u přijímaček?" the
    # judge put it FIRST (strongest wording overlap with the question) and the
    # answer had nothing to answer from, while the passage carrying "nejméně 60 %
    # celkového hodnocení" came fifth and phase 4 dropped it. Telling the judge to
    # skip self-describing passages did not work (migration 171, measured over 3
    # runs), so the exclusion is curatorial: the type says the document never
    # answers, and the pool drops its chunks before any ranking happens.
    _excluded_docs: set = set()
    # which entity PUBLISHED the document — the second axis of the grade tier
    # below. Taken here rather than threaded through six retrievers' chunk dicts:
    # one query already runs over the pool's documents.
    _doc_entity: dict = {}
    if _fus_all:
        try:
            with get_cursor() as _pc:
                _pc.execute(
                    f"""SELECT m.file_uuid::text,
                           COALESCE(tp.search_role, 'normal') = 'priority'
                             OR COALESCE(dp.prio, false),
                           COALESCE(tp.search_role, 'normal') = 'excluded',
                           COALESCE(tp.sort_order, 1000),
                           b.school_level, b.grades,
                           dc.codes, m.legal_entity_code
                    FROM {config.METADATA_TABLE} m
                    LEFT JOIN LATERAL (
                        SELECT tr.search_role, tr.sort_order
                        FROM {config.PG_SCHEMA}.taxonomy_rules tr
                        WHERE tr.type_code = COALESCE(m.document_type_user, m.document_type_llm)
                          AND tr.taxonomy_type_code = 'DOCUMENT_TYPE' AND tr.is_active
                        ORDER BY (tr.legal_entity_code IS NULL) DESC LIMIT 1) tp ON true
                    LEFT JOIN LATERAL (
                        SELECT bool_or(tr.search_role = 'priority') AS prio
                        FROM {config.PG_SCHEMA}.taxonomy_rules tr
                        WHERE tr.id = ANY(CASE
                                WHEN cardinality(COALESCE(m.document_domain_user, ARRAY[]::bigint[])) > 0
                                THEN m.document_domain_user ELSE m.document_domain_llm END)
                          AND tr.taxonomy_type_code = 'DOMAIN' AND tr.is_active) dp ON true
                    LEFT JOIN LATERAL (
                        SELECT array_agg(DISTINCT tr3.type_code) AS codes
                        FROM {config.PG_SCHEMA}.taxonomy_rules tr3
                        WHERE tr3.id = ANY(CASE
                                WHEN cardinality(COALESCE(m.document_domain_user, ARRAY[]::bigint[])) > 0
                                THEN m.document_domain_user ELSE m.document_domain_llm END)
                          AND tr3.taxonomy_type_code = 'DOMAIN' AND tr3.is_active) dc ON true
                    LEFT JOIN {config.PG_SCHEMA}.taxonomy_binding b
                           ON b.id = m.binding_id AND b.is_active
                    WHERE m.file_uuid = ANY(%s::uuid[]) AND m.is_active = true""",
                    [list(_fus_all)])
                for (_fu, _p, _excl, _auth, _blv, _bgr,
                     _dcodes, _lec) in _pc.fetchall():
                    _doc_entity[_fu] = _lec
                    _prio[_fu] = bool(_p)
                    if _excl:
                        _excluded_docs.add(_fu)
                    _authority[_fu] = int(_auth)
                    if _blv or _bgr:
                        _bind[_fu] = (_blv, list(_bgr or []))
                    if _dcodes:
                        _doc_domains[_fu] = set(_dcodes)
        except Exception as e:
            logger.warning("answer: taxonomy lookup failed (%s)", e)
    if _excluded_docs:
        _before = len(chunks)
        chunks = [c for c in chunks
                  if str(c.get("file_uuid")) not in _excluded_docs]
        if len(chunks) != _before:
            logger.info("answer: search_role=excluded — dropped %d chunk(s) of %d "
                        "signpost document(s)", _before - len(chunks),
                        len(_excluded_docs))
            _diag["excluded_docs"] = {"documents": len(_excluded_docs),
                                      "chunks": _before - len(chunks)}
    # ── query→domain match (produktový vlastník follow-up, 2026-08-09): deterministic keyword
    # mapping of the QUESTION onto DOMAIN rows; a chunk of a document carrying a
    # detected domain gets a SMALL additive fusion boost (domain_match_boost —
    # step param > global config->search knob > 0.05). Deliberately gentle: it
    # nudges ties, it must not reshuffle the pool.
    _query_domains: set = set()
    if bool(params.get("domain_match", True)) and _doc_domains:
        _query_domains = _detect_query_domains(
            state, float(params.get("domain_kw_max_df_share", 0.25)))
        if _query_domains:
            _diag["query_domains"] = sorted(_query_domains)
            logger.info("answer: query domains %s (docs carrying them: %d)",
                        sorted(_query_domains),
                        sum(1 for v in _doc_domains.values() if v & _query_domains))
    if _bind and bool(params.get("binding_fallback", True)):
        _code2id: dict = {}
        try:
            with get_cursor() as _c2:
                _c2.execute(f"SELECT code, id FROM {config.PG_SCHEMA}"
                            f".resolved_code_list(%s, 'school_grade')",
                            (state.get("legal_entity"),))
                _code2id = {str(r[0]): r[1] for r in _c2.fetchall()}
        except Exception as e:
            logger.warning("answer: binding grade map failed (%s)", e)
        _inherited = 0
        for c in chunks:
            _b = _bind.get(str(c.get("file_uuid")))
            if not _b:
                continue
            _blv, _bgr = _b
            _changed = False
            if _blv and not c.get("school_level"):
                c["school_level"] = _blv
                _changed = True
            if _bgr and not (c.get("school_grade_ids") or []):
                _ids = [_code2id[g] for g in _bgr if g in _code2id]
                if _ids:
                    c["school_grade_ids"] = _ids
                    _changed = True
            if _changed:
                c["audience_from_binding"] = True
                _inherited += 1
        if _inherited:
            logger.info("answer: binding audience fallback filled %d chunk(s)", _inherited)
            _diag["binding_fallback"] = _inherited

    # other-ročník safety net thresholds (see the block below)
    other_grade_top_n = int(params.get("other_grade_top_n", 2))
    min_coverage_ans = float(params.get("other_grade_min_coverage", 0.5))
    # conditional grade focus: ONLY when the asked grade is present in the pool
    # (matching = tagged with the caller grade or 9999) do we narrow to it;
    # if nothing matches, the grade is a pure boost and the pool is untouched.
    if _grade_ids and grade_scope == "focus_if_match":
        _match = [c for c in chunks
                  if _grade_ids.intersection(c.get("school_grade_ids") or ())]
        if _match:
            # narrow to the caller's grade — but KEEP grade-less chunks (empty
            # school_grade_ids = a whole-school rule that applies to everyone, e.g.
            # docházka). Drop ONLY chunks tagged for a DIFFERENT specific grade.
            # Dropping the grade-less chunks too would delete the very answer to a
            # whole-school question for any pupil who states a ročník (measured:
            # a 6th-grader asking about docházka lost the docházka chunk and got
            # only the 6th-grade supply list).
            _kept = [c for c in chunks
                     if (not (c.get("school_grade_ids") or []))
                     # a covering 9999 is UNIVERSAL, not "another ročník": dropping it
                     # here would throw away exactly what the grade round now retrieves
                     or _grade_universal_ids.intersection(c.get("school_grade_ids") or ())
                     or _grade_ids.intersection(c.get("school_grade_ids") or ())]
            # OTHER-ROČNÍK SAFETY NET (the document twin of what qa_lexical has had
            # since migration 158). The narrowing above is right, but it turns three
            # different situations into the same silence: "the school has no such
            # event", "we did not find it" and "it exists, just not for you".
            # Measured on "kdy je lyžařský výcvik?" asked by a 9th-grader: the
            # calendar row is tagged {7, 8}, the vector step DID bring it into the
            # pool, and this filter dropped it — leaving three chunks with coverage
            # 0.258 and no heading match, i.e. nothing that answers, so the pupil was
            # told there is no answer while the school does run a ski course. So when
            # what remains answers NOTHING (no heading match and every coverage below
            # min_coverage), up to other_grade_top_n of the dropped chunks come back
            # marked other_grade=True, and the prompt is told to name the ročníky
            # they apply to. Ordering puts them last, so a pool that does answer is
            # untouched.
            _drop = [c for c in chunks if c not in _kept]
            _answers = any((c.get("heading_match")
                            or float(c.get("coverage") or 0) >= min_coverage_ans)
                           for c in _kept)
            _readmit: list = []
            if _drop and not _answers:
                _drop.sort(key=lambda c: -(float(c.get("coverage") or 0)
                                           + (0.5 if c.get("heading_match") else 0)))
                for c in _drop[:other_grade_top_n]:
                    c["other_grade"] = True
                    _readmit.append(c)
                logger.info("answer: other-ročník net — nothing in the caller's own "
                            "ročník answered, re-admitting %d chunk(s) from other "
                            "ročníky", len(_readmit))
            _diag["grade_focus"] = {"grade": str(_grade), "in": len(chunks),
                                    "kept": len(_kept), "other_grade": len(_readmit)}
            logger.info("answer: grade_scope focus — %d of %d chunk(s) kept "
                        "(caller grade %s: matching + grade-less)",
                        len(_kept), len(chunks), _grade)
            chunks = _kept + _readmit

    # ── level preference (ZŠ vs SŠ) ──────────────────────────────────────────
    # A combined ZŠ+SŠ document (one školní řád) carries the SAME topic at both
    # levels (docházka pro ZŠ / docházka pro SŠ). The caller's level — from the
    # ročník (1–9 ZŠ, 10–13 SŠ) or, absent that, from their legal_entity — picks
    # the right one: drop the OPPOSITE level's chunks, but only when a same-level
    # chunk exists, and NEVER drop level-neutral chunks. This is what stops a SŠ
    # student getting the ZŠ docházka out of a shared školní řád.
    # The UNIT the caller belongs to is AUTHORITATIVE for level: a pupil enrolled
    # in a ZŠ unit is ZŠ even if a wrong ročník number says otherwise (a ZŠ pupil
    # who sends 13, or an SŠ student who types "3" meaning 3rd-year SŠ when the
    # unified axis wants 12). Only when the entity has NO level of its own — an
    # ORGANISATION spanning ZŠ+SŠ — do we read the level off the ROČNÍK, and that
    # mapping is DATA (legal_entity.grade_levels, migration 138), resolved by
    # edu_ai.grade_level(): the arithmetic that used to live here was one of three
    # copies, and it cannot express a víceleté gymnázium whose 6..9 are SŠ.
    # Entity first, grade only as a fallback: the number must never override the
    # caller's real school.
    _caller_level = None
    try:
        with get_cursor() as _c:
            _c.execute(
                f"SELECT (SELECT level FROM {config.PG_SCHEMA}.legal_entity "
                f"          WHERE legal_entity_code = %s), "
                f"       {config.PG_SCHEMA}.grade_level(%s, %s)",
                (state.get("legal_entity"), state.get("legal_entity"),
                 str(_grade) if _grade else None))
            _row = _c.fetchone() or (None, None)
            _lv = str(_row[0] or "").strip().upper()
            _caller_level = _lv if _lv in ("ZS", "SS") else None
            if _caller_level is None:
                _gl = str(_row[1] or "").strip().upper()
                _caller_level = _gl if _gl in ("ZS", "SS") else None
    except Exception as e:
        logger.warning("caller level resolution failed (%s)", e)
    if _caller_level:
        _opp = "SS" if _caller_level == "ZS" else "ZS"
        if any((c.get("school_level") or "").upper() == _caller_level for c in chunks):
            _before = len(chunks)
            chunks = [c for c in chunks
                      if (c.get("school_level") or "").upper() != _opp]
            if _before != len(chunks):
                logger.info("answer: level focus %s — dropped %d %s-level chunk(s)",
                            _caller_level, _before - len(chunks), _opp)
                _diag["level_focus"] = {"caller_level": _caller_level,
                                        "dropped": _before - len(chunks), "opposite": _opp}
    # BM25 MAGNITUDE (not rank). RRF sees only rank, so a rank-1 vs rank-2 BM25
    # winner is nearly a tie and keyword_weight ×3 decides — which kept the
    # frequency-heavy blog above the authoritative section on "docházka". BM25
    # carries a real score gap (e.g. 7.4 vs 5.1); feed it as a pool-normalised
    # term so the gap counts. It is query relevance → also counts for the source
    # cutoff (topical). document_lexical writes c["bm25"] when bm25_lexical is on.
    bm25_weight = float(params.get("bm25_weight", 0.03))
    _bm25_max = max((c.get("bm25") or 0.0) for c in chunks) if chunks else 0.0
    # GRADE near-filter (decided in the lexical/fusion phase): if a chunk carrying
    # the caller's EXACT grade is a LEXICAL hit (topically relevant — has a
    # lexical_rank, not just pulled in as a sibling/grade-recall), the grade becomes
    # a strong PROMOTER — that grade's chunks are lifted decisively (near-hard
    # filter: e.g. the 6th-grade supply list beats the grade-less přijímačky guide).
    # Otherwise the grade stays a soft boost, so a whole-school query (docházka,
    # grade-less) is not harmed. This is the "hard filter vs boost" decision.
    # HEADING match: a discriminative query concept sitting in the chunk's own
    # HEADING (document_lexical's `by_heading`, df-share gated). This is genuine
    # topical relevance — the heading says the chunk is ABOUT the query — so it
    # counts for the source cutoff too, unlike the grade/level caller bonuses.
    # It is the tie-breaker the grade near-filter alone lacks: two grade-6 chunks
    # both get the flat grade boost, but only the supply list ("Seznam pomůcek")
    # is ABOUT pomůcky; the grade-6 section of the school rules whose body merely
    # says "škola" is not. Without it the two grade-6 chunks are separated only by
    # their raw BM25 rank, and the rules section (denser body) wins the wrong way.
    heading_match_boost = float(params.get("heading_match_boost", 0.12))
    # The qualifying chunk must ALSO carry a heading match — a grade TAG plus
    # generic lexical overlap is not grade-specific EVIDENCE. Without this,
    # 'uvolnění na 14 dní?' as a 9th-grader promoted the admissions guide
    # (grade-9-tagged, full of žák/škola/dní tokens, ratio 0.9 so the
    # relevance guard held) over the grade-less řád, and the answer died.
    # The near-filter's own design comment states the separator: the heading
    # IS what tells the supply list from a section merely repeating the grade.
    # Binding-INHERITED grades are audience metadata (which pupils a document
    # is FOR), not grade-specific evidence — the admissions guide inherited
    # grade 9 from its binding and hijacked the grade tier for a 9th-grader's
    # docházka question. They still serve the grade/level FOCUS above.
    def _grade_tier(c):
        """(specifičnost ročníku, blízkost entity) — Martin, 2026-08-18.

        Two axes, compared lexicographically, so the requested order falls out
        without inventing a policy for the combinations that were not named:

            specificity  0 = the caller's exact ročník
                         1 = "all ročníky" (a 9999 row covering the caller)
                         2 = no ročník at all
            proximity    0 = the caller's own school
                         1 = an ancestor (the umbrella organisation)
                         2 = anyone else (a sibling unit publishing org-wide)

        The three cases asked for map onto (0,0) strong, (1,0) medium, (1,1) none.
        A combination that was NOT named — an organisation document tagged with the
        caller's exact ročník, which is common because a document ingested under the
        organisation stores the UNITS' ročník rows — lands at (0,1): below the
        school's own ročník match, above anything universal. That follows from the
        axes rather than from a rule invented for it.

        Ordering only, never filtering: a chunk in the last tier still answers when
        nothing better is in the pool. Constant (hence a no-op) when the caller has
        no ročník.
        """
        if not _grade:
            return (0, 0)
        _g = set(c.get("school_grade_ids") or ())
        if _grade_exact_ids & _g:
            gs = 0
        elif _grade_universal_ids & _g:
            gs = 1
        elif (not _g and _caller_level
              and (c.get("school_level") or "") == _caller_level):
            # LEVEL IS AUDIENCE. A chunk with no ročník that sits under the
            # caller's own level ("I. ZÁKLADNÍ ŠKOLA › Školní řád pro žáky") is not
            # "about no ročník" — it is about ALL ročníky of that level, which is
            # what the 9999 code means. Measured 23. 8. on "Jak požádat o
            # individuální vzdělávací plán?": the answering chunk carries level ZS
            # and no ročník, so it landed in the LAST tier and 20+ CERMAT chunks
            # tagged {5,7,9} pushed it out of the judge's reach — the same question
            # asked without a ročník answered from it. 347 chunks are in this
            # position today (141 ZŠ, 206 SŠ — for a SŠ caller that is nearly the
            # whole school rules). Ordering only, never filtering.
            gs = 1
        else:
            gs = 2
        _ent = _doc_entity.get(str(c.get("file_uuid") or ""))
        if _ent and _ent == state.get("legal_entity"):
            ep = 0
        elif _ent and _ent in (state.get("organization_entities") or []):
            ep = 1
        else:
            ep = 2
        return (gs, ep)

    def _native_grade_hit(c):
        return (not c.get("audience_from_binding")
                and _grade_exact_ids.intersection(c.get("school_grade_ids") or ()))
    _grade_hard = bool(_grade_exact_ids) and any(
        c.get("lexical_rank") and c.get("heading_match") and _native_grade_hit(c)
        for c in chunks)
    if _grade_hard:
        logger.info("answer: grade near-filter ON — grade %s has a lexical hit → "
                    "strong promote of grade-matching chunks", _grade)
    _diag["grade_near_filter"] = _grade_hard

    # PRIORITY documents (authority tier): a document whose TYPE is flagged
    # is_priority in taxonomy_rules — the school's OWN řád / informace — outranks
    # non-priority docs (external Cermat/MŠMT reference) WITHIN the same grade band.
    # One query over the pool's documents; the user type override wins over the LLM
    # type; the global taxonomy row wins over a per-entity duplicate. A priority doc
    # still has to be topically in the pool to matter (the cutoff runs on
    # fused_topical), so a blanket flag does not drag an off-topic doc in.
    # is_priority + authority were resolved in the single taxonomy lookup above
    # (type OR domain priority; authority = the type's sort_order ladder)
    _priority_tier = bool(params.get("priority_tier", True))
    for c in chunks:
        _fu = str(c.get("file_uuid"))
        c["is_priority"] = bool(_priority_tier and _prio.get(_fu, False))
        c["authority"] = _authority.get(_fu, 1000)
        c["domain_match"] = bool(_query_domains
                                 and (_doc_domains.get(_fu) or set()) & _query_domains)
    _diag["priority_docs"] = sum(1 for c in chunks if c.get("is_priority"))

    def _fused(c, topical=False):
        s = 0.0
        for key in ("lexical_rank", "vector_rank"):
            r = c.get(key)
            if r:
                s += 1.0 / (rrf_k + r)
        # the semantic step hit a DIFFERENT chunk of this chunk's element —
        # section-level lexical+vector agreement (see rag_vector's by_elem).
        # Full weight by default: it replaces the accidental full-weight
        # annotation the old element-keyed dedup used to hand out, and Q2
        # ("uvolnění na 14 dní") is answered from exactly this corroboration.
        _ver = c.get("vector_element_rank")
        if _ver and not c.get("vector_rank"):
            s += vector_element_weight / (rrf_k + _ver)
        # keyword_rank carries the heaviest weight — see keyword_weight above
        _kr = c.get("keyword_rank")
        if _kr:
            s += keyword_weight / (rrf_k + _kr)
        if _bm25_max > 0 and c.get("bm25"):
            s += bm25_weight * (c["bm25"] / _bm25_max)
        # heading topical match — genuine relevance, so it counts even in the
        # topical (source-cutoff) score, and it is the signal that separates the
        # grade-6 supply LIST from a grade-6 section of the rules (see above).
        if c.get("heading_match"):
            s += heading_match_boost
        # query domain ↔ document domain agreement — genuine topical signal
        # (the question's AREA matches what the document is filed under), so it
        # counts like heading_match does, only smaller (see domain_match_boost)
        if c.get("domain_match"):
            s += domain_match_boost
        # SINGLE SCORE (2026-08-09): the score is TOPICAL only. Caller preferences
        # (grade, level, priority) used to be additive bonuses here too — but the
        # ORDERING tiers in _sortkey took those jobs over (grade round + near-filter
        # tier, level focus drops the opposite level outright, priority tier), and
        # within a tier the bonus is a constant that orders nothing. Dropping the
        # additive copies makes fused ≡ fused_topical: one number to read in the
        # logs, and the source cutoff runs on the same score that ranks. The
        # `topical` parameter is kept for call-site compatibility.
        return s

    # TWO TIERS: everything that was MATCHED first, ordered by the fusion, and
    # only then the siblings that were invited along with it. Ranking siblings
    # among the matches by their anchor's position let one large section fill the
    # whole context with its own continuation and push the actual answer out —
    # measured on "Muze se ve skole fotit?", where four chunks of the Cermat
    # application guide displaced the chunk that answers the question. Evidence
    # earns a place in the prompt; context only fills what is left.
    # Ordering tiers (only when the grade near-filter is ON — a known grade with a
    # grade-specific lexical hit): (1) evidence before siblings; (2) the caller's
    # exact grade before everything else — "ročníkové chunky mají prioritu"; (3)
    # WITHIN that grade, a chunk whose HEADING states a discriminative query concept
    # before one that merely carries the grade — the supply LIST ("Seznam pomůcek",
    # heading_match) beats the grade-6 section of the rules whose body just repeats
    # "škola". rrf_k is 1 in production, so a rank-1-vs-2 gap (0.5 vs 0.33) dwarfs
    # any additive boost; only a hard tier lets the right chunk win. Grade unknown /
    # near-filter off → the plain fused order, unchanged.
    # RELEVANCE GUARD on the priority tier: authority decides between COMPARABLY
    # relevant documents — it must not override an order-of-magnitude relevance
    # gap. Measured failure: "Jak se připravit na přijímačky" put the school's own
    # o-studiu page (best fused 0.26 — výsledky/termíny, nothing about preparing)
    # above eduin's preparation guide (0.53) purely on the priority bit. The tier
    # therefore stays active only while the best PRIORITY chunk reaches
    # priority_min_ratio of the best non-priority chunk; below that the pool is
    # ordered by relevance alone. "Uvolnění žáka" keeps its tier (the řád is also
    # relevance-competitive there); the tangential-authority case loses it.
    _pr_ratio = float(params.get("priority_min_ratio", 0.5))
    _priority_active = any(c.get("is_priority") for c in chunks)
    if _priority_active:
        _pmax = max((_fused(c) for c in chunks if c.get("is_priority")), default=0.0)
        _nmax = max((_fused(c) for c in chunks if not c.get("is_priority")), default=0.0)
        if _nmax > 0 and _pmax < _pr_ratio * _nmax:
            _priority_active = False
            logger.info("answer: priority tier OFF — best priority chunk %.3f under "
                        "%.0f%% of best non-priority %.3f (relevance guard)",
                        _pmax, _pr_ratio * 100, _nmax)
        _diag["priority_tier"] = {"active": _priority_active,
                                  "best_prio": round(_pmax, 4),
                                  "best_nonprio": round(_nmax, 4)}

    # (The GLOBAL grade_min_ratio guard was removed 2026-08-10 — subsumed by
    # the heading-match qualification; ablation-proven identical results.)
    # PER-CHUNK grade-tier guard (2026-08-10, produktový vlastník's Q4/9 case): a native-
    # grade chunk may jump ahead of grade-less chunks only while it is
    # relevance-COMPARABLE — the eduin "Kdy a kde se budou konat" logistics
    # section (grade 9, fused 0.44) must not displace the grade-less
    # "Hodnocení zkoušky" (0.56) that actually details the answer. Unlike the
    # removed global guard this acts per chunk: strong grade chunks keep the
    # tier, weak ones fall back to plain fused order.
    _best_gradeless = max((_fused(c) for c in chunks if not _native_grade_hit(c)),
                          default=0.0)
    _g_chunk_ratio = float(params.get("grade_chunk_min_ratio", 0.8))

    def _sortkey(c):
        # tiers, compared left→right: evidence before siblings; then (only under the
        # grade near-filter) the caller's exact grade; then PRIORITY documents (own
        # authoritative řád/informace before external reference) — grade OUTER,
        # priority INNER; then a heading-topical match; then the fused score. The
        # priority tier fires even with NO grade (then it is the primary tier), which
        # is what lets the school's own uvolnění řád beat the external Cermat guide on
        # a grade-less query. rrf_k=1 in prod makes score gaps large, so only a hard
        # tier (not an additive boost) orders authority reliably. The tier is subject
        # to the relevance guard above (_priority_active).
        sib = 0 if not c.get("is_sibling") else 1
        p = 0 if (_priority_active and c.get("is_priority")) else 1
        # WITHIN the priority tier, the type's sort_order is the authority
        # ladder (řád=10 < informace=40): a binding rule outranks a news page
        # even though both carry the priority flag. Outside the tier the
        # component is a constant and orders nothing.
        # ORDER MATTERS vs the heading tier: under the grade near-filter the
        # heading match comes FIRST — the supply LIST ("Seznam pomůcek",
        # heading IS the query) must beat the grade-6 section of the rules
        # regardless of the rules' higher type authority (measured regression:
        # authority placed before h made the řád paragraph lead the pomůcky
        # answer). Authority then breaks heading ties. The grade-less branch
        # has no heading tier, so authority decides there (N13 řád vs news).
        a = c.get("authority", 1000) if p == 0 else 1000
        # GRADE TIER (2026-08-18): was a binary "exact ročník or not", and it only
        # applied under the near-filter. Now the two-axis tier above, so the order
        # own+ročník > own+všechny > organizace+všechny > bez ročníku holds even when
        # NO chunk carries the exact ročník — which is precisely the case the binary
        # version could not express, because it never fired.
        gt = _grade_tier(c)
        if _grade_hard:
            # the near-filter's guard survives: an exact-ročník chunk that scores far
            # below the best grade-less one does not get to lead on its tag alone.
            if gt[0] == 0 and _fused(c) < _g_chunk_ratio * _best_gradeless:
                gt = (2, gt[1])
            h = 0 if (gt[0] == 0 and c.get("heading_match")) else 1
            return (sib, gt, p, h, a, -_fused(c), -(c.get("score") or 0.0))
        return (sib, gt, p, a, -_fused(c), -(c.get("score") or 0.0))
    if any(_fused(c) for c in chunks):
        chunks = sorted(chunks, key=_sortkey)
        for c in chunks:
            c["fused"] = round(_fused(c), 6)
            c["fused_topical"] = round(_fused(c, topical=True), 6)
    else:
        chunks = sorted(chunks, key=lambda c: c.get("score", 0.0), reverse=True)

    # The TIER order's top chunk, kept before the reranker reorders the pool: the
    # citation-ratio bar below can then be anchored on the score the tiers judged
    # best instead of on whatever the reranker promoted (see source_ratio_anchor).
    _tier_first = chunks[0] if chunks else None
    # WHO DECIDES THE ORDER. The tier order is computed by SQL and fusion, so it is
    # the same on every run; the judge's permutation is free-form LLM output and is
    # NOT. Measured 2026-08-23 on three identical runs of "Co se hodnotí u
    # přijímaček" (log 5416/5417/5418): identical pool, identical fused scores,
    # identical judge positions 1-2 — and from position 3 down three different
    # orders, so the third seeded document differed, and the chunk carrying the
    # 60 % rule ("Hodnocení zkoušky") reached the context in ONE run of the three.
    # The answer did not choose between facts; two runs never saw the fact.
    # Keeping the index lets the veto stand while the ORDER goes back to the tiers.
    _tier_pos = {id(c): i for i, c in enumerate(chunks)}

    # ── THIN CHUNKS: short AND matched nothing but the embedding ─────────────
    # Measured 2026-08-23 on "Co se hodnotí u přijímaček?" (log 5255): two chunks
    # of an MŠMT form appendix — "HODNOCENÍ NA VYSVĚDČENÍCH Z PŘEDCHOZÍHO
    # VZDĚLÁVÁNÍ" (50 chars) and "Povinné předměty, povinně volitelné předměty"
    # (44) — were promoted by the JUDGE to positions 1 and 2 with grade 2, and the
    # answer was built from an empty form. Fusion had them LAST of the top six
    # (0.117 and 0.193): it did not fail, so the guard does not belong there.
    #
    # "No fact" is deliberately NOT tested on digits: "Ředitelem školy je Jan
    # Novák." is 29 chars with no digit and answers "kdo je ředitel". What those
    # appendix chunks actually had in common is that they reached the pool through
    # the VECTOR ALONE, covering not one concept of the query (cov=None, lex=None,
    # concepts_covered=[]) — thinness is a relation to THIS query, not a property
    # of the chunk, which is also why it cannot be a stored column.
    # The row_NN_column_NN prefix is stripped first: table cells like
    # "row_02_column_05: Evidenční číslo:" are mostly coordinates, and a naive
    # length (or digit) test reads them as substantial.
    _thin_max = int(params.get("thin_max_chars") or 120)
    _thin_digit = bool(params.get("thin_require_digit", False))

    def _is_thin(c) -> bool:
        _t = _RE_CELL.sub("", c.get("chunk_text") or "")
        _t = _RE_MARKUP.sub(" ", _t).strip()
        if len(_t) >= _thin_max:
            return False
        if c.get("concepts_covered"):
            return False                      # covered a concept of the query
        if (c.get("lexical_rank") or c.get("keyword_rank") or c.get("coverage")):
            return False                      # came in lexically or by keywords
        if _thin_digit and re.search(r"\d", _t):
            return False
        return True

    for c in chunks:
        c["thin"] = _is_thin(c)
    _thin_ids = [c.get("id") for c in chunks if c.get("thin")]
    if _thin_ids:
        _diag["thin"] = {"n": len(_thin_ids), "ids": _thin_ids[:20]}
        logger.info("answer: %d thin chunk(s) in the pool (short + vector-only): %s",
                    len(_thin_ids), _thin_ids[:20])


    # ── cross-encoder rerank of the fused top-K (optional) ───────────────────
    # The cross-encoder reads (query, chunk) TOGETHER and reorders the strongest
    # candidates by true relevance — the semantic call the lexical/vector/keyword
    # signals each miss (blog ABOUT docházka vs the RULES; school pomůcky vs exam
    # pomůcky). It reorders the SEED priority (which document answers); the source
    # cutoff below still runs on fused_topical. Off by default; on model-load
    # failure rerank_scores returns None and the fusion order is kept.
    # global default config.RERANK_MODE (off|local|cloud); a step/request param
    # overrides it. `rerank_mode` is the ONLY spelling — the older `rerank` bool +
    # `rerank_backend` pair was removed (no settings row, no script and no request
    # ever set them, so it was two extra names for one knob).
    _rr_mode = str(params.get("rerank_mode") or config.RERANK_MODE or "off").lower()
    # THREE MODES, not two: `local` is the in-process bge cross-encoder, `cloud`
    # the LLM listwise reranker, `jina` the hosted cross-encoder. The first and
    # the third return a SCORE, the second a permutation — which is why only the
    # score modes can carry a veto threshold (rerank_score_min below).
    _rr_on = _rr_mode in ("local", "cloud", "jina")
    _rr_backend = {"cloud": "openai", "jina": "jina"}.get(_rr_mode, "local")
    # OUTPUT MODE (rerank_output): "order" is what has always run — the listwise
    # permutation, used ONLY to reorder the top-K. "graded" asks the same single
    # call for a per-passage VERDICT (2 answers / 1 same topic / 0 unrelated) plus
    # the rank, because the permutation's derived scores are positions in disguise
    # ("6.0" = eleventh of sixteen, not "weak") and no threshold can be written on
    # them. The verdict then does two things the permutation cannot: it VETOES a
    # passage, and it lets a 2nd+ document be admitted on "brings an answer"
    # instead of on a score ratio. Tiers, roles, grades and validity are untouched
    # — the reranker knows nothing about them.
    _rr_out = str(params.get("rerank_output") or "order").lower()
    # what the reranker gets to see per passage. A parameter because the cut
    # decides what it can judge, and a VETO on a third of a chunk is a different
    # kind of mistake than a reorder on it.
    _rr_chars = int(params.get("rerank_passage_chars") or (900 if _rr_out == "graded" else 500))
    # veto floor: never let the verdicts strip the pool below this
    _rr_keep = int(params.get("rerank_min_keep") or 6)
    # what a 2nd+ document must bring to be cited (2 = an answer, 1 = same topic)
    _rr_req_grade = int(params.get("rerank_require_grade") or 2)
    # PHASES 4 AND 5 MUST HEAR THE RERANKER IN BOTH MODES. In "order" the only
    # thing it can say is a position, so a 2nd+ document is cited only when its
    # best chunk sits within the top N of the reranked order. Measured on 31
    # questions: legitimately cited second documents sat at positions 2-10, the
    # balast of "můžeme ve škole fotit?" at 11-12 — hence a default well past
    # what the context can even hold. 0 turns the gate off.
    _rr_seed_top_n = int(params.get("rerank_seed_top_n") or 12)
    # WHAT THE VERDICT IS ALLOWED TO DECIDE. With grade ordering on (today's
    # behaviour) the score is verdict-major, so the whole grade-1 block sits below
    # the whole grade-2 block. Measured 22. 8.: gpt-4o-mini hands out exactly ONE
    # grade 2 per query whatever the prompt says (1 of 8, 1 of 20, and 1 of 20
    # again after the prompt was rewritten to say a partial fact counts and the
    # number of 2s is unlimited) — it reads "2" as "the best passage". Ordering by
    # that means one chunk on top and everything else in one undifferentiated
    # heap. What the judge IS reliable at is the other end: on "fotit" it gave 0 to
    # fifteen of sixteen passages and was right about every one. So the verdict can
    # be used for the veto ALONE: rerank_grade_ordering=false scores by rank like
    # the order mode, and grade 0 only drops.
    _rr_grade_ordering = bool(params.get("rerank_grade_ordering", True))
    # WHICH model judges. Until now the only piece of the reranker still living in
    # docker-compose (RERANK_OPENAI_MODEL) while its mode sat in settings and its
    # prompt in prompt_registry — so the one hypothesis the measurements kept
    # pointing at, "the judge is simply a weaker model than the one writing the
    # answer", could not be tested without a rebuild. Empty = env fallback.
    _rr_model = (params.get("rerank_model") or "").strip() or None
    _rr_effort = (params.get("rerank_reasoning_effort") or "low").strip() or None
    # score backends only: a passage BELOW this is dropped from the pool, the same
    # job the graded verdict does for the LLM backend — except a calibrated score
    # can carry a number, a permutation position cannot. 0 disables the veto and
    # the backend only reorders. Measured on "můžeme ve škole fotit?": the right
    # rule 0.3224, the topically adjacent competition 0.0125, the rest negative.
    _rr_score_min = float(params.get("rerank_score_min") or 0.0)
    _rr_timeout = float(params.get("rerank_timeout_s") or 15)
    # WHO EVEN REACHES THE JUDGE. The slice used to be the pool's first rerank_top_k,
    # and the pool is in TIER order — so the ročník tier decides who is judged. On
    # "Jak požádat o individuální vzdělávací plán?" asked by a parent of a 5th-grader,
    # the answering chunk (school rules, Článek I, "mají právo požadovat vypracování
    # individuálního vzdělávacího plánu") carries NO ročník, because it applies to
    # everyone — and 20+ CERMAT admission chunks tagged {5,7,9} sat above it, so the
    # judge never saw it and the answer came back empty. Asked WITHOUT a ročník the
    # same question answered from that very chunk, which is absurd from outside:
    # stating your ročník made the answer worse.
    # So the slice is INTERLEAVED: rerank_grade_top_n places go to chunks carrying
    # the caller's exact ročník, the rest to everyone else (grade-less = whole-school
    # rules, and universal 9999), each group in its own tier order. Unused places in
    # either group fall to the other, so a pool without ročníks behaves exactly as
    # before. 0 restores the plain top-k slice.
    _rr_grade_top_n = int(params.get("rerank_grade_top_n") or 12)
    # Does the judge's permutation reorder the pool (true, as always) or only veto
    # from it (false)? See the comment on _tier_pos above for the measurement.
    _rr_reorder = bool(params.get("rerank_reorder", True))
    _rr_graded = False
    _rr_judged_any = False      # did the reranker actually rank anything?
    if _rr_on and len(chunks) > 1:
        from ..rerank import rerank_scores, rerank_graded, log_passages, log_order
        _rk = int(params.get("rerank_top_k") or config.RERANK_TOP_K)
        # THIN chunks are not offered to the judge at all. It reads only
        # heading_path + text, so it cannot tell an empty form row from a fact and
        # has been measured grading such rows 2 ("answers the question"). Keeping
        # them out also frees their places in the top-K for passages nobody saw.
        _thin_judge = bool(params.get("thin_guard_judge", True))
        _jpool = ([c for c in chunks if not c.get("thin")]
                  if _thin_judge else chunks)
        if _thin_judge and len(_jpool) < len(chunks):
            _diag.setdefault("thin", {})["not_judged"] = len(chunks) - len(_jpool)
        # ── PREFILTER: WHO EVEN REACHES THE JUDGE (optional) ──────────────
        # The slice below is POSITIONAL — tier order plus a ročník interleave —
        # so on a large pool it is position, not content, that decides who gets
        # judged. Measured 2026-08-27 over 514 logged queries: median pool 15,
        # p90 42, max 75, against rerank_top_k = 20. On roughly a third of the
        # queries 20+ chunks were therefore never read by anything. A hosted
        # cross-encoder scores the WHOLE pool for a fraction of the judge's cost,
        # so it picks the slice and the judge keeps the two decisions it is
        # measurably better at: the veto (grade 0) and the order.
        #
        # IT NEVER REORDERS, and that is not caution but a measurement: as a
        # ranker jina-reranker-v3.5 put "pf 2026" above the events calendar on
        # "kdy je nejbližší škola v přírodě" (fused 0.257 vs 1.020), the focus
        # gate then computed its focus from that seed and REJECTED the answering
        # document. As a prefilter the same bad model still kept that chunk —
        # it sat at position 4 of 8. Ordering is where a cheap model destroys an
        # answer; selection is where it can only help.
        #
        # Three guards, in this order:
        #   pool <= rerank_top_k → NO CALL AT ALL. The judge would see the whole
        #     pool anyway, so the only thing a prefilter could do there is be
        #     wrong. On the logged distribution this alone skips ~64 % of queries.
        #   max_in caps what is sent (in tier order), so the bill has a ceiling
        #     however large a pool the fusion hands over.
        #   keep_anchor keeps the tier's own #1 whatever the vendor thinks of it —
        #     without it a foreign model could drop the chunk our own fusion rates
        #     best, which is exactly the failure that costs half a day to find.
        _pf = params.get("rerank_prefilter") or {}
        _pf_backend = str(_pf.get("backend") or "").lower()
        if _pf_backend == "jina" and len(_jpool) > _rk:
            from ..rerank import jina_scores
            _pf_in = _jpool[:max(_rk, int(_pf.get("max_in") or 40))]
            _pf_docs = [((c.get("heading_path") or "") + "\n"
                         + (c.get("chunk_text") or "")).strip() for c in _pf_in]
            _pf_s = jina_scores(state.get("user_question") or "", _pf_docs,
                                passage_chars=int(_pf.get("passage_chars") or 400),
                                model=(_pf.get("model") or None),
                                timeout=float(_pf.get("timeout_s") or 10))
            if _pf_s:
                for _c, _v in zip(_pf_in, _pf_s):
                    _c["pf_raw"] = round(float(_v), 4)
                _pf_by = sorted(_pf_in, key=lambda c: -(c.get("pf_raw") or 0.0))
                _pf_keep = _pf_by[:int(_pf.get("top_n") or 12)]
                # raw_min is an ABSOLUTE threshold and it is only meaningful on a
                # model whose scores are calibrated: jina-reranker-v3 gives
                # off-topic passages a NEGATIVE score, so 0.0 has a meaning. On
                # v3.5 everything is positive and a floor there vetoes nothing.
                # Never let it empty the pool — the judge decides, not the filter.
                if _pf.get("raw_min") is not None:
                    _pf_above = [c for c in _pf_keep
                                 if (c.get("pf_raw") or 0.0) >= float(_pf["raw_min"])]
                    # FLOOR, same idea as rerank_min_keep on the judge's veto:
                    # measured on "Seznam pomůcek na školní rok 2026/2027" the
                    # threshold cut 28 candidates to 2 (the third already scored
                    # −0.0171). The answer held, but two chunks leave no room for
                    # a second citation and no room to be wrong — and a threshold
                    # that can starve the judge is worse than one that lets three
                    # weak passages through, because the judge can still say 0.
                    _pf_floor = int(_pf.get("min_keep") or _rr_keep)
                    _pf_keep = (_pf_above if len(_pf_above) >= _pf_floor
                                else _pf_keep[:max(_pf_floor, len(_pf_above))])
                _pf_kept = {id(c) for c in _pf_keep}
                if bool(_pf.get("keep_anchor", True)):
                    _pf_kept.add(id(_jpool[0]))
                _pf_before = len(_jpool)
                _jpool = [c for c in _pf_in if id(c) in _pf_kept]  # tier order
                _diag["rerank_prefilter"] = {
                    "backend": "jina", "model": _pf.get("model") or "(env)",
                    "pool": _pf_before, "sent": len(_pf_in), "kept": len(_jpool),
                    "raw_top3": [c.get("pf_raw") for c in _pf_by[:3]],
                    "raw_min": _pf.get("raw_min")}
                logger.info("answer: prefilter jina — %d of %d sent, %d kept "
                            "(pool %d)", len(_pf_in), _pf_before, len(_jpool),
                            len(chunks))
            else:
                # fail-open, like every other reranker path: the positional slice
                # below runs exactly as it does today
                _diag["rerank_prefilter"] = {"backend": "jina", "failed": True,
                                             "sent": len(_pf_in)}
        if _rr_grade_top_n and _grade_exact_ids:
            _with = [c for c in _jpool
                     if _grade_exact_ids.intersection(c.get("school_grade_ids") or ())]
            _without = [c for c in _jpool
                        if not _grade_exact_ids.intersection(c.get("school_grade_ids") or ())]
            _n_with = min(_rr_grade_top_n, len(_with), _rk)
            _n_without = min(_rk - _n_with, len(_without))
            # whatever the ročník group did not use goes back to the ročník group
            _n_with = min(len(_with), _rk - _n_without)
            _picked = _with[:_n_with] + _without[:_n_without]
            _seen_ident = {(id(c)) for c in _picked}
            _cand = [c for c in _jpool if id(c) in _seen_ident]   # keep tier order
            _diag["rerank_slice"] = {"grade": _n_with, "other": _n_without,
                                     "pool": len(chunks)}
            logger.info("answer: rerank slice interleaved — %d with ročník + %d "
                        "without (pool %d)", _n_with, _n_without, len(chunks))
        else:
            _cand = _jpool[:_rk]
        # ── ONE CONTRACT FOR EVERY BACKEND ─────────────────────────────────
        # Three backends speak three languages: the LLM judge answers with a
        # verdict (2/1/0) plus a permutation, Jina with a score that MOVES WITH
        # THE BATCH (measured 2026-08-27: the same passage scored +0.099 alone,
        # −0.022 beside a strong one, +0.047 among weak ones), the local bge with
        # a cross-encoder score. Downstream needs only two things — an ORDER and
        # a keep/drop — so every backend is mapped onto the same four fields here
        # and phases 4 and 5 never learn which one ran:
        #
        #   rr_pos   1..n, always defined (order is the one thing all three give)
        #   rr_rel   0..1, strength WITHIN the batch — min-max normalised
        #   rr_keep  survived the veto
        #   rr_raw   the native value, diagnostics only, never a decision
        #
        # The threshold is therefore RELATIVE (rerank_rel_min), not absolute: it
        # moves with the batch exactly as a listwise score does, so the batch
        # dependence cancels out. Same pattern as source_min_ratio for a 2nd
        # citation. What a relative threshold can NEVER say is "nothing here is
        # good enough" — the best passage always scores 1.0. That question belongs
        # to the topical gate in document_lexical, not to the reranker.
        def _rr_contract(ordered, raw_of, keep_of, rel_of, prune=True):
            """Stamp the shared fields; `prune` says whether the POOL shrinks.

            The graded branch must NOT prune: it already applies its own veto
            (grade > 0) WITH the min_keep floor, and the seed gate then asks per
            DOCUMENT. Pruning here as well collapsed a 6-chunk pool to 1 — the
            floor put everyone back and the contract immediately threw five out
            again, which is the opposite of what the floor is for."""
            _vals = [rel_of(c) for c in ordered]
            _lo, _hi = (min(_vals), max(_vals)) if _vals else (0.0, 0.0)
            _span = (_hi - _lo) or 0.0
            for _i, _c in enumerate(ordered, 1):
                _c["rr_pos"] = _i
                # all equal (or a single passage) → no discrimination to express
                _c["rr_rel"] = round(1.0 if _span == 0 else
                                     (rel_of(_c) - _lo) / _span, 4)
                _c["rr_raw"] = raw_of(_c)
                _c["rr_keep"] = bool(keep_of(_c))
            _min_rel = float(params.get("rerank_rel_min") or 0.0)
            if _min_rel and prune:
                _above = [c for c in ordered if c["rr_rel"] >= _min_rel]
                if len(_above) < _rr_keep:      # floor: never veto into a hole
                    _above = ordered[:max(_rr_keep, len(_above))]
                _abv = {id(c) for c in _above}
                for _c in ordered:
                    _c["rr_keep"] = _c["rr_keep"] and id(_c) in _abv
            return [c for c in ordered if c["rr_keep"]] if prune else ordered

        _judged = {id(c) for c in _cand}
        _docs = [((c.get("heading_path") or "") + "\n" + (c.get("chunk_text") or "")).strip()
                 for c in _cand]
        if bool(params.get("rerank_log_passages", False)):
            log_passages(_docs, _rr_chars, _cand)
        _verdicts = (rerank_graded(state.get("user_question") or "", _docs,
                                   passage_chars=_rr_chars, model=_rr_model,
                                   effort=_rr_effort)
                     if (_rr_out == "graded" and _rr_backend == "openai") else None)
        if _verdicts:
            _rr_graded = True
            _rr_judged_any = True
            _n = len(_cand)
            for c, v in zip(_cand, _verdicts):
                c["rerank_grade"] = v["grade"]
                # verdict-major, rank-minor in ONE number, so the ordering below
                # (and every reader of `rerank`) needs no change. With grade
                # ordering off the score is the position alone — the verdict then
                # only decides who is dropped, never who leads.
                c["rerank"] = round((v["grade"] * 1000 if _rr_grade_ordering else 0)
                                    + (_n - v["rank"]), 4)
            _ordered = sorted(_cand, key=lambda c: -(c.get("rerank") or 0))
            for _i, _c in enumerate(_ordered, 1):
                _c["rerank_pos"] = _i
            _survivors = [c for c in _ordered if (c.get("rerank_grade") or 0) > 0]
            if len(_survivors) < _rr_keep:          # floor: never veto into a hole
                _survivors = _ordered[:max(_rr_keep, len(_survivors))]
            _dropped = len(_ordered) - len(_survivors)
            # THE JUDGE VETOES, THE TIERS ORDER. With rerank_grade_ordering=false
            # the verdict already only decides who is dropped — but the `rerank`
            # score is then the judge's POSITION, and phases 4 and 5 read the pool
            # in that order, so the judge led anyway, by permutation instead of by
            # grade. rerank_reorder=false finishes the thought: survivors go back
            # into tier order and the run-to-run coin flip disappears.
            if not _rr_reorder:
                # …but the VERDICT still leads when rerank_grade_ordering is on.
                # Measured on "kdy je lyžařský výcvik?" (log 5491): fusion put two
                # unrelated chunks (0.530 "Zdroje informací", 0.509 školní řád) above
                # the calendar row that answers (0.500), and the judge was the only
                # step that saw it — it gave the calendar the sole grade 2. Dropping
                # the permutation outright therefore killed the answer: the veto
                # could not help (min_keep 6 keeps everyone when the pool holds 3),
                # so the first paragraph was seeded from junk. A grade is 2/1/0 and
                # reproduces across runs; a 20-item permutation does not. So the
                # grade CLASS orders, the tiers order WITHIN it, the permutation is
                # ignored.
                _survivors = sorted(
                    _survivors,
                    key=lambda c: (-(c.get("rerank_grade") or 0) if _rr_grade_ordering
                                   else 0, _tier_pos.get(id(c), 10 ** 6)))
                for _i, _c in enumerate(_survivors, 1):
                    _c["rerank_pos"] = _i      # position in the ORDER that now holds
            # REJOIN BY IDENTITY, NOT BY INDEX. The interleaved slice picks from the
            # WHOLE pool, so a judged chunk can sit past `_rk` — `chunks[_rk:]` then
            # returned it a second time (twice in the context, phase 5 has no dedup)
            # while an unjudged chunk before `_rk` vanished from the pool without ever
            # being seen. Measured on "Co se hodnotí u přijímaček?": slice 14+6 out of
            # a pool of 46, so all six "other" chunks came from beyond the cut.
            # the contract, computed on the ORDER that actually holds: rel from
            # the verdict (grade/2), keep from the verdict the veto already applied
            _survivors = _rr_contract(
                _survivors,
                raw_of=lambda c: {"grade": c.get("rerank_grade"),
                                  "score": c.get("rerank")},
                keep_of=lambda c: (c.get("rerank_grade") or 0) >= _rr_req_grade,
                rel_of=lambda c: float(c.get("rerank_grade") or 0) / 2.0,
                prune=False)
            chunks = _survivors + [c for c in chunks if id(c) not in _judged]
            _diag["rerank"] = {"mode": "graded", "model": _rr_model or "(env)",
                               "reorder": _rr_reorder,
                               "ordering": "grade" if _rr_grade_ordering else "rank",
                               "judged": _n, "vetoed": _dropped,
                               "grades": {g: sum(1 for c in _ordered
                                                 if (c.get("rerank_grade") or 0) == g)
                                          for g in (2, 1, 0)}}
            logger.info("answer: graded rerank of top-%d — vetoed %d, grades %s",
                        _n, _dropped, _diag["rerank"]["grades"])
            if bool(params.get("rerank_log_passages", False)):
                log_order(_ordered, _docs)
        else:
            _scores = rerank_scores(state.get("user_question") or "", _docs,
                                    backend=_rr_backend, passage_chars=_rr_chars,
                                    model=_rr_model, effort=_rr_effort,
                                    timeout=_rr_timeout)
            if _scores:
                for c, sc in zip(_cand, _scores):
                    c["rerank"] = round(float(sc), 4)
                _ordered = sorted(_cand, key=lambda c: -(c.get("rerank") if c.get("rerank")
                                                         is not None else -1e9))
                for _i, _c in enumerate(_ordered, 1):
                    _c["rerank_pos"] = _i
                # rel and keep both from the score; rerank_score_min stays as an
                # ABSOLUTE escape hatch for a backend whose scale is stable
                # (bge is; Jina is not — hence rerank_rel_min in the contract).
                _kept = _rr_contract(
                    _ordered,
                    raw_of=lambda c: c.get("rerank"),
                    keep_of=lambda c: (not _rr_score_min
                                       or (c.get("rerank") or -1e9) >= _rr_score_min),
                    rel_of=lambda c: float(c.get("rerank") or 0.0))
                if len(_kept) < _rr_keep:           # floor: never veto into a hole
                    _kept = _ordered[:max(_rr_keep, len(_kept))]
                    for _c in _kept:
                        _c["rr_keep"] = True
                _vetoed = len(_ordered) - len(_kept)
                chunks = _kept + [c for c in chunks if id(c) not in _judged]
                _diag["rerank"] = {"mode": "order", "judged": len(_cand),
                                   "backend": _rr_backend,
                                   "score_min": _rr_score_min or None,
                                   "rel_min": float(params.get("rerank_rel_min") or 0) or None,
                                   "vetoed": _vetoed,
                                   "rel_top3": [c.get("rr_rel") for c in _kept[:3]],
                                   "model": _rr_model or "(env)"}
                if bool(params.get("rerank_log_passages", False)):
                    log_order(_ordered, _docs)
                _rr_judged_any = True
                logger.info("answer: cross-encoder reranked top-%d", len(_cand))

    # documents relevance score (0..1) for the query log: coverage for
    # lexical/keywords chunks, else the raw score (vector = cosine). Its meaning
    # is disambiguated by strategies_text in the log.
    # DIAGNOSTIC: the full fused candidate pool (top-N) with every per-signal rank,
    # so the query log shows WHERE the expected chunk landed and what outranked it —
    # the recall/ranking view the selected context_chunks alone cannot give.
    _cand_n = int(params.get("log_candidates_n") or 20)
    _diag["pool_size"] = len(chunks)
    _diag["candidates"] = [{
        "file_item_uuid": str(c.get("file_item_uuid")), "chunk_index": c.get("chunk_index"),
        "section": _section_label(c), "retrieval_type": c.get("retrieval_type"),
        "lexical_rank": c.get("lexical_rank"), "keyword_rank": c.get("keyword_rank"),
        "vector_rank": c.get("vector_rank"), "bm25": c.get("bm25"), "rerank": c.get("rerank"),
        "rerank_grade": c.get("rerank_grade"), "rerank_pos": c.get("rr_pos"),
        "pf_raw": c.get("pf_raw"),
        "rr_rel": c.get("rr_rel"), "rr_keep": c.get("rr_keep"),
        "coverage": c.get("coverage"), "fused": c.get("fused"),
        "fused_topical": c.get("fused_topical"), "school_level": c.get("school_level"),
        "grades": [str(g) for g in (c.get("school_grade_ids") or [])],
        "heading_match": c.get("heading_match"),
        "priority": c.get("is_priority"),
        # what the focus gate reads — without it in the log a gate that found no
        # anchor is indistinguishable from a gate whose input was never labelled
        "concepts_covered": c.get("concepts_covered"),
    } for c in chunks[:_cand_n]]

    _top = chunks[0]
    _doc_score = _top.get("coverage")
    if _doc_score is None:
        _doc_score = _top.get("score")

    # Context keeps MULTIPLE chunks per document (up to CONTENT_TOP_N chunks in
    # total) so the LLM sees real content, not just the best-matching heading/TOC
    # chunk. The user-facing sources list is deduplicated per DOCUMENT (top
    # SOURCES_TOP_N links). Document identity = source_url, else the clean
    # original_file_name, else file_name / file_uuid.
    def _doc_key(c):
        return (c.get("source_url") or c.get("original_file_name")
                or c.get("file_name") or c.get("file_uuid"))

    # How much the model gets to see is config, like the model and the output cap
    # beside it: params (settings) -> env -> code default. These three
    # decide WHAT the answer is built from — content_top_n is why a chunk ranked
    # 6th never reaches the prompt at all — so they do not belong in a constant
    # that needs an image rebuild to change.
    content_top_n = int(params.get("content_top_n") or config.CONTENT_TOP_N)
    sources_top_n = int(params.get("sources_top_n") or config.SOURCES_TOP_N)
    token_budget = int(params.get("token_budget") or config.TOKEN_BUDGET)
    # Characters per token — how the token budget becomes a character budget.
    # ~4 holds for English and for Czech without diacritics; Czech WITH
    # diacritics and the other UI languages run lower, so a corpus in one of
    # those overfills the prompt at 4 and the model silently truncates.
    char_budget = int(token_budget * float(params.get("chars_per_token") or 4))
    parts, sources, used = [], [], 0
    selected: list[dict] = []      # chosen chunks, re-ordered for the prompt below
    # what ACTUALLY reached the prompt, for the trace: the pool can hold 30
    # chunks while only CONTENT_TOP_N of them are shown to the model, and
    # reporting the pool size as "the answer was built from N chunks" is simply
    # false — the question "which chunks composed this answer" has to be
    # answerable from the trace alone.
    context_chunks: list[dict] = []
    _ctx_meta: list[dict] = []
    # Chunk identity is (file_item_uuid, chunk_index). Deduping on the uuid
    # alone treated a whole SECTION as one chunk, so of a list split across three
    # chunks the prompt could only ever see one of them — which is precisely the
    # case document_siblings exists to fix.
    seen_items: set = set()
    doc_rank: dict = {}          # doc key -> document number in the context

    # SELECTION IN TWO PHASES.
    #
    # Phase 1 — SEEDS. The best-ranked chunks, taken in fused order, decide WHICH
    # documents the answer is built from and in WHAT ORDER: a document's position is
    # the position of its best chunk. `content_top_n` bounds the seeds.
    #
    # Phase 2 — FILL. For each of those documents, its remaining chunks that are
    # already in the shortlist are pulled in, so a document is answered from
    # everything the retrievers found in it rather than from the single fragment
    # that happened to rank highest. Bounded per document
    # (`max_chunks_per_document`) and by the character budget.
    #
    # Why: on "jaké potřebuji pomůcky?" the shortlist held the CERMAT exam rules and
    # the school's own per-grade supply lists, interleaved by rank. The model got
    # alternating fragments of two different questions and blended them into one
    # paragraph — a permitted exam pen next to tempera paints. Grouping is what lets
    # the prompt ask for one paragraph per document.
    max_per_doc = int(params.get("max_chunks_per_document") or 6)

    # SEEDS pick the DOCUMENTS, so the cut has to be per document, not a flat top-N
    # of chunks. A flat cut made sources_top_n unreachable: on "jaké potřebuji
    # pomůcky?" the five best chunks all belonged to two entrance-exam documents
    # ("8. Pravidla, povolené pomůcky a den D" has "pomůcky" densely, so ts_rank_cd
    # 3.6 against 1.8), and the school's own supply lists — same heading hit, same
    # concept coverage, candidates 6 to 10 — missed the cut by ONE place. The
    # setting said three documents may be cited; the seeding could never reach the
    # third.
    #
    # Depth is bounded so a document whose best chunk is far down the pool does not
    # earn a paragraph: content_top_n chunks per document slot. Every candidate here
    # already passed document_lexical's gate, so depth is the only guard needed.
    seed_docs: list = []         # doc keys in the order their best chunk appeared
    # How deep into the ranked pool a document may still be found and earn its own
    # paragraph. Derived by default (content_top_n x sources_top_n = 15), but it is
    # a policy: it is exactly this number that decided whether the school's supply
    # lists — candidates 6 to 10 — reached the answer at all.
    _scan_depth = int(params.get("seed_scan_depth") or content_top_n * sources_top_n)
    # relative source cutoff (precision): a SECOND+ document earns a paragraph only
    # if its best chunk scores within `source_min_ratio` of the top document's best.
    # Without it, "pomůcky pro 6. ročník" cited the school řád (Článek I, generic
    # "žáci … řádné docházení") as document 2 on a weak "žák" match — far below the
    # supply-list #1. Relative, not absolute, so it travels across embedding models
    # and query lengths. Scored on the SAME key the pool was sorted by (fused when
    # any chunk carried a rank, else the raw score).
    _min_ratio = float(params.get("source_min_ratio", 0.5))
    # WHOSE score sets the bar. Until now: whatever chunk stands FIRST in the pool
    # — which, with the reranker on, is the chunk the RERANKER promoted, not the one
    # the tiers ranked best. Measured on "Jak škola předchází šikaně?": the reranker
    # put a chunk with fused_topical 0.153 first while the pool held 0.396, so the
    # bar fell to 0.077 and the ratio gate stopped filtering anything. The anchor is
    # therefore a parameter:
    #   tier_first — the top chunk of the TIER order, captured before the reranker
    #                reorders (the score the tiers themselves judged),
    #   pool_first — the old behaviour (first in the current pool order),
    #   pool_max   — the strongest topical score anywhere in the pool. Careful:
    #                fused mixes signals, so on "fotit" the pool maximum WAS the
    #                off-topic career document (0.5) — a bar set by balast.
    _ratio_anchor = str(params.get("source_ratio_anchor") or "tier_first").lower()
    _thin_seed = bool(params.get("thin_guard_seed", True))

    def _rel(c):
        # cutoff on the TOPICAL score (query relevance only): a document earns a
        # 2nd+ citation on its own topical merit, never because a caller-grade or
        # caller-level bonus lifted an otherwise off-topic chunk past the bar.
        if c.get("fused_topical") is not None:
            return c["fused_topical"]
        return c["fused"] if c.get("fused") is not None else (c.get("score") or 0.0)

    # FOCUS GATE (step 3): the score ratio above measures HOW MUCH of the query a
    # document matched, never WHICH PART. On "Co se hodnotí u přijímaček?" the
    # eduin guide reached 90 % of the top purely on the TOPIC ("přijímačky" in its
    # heading) while containing the question's ASPECT ("hodnotí") nowhere — and
    # earned a motivational paragraph in the answer. The gate anchors the aspect on
    # the TOP document's qualifying chunk: its covered RARE concepts (df share ≤
    # focus_max_df, carried by document_lexical as concepts_covered) become the
    # focus, and a 2nd+ document is admitted only if some chunk of its own speaks
    # of at least one of them. Anchoring on document #1 rather than on the query's
    # rarest token is deliberate: measured on this corpus, "přijímačka" (0.4–1.6 %)
    # is RARER than "hodnotit" (1.2–2.3 %) because documents write "přijímací
    # zkouška" — the naive rarest-token rule would have let the eduin guide pass.
    # Safety: no rare concept on the top chunk → gate off; document #1 never gated.
    _focus_gate = bool(params.get("focus_gate", True))
    _focus_max_df = float(params.get("focus_max_df", 0.05))
    _focus: list = []

    def _fold_txt(s: str) -> str:
        return "".join(ch for ch in unicodedata.normalize("NFD", (s or "").lower())
                       if unicodedata.category(ch) != "Mn")

    # ALL focus concepts are required (focus_require="all"), not any: the top
    # chunk of Q4 covers BOTH "hodnotit" (aspect) and "prijimac" (topic — rare in
    # this corpus too, documents write "přijímací zkouška"), and with any-of the
    # eduin guide slipped back in on the topic alone. Requiring the full set is
    # what actually encodes "the same aspect OF the same topic"; the concepts a
    # candidate document may satisfy anywhere in its pooled chunks (union).
    _focus_require = str(params.get("focus_require", "all"))

    def _doc_has_focus(doc_key) -> bool:
        _have: set = set()
        for x in chunks:
            if _doc_key(x) != doc_key:
                continue
            _have.update(t for t, _sh in (x.get("concepts_covered") or [])
                         if t in _focus)
            # vector/keyword chunks carry no concepts_covered — fall back to a
            # folded substring test over the chunk's own text + headings
            _txt = _fold_txt(" ".join(str(x.get(k) or "") for k in
                                      ("chunk_text", "heading_path")))
            _have.update(t for t in _focus if t in _txt)
            if _focus_require != "all" and _have:
                return True
            if set(_focus) <= _have:
                return True
        return (bool(_have) if _focus_require != "all"
                else set(_focus) <= _have)

    _top_rel = None
    if _ratio_anchor == "tier_first" and _tier_first is not None:
        _top_rel_anchor = _rel(_tier_first)
    elif _ratio_anchor == "pool_max":
        _top_rel_anchor = max((_rel(c) for c in chunks), default=None)
    else:
        _top_rel_anchor = None                 # pool_first: the loop sets it below
    _diag["source_ratio"] = {"anchor": _ratio_anchor,
                             "bar": (round(_min_ratio * _top_rel_anchor, 4)
                                     if _top_rel_anchor else None)}
    _seen_seed: set = set()
    _thin_seeds: list = []       # the ones the guard held back, for the floor below
    for c in chunks:
        ident = (c.get("file_item_uuid"), c.get("chunk_index"))
        if ident in _seen_seed:
            continue
        _seen_seed.add(ident)
        if len(_seen_seed) > _scan_depth or len(seed_docs) >= sources_top_n:
            break
        # A THIN chunk may not REPRESENT its document. `continue`, not a rejection
        # of the document: if the document holds a real chunk further down the pool,
        # that one seeds it and is judged on its own score. What this stops is an
        # empty form row founding a paragraph — and, when the anchor is pool_first,
        # a chunk with concepts_covered=[] silently emptying the focus set below
        # and switching the focus gate off for the whole query.
        if _thin_seed and c.get("thin"):
            _diag.setdefault("thin", {}).setdefault("not_seed", []).append(c.get("id"))
            logger.info("answer: thin chunk %s may not seed a document", c.get("id"))
            _thin_seeds.append(c)
            continue
        key = _doc_key(c)
        if key not in seed_docs:
            _r = _rel(c)                       # this is the doc's best chunk (sorted)
            if _top_rel is None:
                # the bar: the chosen anchor, else the #1 document (pool_first)
                _top_rel = _top_rel_anchor if _top_rel_anchor is not None else _r
                if _focus_gate:
                    _focus = [t for t, _sh in (c.get("concepts_covered") or [])
                              if _sh is not None and _sh <= _focus_max_df]
                    _diag["focus_gate"] = {"focus": _focus, "rejected": []}
            elif _top_rel > 0 and _r < _min_ratio * _top_rel:
                # the ONLY gate here that used to reject silently — and it can
                # reject the chunk that actually answers: on "Jak požádat o
                # individuální vzdělávací plán?" the bar was 0.31 (half of a
                # topically strong CERMAT chunk about exam accommodations) and the
                # school-rules chunk carrying the answer scored 0.28, so it missed
                # by 0.03 and the answer became no_answer with no trace of why.
                _diag.setdefault("source_ratio", {}).setdefault("rejected", []).append(
                    f"{str(key)[:60]} ({_r:.4f} < {_min_ratio * _top_rel:.4f})")
                logger.info("answer: source ratio — document %s at %.4f below the"
                            " bar %.4f; not seeded", key, _r, _min_ratio * _top_rel)
                continue                       # too far below #1 → not a cited source
            elif (_rr_seed_top_n and (c.get("rr_pos") or 0) > _rr_seed_top_n):
                _diag.setdefault("rerank", {}).setdefault("rejected", []).append(
                    f"{str(key)[:60]} (poz {c.get('rr_pos')})")
                logger.info("answer: rerank position %s beyond top-%d — document %s "
                            "not seeded", c.get("rr_pos"), _rr_seed_top_n, key)
                continue
            elif _rr_on and _rr_judged_any and not any(
                    x.get("rr_keep") for x in chunks
                    if _doc_key(x) == key and x.get("rr_pos")):
                # BACKEND-BLIND: the verdict used to be read as a grade, which only
                # the LLM backend produces. rr_keep is the same statement in the
                # shared contract, so a cross-encoder can veto too — through
                # rerank_rel_min instead of through a grade.
                _diag.setdefault("rerank", {}).setdefault("rejected", []).append(str(key)[:80])
                logger.info("answer: rerank verdict — document %s brings no "
                            "answer; not seeded", key)
                continue
            elif _focus and not _doc_has_focus(key):
                _diag.setdefault("focus_gate", {}).setdefault("rejected", []).append(str(key)[:80])
                logger.info("answer: focus gate — document %s matched the topic but "
                            "none of the focus concepts %s; not seeded", key, _focus)
                continue
            seed_docs.append(key)
    # FLOOR: never let the thin guard answer nothing. A short chunk that came in on
    # the embedding alone is usually an empty form row — but not always: "Ředitelem
    # školy je Jan Novák." is 29 chars and, asked as "kdo vede školu", covers no
    # query concept lexically either. Barring it costs nothing while some other
    # document answers, and everything when none does. So if the pass seeded NO
    # document, the best held-back chunk seeds one after all (one, not all: the
    # point is to answer, not to fill the answer with form rows).
    if _thin_seed and not seed_docs and _thin_seeds:
        _fk = _doc_key(_thin_seeds[0])
        seed_docs.append(_fk)
        _diag.setdefault("thin", {})["floor"] = _thin_seeds[0].get("id")
        logger.info("answer: thin floor — no document seeded, falling back to thin "
                    "chunk %s (document %s)", _thin_seeds[0].get("id"), _fk)
    # the shortlist, regrouped: document order from the seeds, reading order inside
    _by_doc: dict = {}
    for c in chunks:
        _by_doc.setdefault(_doc_key(c), []).append(c)
    _section_order: dict = {}
    for i, c in enumerate(chunks):
        _section_order.setdefault(
            (c.get("output_row_number"), c.get("file_item_uuid")), i)
    # FILL prefers chunks covering the question's FOCUS concepts: the per-doc
    # cap used to cut by section relevance order alone, and a 0.03 cosine
    # jitter (the grade-enriched embedding of 'Pro 7. ročník: …') let the
    # přestávky section displace the uvolnění text that actually answers.
    # Ordering preference only — never a gate. The substring test allows a
    # 2-char stem mismatch (focus 'uvolneny' vs text 'uvolnění').
    def _chunk_has_focus(c) -> bool:
        if not _focus:
            return False
        if any(t in _focus for t, _sh in (c.get("concepts_covered") or [])):
            return True
        _txt = _fold_txt(" ".join(str(c.get(k) or "") for k in
                                  ("chunk_text", "heading_path")))
        return any(t[:max(4, len(t) - 2)] in _txt for t in _focus)

    ordered: list = []
    for key in seed_docs:
        # ORDER INSIDE A DOCUMENT: the reranker's opinion first — but by ELEMENT,
        # not by chunk. A split list ("Seznam pomůcek" chunk 0,1,2) must stay in
        # reading order or the prompt gets it shuffled, so an element is ranked by
        # its BEST chunk and its chunks then follow chunk_index. Without a rerank
        # verdict this falls back to focus-first + reading order as before.
        _grp = _by_doc.get(key) or []
        _elem_best: dict = {}
        for _c in _grp:
            _e = _c.get("file_item_uuid")
            _p = _c.get("rr_pos") or 10 ** 6
            if _e not in _elem_best or _p < _elem_best[_e]:
                _elem_best[_e] = _p
        group = sorted(
            _grp,
            key=lambda c: (0 if _chunk_has_focus(c) else 1,
                           _elem_best.get(c.get("file_item_uuid"), 10 ** 6),
                           _section_order[(c.get("output_row_number"),
                                           c.get("file_item_uuid"))],
                           c.get("chunk_index") if c.get("chunk_index")
                           is not None else 0))
        ordered.extend(group[:max_per_doc])

    for c in ordered:
        fiu = c.get("file_item_uuid")
        ident = (fiu, c.get("chunk_index"))
        if ident in seen_items:
            continue
        key = _doc_key(c)
        # a chunk from a NEW document beyond the top-N shown sources would be
        # uncitable — skip it and keep filling from the top documents
        if key not in doc_rank and len(doc_rank) >= sources_top_n:
            continue
        if key not in doc_rank:
            doc_rank[key] = len(doc_rank) + 1
            title, link = source_display(c)
            sources.append({
                "rank": doc_rank[key],
                "title": title,             # user-friendly name (frontend link text)
                "url": link,                # clickable link (source_url) or None
                "file_uuid": c.get("file_uuid", ""),
                "file_item_uuid": fiu,      # first (highest-scored) chunk of the doc
                "file_name": c.get("file_name", ""),
                "original_file_name": c.get("original_file_name"),
                "source_url": c.get("source_url"),
            })
        header = _ctx_header(c, doc_rank[key])
        text = c.get("chunk_text", "") or ""
        remaining = char_budget - used - len(header)
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining] + "\n[...truncated]"
        c["_ctx_text"] = text
        selected.append(c)
        used += len(header) + len(text)
        seen_items.add(ident)
        _ctx_meta.append({
            "chunk_id": c.get("chunk_id"),
            "file_item_uuid": fiu,
            "chunk_index": c.get("chunk_index"),
            "doc": doc_rank[key],
            "title": (sources[doc_rank[key] - 1]["title"] if doc_rank[key] <= len(sources)
                      else c.get("file_name")),
            "retrieval_type": c.get("retrieval_type"),
            "score": c.get("score"),
            "fused": c.get("fused"),
            "lexical_rank": c.get("lexical_rank"),
            "keyword_rank": c.get("keyword_rank"),
            "vector_rank": c.get("vector_rank"),
            "cosine": c.get("cosine"),
            # the reranker's verdict on THIS chunk: position in the reranked order
            # (what the seed gate and the in-document order below consult) and the
            # raw score/grade behind it. Without them the trace showed a prompt
            # order the reranker had shaped and no way to see that it had.
            "rerank": c.get("rerank"),
            "rerank_pos": c.get("rr_pos"),
            "rr_rel": c.get("rr_rel"),
            "rerank_grade": c.get("rerank_grade"),
            "coverage": c.get("coverage"),
            # which concept let this chunk through the heading gate (None = it
            # passed on coverage alone) — without it the diagnostics cannot tell
            # the two doors apart, and the log's by_heading count has no detail
            "heading_match": c.get("heading_match"),
            "chars": len(text),
            "section": _section_label(c),
            "preview": (text[:160] + "…") if len(text) > 160 else text,
        })

    # The prompt order IS the selection order above: documents in seed order,
    # sections in the order they were found, chunks by chunk_index inside a section
    # so a list split across chunks reads 0,1,2 instead of in pieces.

    parts = []
    by_ident = {(m["file_item_uuid"], m.get("chunk_index")): m for m in _ctx_meta}
    for c in selected:
        header = _ctx_header(c, doc_rank[_doc_key(c)])
        parts.append(header + (c.get("_ctx_text") or ""))
        m = by_ident.get((c.get("file_item_uuid"), c.get("chunk_index")))
        if m:
            # position in the PROMPT — the rank the model actually sees, which
            # neither the fused score nor the pool order states directly
            m["prompt_rank"] = len(context_chunks) + 1
            context_chunks.append(m)
    # testing/eval: which chunks entered the LLM, with their fusion statistics —
    # the flow_trace carries the full dicts; this line makes it greppable
    logger.info("answer: context → LLM: %s",
                [f"#{m['prompt_rank']} chunk {m.get('chunk_id')} (doc {m['doc']}, "
                 f"fused={m.get('fused')}, cov={m.get('coverage')}, "
                 f"lex={m.get('lexical_rank')}, kw={m.get('keyword_rank')}, "
                 f"vec={m.get('vector_rank')}, via {m.get('retrieval_type')})"
                 for m in context_chunks])

    context_text = "\n\n".join(parts)
    sources_json = json.dumps(sources, ensure_ascii=False)
    user_query = state.get("user_question", "")

    # Deterministic answer-language control: prepare() resolved the query
    # language (validated /search filter > shared detection). An explicit
    # instruction at the END of the user message is followed far more reliably
    # by small models (nano) than the general system-prompt language rule.
    # The language NAME comes from the registry's labels jsonb, so a new
    # language needs no name map here (it used to be {"cs": …, "en": …}).
    # lang is None when detection could not tell: then we say NOTHING about the
    # language instead of ordering the model into one — the model matches the
    # question's language on its own, and a wrong explicit instruction is worse
    # than none. The system prompt still carries the general language rule.
    lang = state.get("query_language")
    lang_instruction = f"\n\nRespond in {language_name(lang)}." if lang else ""

    # ── date + grade context (produktový vlastník, 2026-08-05) ─────────────────────────────
    # The answer must SAY which grade the used passages apply to, and relative
    # time ("letos", "příští školní rok") must resolve against the real date.
    # The model only states what it is given here — prompt v5, GRADE VALIDITY.
    from datetime import date as _date
    meta_lines = [f"Today's date: {_date.today().isoformat()}"]
    _profile_g = state.get("user_school_grade")
    _question_g = state.get("question_grade")
    _differ = (_profile_g and _question_g
               and str(_profile_g) != str(_question_g))
    _noted = bool(_differ and _mismatch_noted_before(state))
    # An ALREADY-NOTED mismatch must not reach the model AT ALL: given the two
    # grade lines it writes the note on its own initiative, NOTE or not
    # (measured — the anti-nag guard was bypassed by the model's helpfulness).
    if _profile_g and not _noted:
        meta_lines.append(f"Caller's grade (profile): {_profile_g}")
        # ONE ROW, SEVERAL GRADES. A school calendar writes the whole school into
        # one line: "OZP | 2. stupeň 11.-15.5. (Stará Živohošť) 1., 2. třída
        # 18.-22.5. (Krkonoše) 3.-5. třída 8.-12.6." Measured on "Kdy je nejbližší
        # škola v přírodě?" asked by a 5th-grader's parent: the model answered with
        # the 2. stupeň date — the first variant in the row — although the line for
        # 3.-5. třída sat two clauses later. Knowing the grade is not enough; it has
        # to be told to pick inside the passage.
        meta_lines.append(
            f"NOTE: when a passage lists SEVERAL variants by grade or stage "
            f"(1./2. stupeň, ranges of třída, per-ročník dates), answer with the "
            f"variant that applies to grade {_profile_g} and say which grades it "
            f"applies to. Never report another grade's variant as the answer; if "
            f"the passage has no variant for grade {_profile_g}, say which grades "
            f"it does cover. This is NOT a grade mismatch (the question names no "
            f"other grade), so add no mismatch note and no VARIANT line — left "
            f"unsaid, the model wrote one anyway, comparing grade 5 with grade 5.")
    if _question_g and not _noted:
        meta_lines.append(f"Grade the question asks about: {_question_g}")
    _mismatch: tuple | None = None
    if _differ and not _noted:
        # the model skipped the mismatch note when left to compare the two
        # numbers itself (measured on gpt-4.1-mini) — so the code compares and
        # instructs, the model only phrases it. Noted at most ONCE per
        # conversation (_mismatch_noted_before) — a user asking three
        # 7th-grade questions in a row must not be nagged three times.
        _mismatch = (str(_question_g), str(_profile_g))
        meta_lines.append(
            f"NOTE: the question asks about grade {_question_g} but the "
            f"caller's profile says grade {_profile_g} — per GRADE VALIDITY "
            f"rule 2, add one italic sentence pointing this out, and append "
            f"the final VARIANT line rephrasing the question for grade "
            f"{_profile_g}.")
    try:
        _gids = {i for c in selected for i in (c.get("school_grade_ids") or ())}
        if _gids:
            with get_cursor() as _c:
                _c.execute(f"SELECT id, code FROM {config.PG_SCHEMA}.code_list "
                           "WHERE list_code = 'school_grade' AND id = ANY(%s)",
                           (list(_gids),))
                _id2code = dict(_c.fetchall())
            _doc_grades: dict = {}
            # OTHER-ROČNÍK passages get their OWN line. Their ročníky are the whole
            # point of the class, and the lens below would delete every one of them
            # (none is the caller's) — the note then silently took its grades from a
            # SIBLING chunk of the same document. Measured on "kdy je lyžařský
            # výcvik?" asked by a 6th-grader: the answering row is tagged {7,8}, the
            # note claimed "6. ročník" because the adaptační-kurz row of the same
            # calendar carries {6,7}. The answer text was right (the model read the
            # ročníky out of the passage); the deterministic note under it lied.
            _other_grades: dict = {}
            for c in selected:
                codes = {_id2code.get(i) for i in (c.get("school_grade_ids") or ())}
                codes.discard(None)
                if c.get("other_grade") and codes:
                    _other_grades.setdefault(doc_rank[_doc_key(c)], set()).update(codes)
                    continue
                # a chunk enumerating EVERY grade 1–9 individually is a
                # whole-school rule that merely lacks the 9999 tag (školní řád
                # docházka) — reporting it as the caller's grade ("týká se 6.
                # ročníku") misstates its validity; normalise to all-grades
                if {str(i) for i in range(1, 10)} <= codes:
                    codes = (codes - {str(i) for i in range(1, 10)}) | {"9999"}
                # when a grade lens is active, report ONLY the asked grade (or the
                # genuine all-grades 9999) — not every grade a chunk happens to
                # co-carry. A popular grade (7 is co-tagged on národní testování,
                # přijímačky, …) would otherwise make the note collapse to "all
                # grades" even though the answer is about that one grade.
                if _grade and codes:
                    codes = {cd for cd in codes if cd == str(_grade) or cd == "9999"}
                if codes:
                    _doc_grades.setdefault(doc_rank[_doc_key(c)], set()).update(codes)
            if _doc_grades:
                def _gsort(v):
                    # 9999 (all-grades) last, real grades in numeric order
                    return sorted(v, key=lambda x: (x == "9999",
                                                    int(x) if x.isdigit() else 0))
                meta_lines.append(
                    "Grades the context passages apply to: " + "; ".join(
                        f"document {n}: {', '.join(_gsort(v))}"
                        for n, v in sorted(_doc_grades.items())))
            if _other_grades:
                meta_lines.append(
                    "Passages that belong to ANOTHER ročník than the caller's: "
                    + "; ".join(f"document {n}: {', '.join(_gsort(v))}"
                                for n, v in sorted(_other_grades.items()))
                    + " — name those ročníky when you use such a passage.")
    except Exception as e:
        logger.warning("answer: grade meta lines failed (%s) — omitted", e)
    meta_instruction = "\n\n" + "\n".join(meta_lines)

    # DETERMINISTIC PARAGRAPH SCAFFOLD, for the same reason the language rule sits
    # here rather than in the system prompt: a small model follows an explicit
    # instruction at the END of the user message far more reliably than a general
    # rule above the context. Asking the system prompt for "one paragraph per
    # document" was ignored by gpt-4.1-nano AND gpt-4.1-mini on "jaké potřebuji
    # pomůcky?" — both wrote a single paragraph and silently dropped one of the two
    # documents, which is a wrong answer, not a shorter one. Here the code already
    # KNOWS how many documents are in the context and what each is called, so it
    # states the shape instead of asking for it.
    # The label lists EVERY section of that document present in the context, not
    # just the first. Naming only the first anchored the model to it: the school
    # document contributed the 2025/26 first-grade stub and four chunks of the
    # 2026/2027 lists, and a label reading "…2025/2026 - 1. stupeň" produced a
    # paragraph about the first grade alone while the four lists sat unused.
    _sections: dict = {}
    _excerpts: dict = {}
    for c in selected:
        n = doc_rank[_doc_key(c)]
        h = c.get("section_heading") or c.get("file_name") or ""
        _sections.setdefault(n, [])
        if h and h not in _sections[n]:
            _sections[n].append(h)
        # the source text behind this paragraph, in the order the model read it, so
        # a client can show what the answer was actually built from. Headings are
        # written in because they are not part of chunk_text since migration 61 and
        # without them a bare "4x 544, 2x 460 …" is unreadable on its own.
        _head = _section_label(c)
        _excerpts.setdefault(n, []).append(
            (f"### {_head}\n" if _head else "") + (c.get("_ctx_text") or ""))
    _labels = [(n, "; ".join(hs[:4])) for n, hs in _sections.items()]

    # Returning the source text per paragraph is opt-in: it multiplies the response
    # size by roughly the size of the context, which a mobile client on a slow link
    # does not want unless it offers the "show the source" affordance.
    include_excerpt = bool(params.get("include_excerpt", True))
    excerpt_max_chars = int(params.get("excerpt_max_chars") or 4000)
    excerpt_cut_ratio = float(params.get("excerpt_cut_ratio") or 0.6)
    group_instruction = ""
    if len(_labels) > 1:
        # Paragraph label = the DOCUMENT'S OWN HEADER (Martin, 2026-08-09): the
        # heading of the document's best chunk names the content better than a
        # model-invented situation ("Práva a povinnosti pedagogických pracovníků"
        # beats "Během školního roku"). A NULL or one-word header ("Aktuálně",
        # "VÝSLEDKY") names nothing — those fall back to the model's own label.
        # A document contributing SEVERAL sections is labeled by what the
        # sections SHARE — the longest common heading_path prefix ("Školní řád
        # pro žáky"), not the best chunk's own section: the paragraph may be
        # written from another of its sections (measured: mlčenlivost content
        # under a "Docházka do školy" label). A single-section document keeps
        # its specific heading ("Třída – 6.").
        _doc_headers: dict = {}
        _doc_paths: dict = {}
        _doc_first: dict = {}
        for c in selected:
            n = doc_rank[_doc_key(c)]
            _doc_first.setdefault(n, c)
            _p = [s.strip() for s in re.sub(r"\s+", " ", c.get("heading_path") or "")
                  .split("›") if s.strip()]
            if _p:
                _doc_paths.setdefault(n, []).append(_p)
        for n in {doc_rank[_doc_key(c)] for c in selected}:
            _h = ""
            paths = _doc_paths.get(n) or []
            if paths:
                pref = paths[0]
                for _p in paths[1:]:
                    k = 0
                    while k < min(len(pref), len(_p)) and pref[k] == _p[k]:
                        k += 1
                    pref = pref[:k]
                _h = (pref[-1] if pref else "").strip(" #*")
            if len(_h.split()) < 2:
                _c0 = _doc_first[n]
                _h = re.sub(r"\s+", " ", (_c0.get("subsection_heading")
                                          or _c0.get("section_heading") or "")).strip(" #*")
            # this LABEL is what the paragraph opens with in bold, so it gets the
            # same trim as everything else user-facing. Deliberately AFTER the
            # word count: the number should not decide whether a document is
            # labelled at all, only how the label reads.
            _doc_headers[n] = _strip_list_numbering(_h) if len(_h.split()) >= 2 else None
        _lines = "\n".join(
            f"{n}) {lab}\n   LABEL: "
            + (f"\"{_doc_headers[n]}\"" if _doc_headers.get(n) else "(write your own)")
            for n, lab in sorted(_labels))
        # ONE COMMAND, NOT TWO. This used to open with "Write exactly N
        # paragraphs, one per document" and only at the END add the exception
        # that a paragraph must be EARNED — a self-contradiction, and the model
        # follows the strong concrete count at the top. Measured on "můžeme ve
        # škole fotit?": four identical runs, identical retrieval, identical
        # context (3 documents) — three answers used one document, the fourth
        # obediently wrote three paragraphs including career counselling and a
        # student interview. The balast paragraph was not the model's idea; the
        # prompt asked for it. The earning rule is now the primary instruction
        # and the count is a ceiling. Downstream is already built for fewer
        # paragraphs than documents: attribution is content-based and the cited
        # sources are filtered to the documents a paragraph actually used.
        group_instruction = (
            f"\n\nThe context holds {len(_labels)} documents — CANDIDATES, not a "
            f"checklist. A paragraph is EARNED by contributing answer content: "
            f"write one ONLY for a document that answers the question or a part "
            f"of it, in the order listed. FEWER paragraphs than documents is the "
            f"normal case and ONE is a perfectly good answer; never add a "
            f"paragraph to reach a count. At most {len(_labels)}. A document that "
            f"merely LACKS the asked information gets no paragraph and no mention "
            f"at all (rule 4: never say what is missing). Do not merge two "
            f"documents into one paragraph. If NO document earns a paragraph, "
            f"reply with the NO-ANSWER PROTOCOL alone — and such a reply carries "
            f"no grade note line either.\n"
            f"What each document covers, and the LABEL its paragraph opens with:"
            f"\n{_lines}\n"
            f"Open every paragraph with its LABEL in **bold** (markdown) followed "
            f"by a colon — UNLESS your answer turns out to be a SINGLE paragraph, "
            f"which carries NO label at all and opens with the answer itself. The "
            f"label exists only to tell paragraphs APART; with one paragraph there "
            f"is nothing to tell apart, and the label then merely announces the "
            f"section it was taken from — measured on \"Co se hodnotí u "
            f"přijímaček?\" (log 5388/5395), where a single paragraph about the "
            f"60 %% weighting opened with \"Kdy se jednotná přijímací zkouška bude "
            f"konat\" and promised the reader dates. A quoted LABEL is used EXACTLY "
            f"as written — never reworded. Where it says (write your own), name the SITUATION "
            f"yourself in 2 to 5 words of the reader's language — the shape is "
            f"\"Při přijímacích zkouškách:\" or \"Během školního roku:\", never a "
            f"document's file name or numbering. The LABEL only NAMES the "
            f"paragraph — it does not choose its content: ANSWER ORDER still "
            f"applies, so the first sentence AFTER the label directly answers "
            f"the question.")

    # ── suggestions: what the user could ask NEXT ────────────────────────────
    # Every item's `question` is a text the client can send straight back to
    # /search — the client needs no semantics beyond "render, tap, resend".
    #
    #   clarify   the context spans documents of DIFFERENT domains (the school's
    #             own rules vs the entrance-exam rules both answer "jaké potřebuji
    #             pomůcky?"). The answer is still generated — whether to show it
    #             or to ask first is the chat's decision, so the clarification
    #             rides beside the answer instead of replacing it. Each option is
    #             {label, question}: label = the domain's taxonomy name to SHOW,
    #             question = the best section heading of that domain's document to
    #             SEND. Not "<question> — <label>": the label's words are not in
    #             the corpus, and appending them was measured to dilute coverage
    #             until the resent query answered nothing.
    #   related   sections of the answering documents that the retrievers matched
    #             but the context had no room for — headings are their own search
    #             signal, so sent as a query they find their section.
    suggestions: list[dict] = []
    _sugg_top_n = int(params.get("related_suggestions_top_n") or 3)
    # A clarify is only honest while the question is genuinely open. At full
    # coverage one reading already answered it — "Kdy jsou přijímačky?" came back
    # with the dates AND a request to pick a domain. Suppressed from this score
    # up; the no_answer end is closed by without_clarify(), so what is left is
    # the middle, which is what the clarification was for.
    _clarify_cap = params.get("clarify_max_match_score")
    _clarify_cap = 1.0 if _clarify_cap is None else float(_clarify_cap)
    _confident = _doc_score is not None and float(_doc_score) >= _clarify_cap
    if _confident:
        logger.info("answer: clarify suppressed — match_score %.4f >= %.4f",
                    float(_doc_score), _clarify_cap)
    try:
        _doc_uuids = [s["file_uuid"] for s in sources if s.get("file_uuid")]
        _type_of: dict = {}
        if len(_doc_uuids) > 1 and not _confident:
            with get_cursor() as cur:
                cur.execute(
                    f"SELECT file_uuid::text, "
                    f"       coalesce(document_type_user, document_type_llm) "
                    f"FROM {config.METADATA_TABLE} "
                    f"WHERE file_uuid::text = ANY(%s) AND is_active", (_doc_uuids,))
                _type_of = {r[0]: r[1] for r in cur.fetchall() if r[1]}
                _distinct = list(dict.fromkeys(
                    _type_of[u] for u in _doc_uuids if u in _type_of))
                if len(_distinct) > 1:
                    cur.execute(
                        f"SELECT type_code, labels FROM "
                        f"{config.PG_SCHEMA}.taxonomy_rules WHERE is_active AND taxonomy_type_code = 'DOCUMENT_TYPE'")
                    _tax = {r[0]: (r[1] or {}) for r in cur.fetchall()}
                    _ui = ui_default_language()
                    # question per domain = the best section heading of the
                    # best-ranked document of that type that can stand alone as a
                    # query (_usable_as_query); a domain with no such heading
                    # contributes no option, and a clarify with a single option
                    # left is no clarification at all
                    _opts = []
                    for tc in _distinct:
                        _lbl = (_tax.get(tc) or {})
                        label = _lbl.get(lang or "") or _lbl.get(_ui) or tc
                        for s in sources:  # rank order
                            if _type_of.get(s.get("file_uuid")) != tc:
                                continue
                            _heads = _sections.get(s["rank"]) or []
                            _q = next((q for q in (_as_query(x, lang) for x in _heads)
                                       if q), None)
                            if _q is None:
                                # the document itself still narrows the search
                                _q = _as_query(s.get("title"), lang)
                            if _q:
                                _opts.append({"label": label, "question": _q})
                            else:
                                logger.info(
                                    "answer: clarify option for %s dropped — no heading "
                                    "usable as a query (%s)", tc, _heads[:3])
                            break
                    if len(_opts) > 1:
                        suggestions.append({
                            "kind": "clarify",
                            "question": system_message("clarify_domains", lang),
                            "options": _opts,
                        })
        # sections that were retrieved for the answering documents but did not
        # reach the context — the nearest unread material
        _used_headings = {h for hs in _sections.values() for h in hs}
        _seen_rel: set = set()
        for c in chunks:
            if len(_seen_rel) >= _sugg_top_n:
                break
            if _doc_key(c) not in seed_docs:
                continue
            if (c.get("file_item_uuid"), c.get("chunk_index")) in seen_items:
                continue
            h = c.get("section_heading")
            if not h or h in _used_headings:
                continue
            # sent as the next query the same way a clarify option is, so it has
            # to survive the same test: "Článek XI" finds every article XI in the
            # corpus, not this section. Dedupe on the SENT text — "8. Pravidla"
            # and "9. Pravidla" trim to one question, not two buttons.
            _q = _as_query(h, lang)
            if not _q or _q in _seen_rel:
                continue
            _seen_rel.add(_q)
            suggestions.append({"kind": "related", "question": _q,
                                "file_uuid": c.get("file_uuid")})
    except Exception as e:
        # suggestions are an offer, never the answer — they must not cost one
        logger.warning("answer: building suggestions failed (%s) — none returned", e)

    # model + output cap are settings params on the `answer` step
    # (config-driven like every other gate); env/config values are the fallback.
    # Output tokens dominate generation latency, so max_tokens is the main knob
    # that keeps document answers in the ~2 s range.
    model = str(params.get("model") or config.ANSWER_MODEL)
    max_tokens = int(params.get("max_tokens") or config.ANSWER_MAX_TOKENS)
    # Only for the reasoning tiers (GPT-5.6+). Absent → the model runs as before.
    reasoning_effort = params.get("reasoning_effort") or None
    # None → llm.py's own default. Ignored by the reasoning tiers, which reject it.
    temperature = params.get("temperature")

    # SYNONYMS MUST REACH THE PROMPT, not only the retrieval. Measured on "Kdy je
    # nejbližší škola v přírodě?": the synonym group pulled the events row in, the
    # reranker put it first and the focus gate threw the tangential documents out —
    # and the model still answered no_answer, because the row says "OZP" and
    # nothing told it that OZP IS a škola v přírodě. The v5 protocol is right to
    # refuse what the context does not say; the equivalence the search acted on is
    # simply part of what the context says.
    # OTHER-ROČNÍK CHUNKS. document_lexical's safety net puts them in the pool only
    # when nothing of the caller's own ročník was found (see there). The model must
    # then say which ročníky the passage applies to instead of presenting it as the
    # caller's answer — three situations (no such event / not found / not for you)
    # were otherwise collapsing into the same silence.
    _other = [c for c in selected if c.get("other_grade")]
    other_grade_instruction = ""
    if _other and len(_other) == len(selected):
        other_grade_instruction = (
            "\n\nEVERY passage in the context belongs to ANOTHER ročník than the "
            "caller's — nothing for their own ročník exists in the corpus. Answer "
            "with what the passages say AND state plainly which ročníky it applies "
            "to and that it therefore does not concern the caller. Do not present "
            "it as the caller's own answer, and do not stay silent: the information "
            "exists, it is just for somebody else.")
    _syn = state.get("synonym_hint") or []
    synonym_instruction = ""
    if _syn:
        _eq = "; ".join(" = ".join(g) for g in _syn[:4])
        synonym_instruction = (
            f"\n\nEQUIVALENT TERMS in this corpus (the retrieval already treated "
            f"them as the same thing, so a passage using one of them ANSWERS a "
            f"question using another — do not refuse it as off-topic): {_eq}.")

    return {
        "messages": [
            SystemMessage(content=_load_system_prompt()),
            HumanMessage(content=f"Context:\n{context_text}\n\nSources:\n{sources_json}\n\n"
                                 f"Question: {user_query}"
                                 f"{meta_instruction}{synonym_instruction}"
                                 f"{other_grade_instruction}"
                                 f"{group_instruction}{lang_instruction}"),
        ],
        "model": model,
        "max_tokens": max_tokens,
        "reasoning_effort": reasoning_effort,
        "temperature": temperature,
        "context_text": context_text,
        "context_chunks": context_chunks,
        "diag": _diag,
        "content_top_n": content_top_n,
        "sources_json": sources_json,
        # The documents that are actually IN the context, in the order the answer
        # was asked to present them, each with the sections of it that were shown.
        # split_answer_parts() maps the generated paragraphs back onto this.
        "doc_order": [
            {"rank": n,
             "title": (sources[n - 1]["title"] if n <= len(sources) else None),
             "url": (sources[n - 1]["url"] if n <= len(sources) else None),
             "file_uuid": (sources[n - 1]["file_uuid"] if n <= len(sources) else None),
             "sections": _sections.get(n) or [],
             "excerpt": (_clip("\n\n".join(_excerpts.get(n) or []),
                               excerpt_max_chars, excerpt_cut_ratio)
                         if include_excerpt else None)}
            for n, _ in sorted(_labels)],
        "match_score": round(float(_doc_score), 4) if _doc_score is not None else None,
        # the other-ročník safety net travels in state from qa_lexical: those packs
        # never answer, so if the document branch answered instead, this is the only
        # place the user learns that a rule for their school exists at all
        "suggestions": _with_curated(
            suggestions + list(state.get("other_grade_suggestions") or []), state),
        "language": lang,
        # (question_grade, profile_grade) when they differ AND the conversation
        # has not been told yet — drives the trailing VARIANT and the two-grade
        # clarify suggestion
        "grade_mismatch": _mismatch,
    }



# ── the v5 NO-ANSWER protocol (prompt search_answer_generation v5) ──────────
# When the context holds no answer the model's WHOLE output is:
#     NO_ANSWER
#     VARIANT: <a better-phrased question>   (up to 4 lines)
# — deterministic to detect, language-free, and it streams: /search/stream just
# buffers tokens until the first newline before deciding to forward anything.
_VARIANT_RX = re.compile(r"^\s*VARIANT\s*:\s*(.+?)\s*$", re.I | re.M)


def parse_no_answer(text: str) -> tuple[bool, list[str]]:
    """(is_no_answer, question variants) — never raises."""
    t = (text or "").strip()
    if not t.upper().startswith("NO_ANSWER"):
        return False, []
    return True, [m.group(1) for m in _VARIANT_RX.finditer(t)][:4]


def extract_trailing_variants(text: str) -> tuple[str, list[str]]:
    """Split trailing "VARIANT: …" lines off an ANSWERED response (prompt v6:
    the model appends one when the question's grade differs from the caller's
    profile). Returns (clean answer, variants)."""
    lines = (text or "").rstrip().split("\n")
    variants: list[str] = []
    while lines:
        m = _VARIANT_RX.match(lines[-1])
        if not m:
            break
        variants.insert(0, m.group(1))
        lines.pop()
    return "\n".join(lines).rstrip(), variants[:2]


def _mismatch_noted_before(state: SearchState) -> bool:
    """Has THIS conversation already been told about the grade mismatch?

    The note must not nag on every follow-up ("pořád se to točí") — so before
    adding it, the previous questions of the conversation are re-read from
    question_answer_log (same scope as resolve_context) and re-analysed with
    grade_from_text: any earlier question whose grade differed from the
    caller's profile means the note was already made. Deterministic and
    language-independent — matching the LLM's phrasing in answer_md would
    break on the first rewording. Never raises; unknown = not noted."""
    conv = state.get("conversation_id")
    if not conv:
        return False
    try:
        from .prepare import grade_from_text
        with get_cursor() as cur:
            cur.execute(
                f"SELECT raw_query FROM {config.QUESTION_ANSWER_LOG_TABLE} "
                "WHERE conversation_id = %s "
                "  AND legal_entity_code IS NOT DISTINCT FROM %s "
                "  AND user_id IS NOT DISTINCT FROM %s "
                "  AND created_at > now() - interval '2 hours' "
                "ORDER BY created_at DESC LIMIT 6",
                (conv, state.get("legal_entity"), state.get("user_id")))
            prev = [r[0] for r in cur.fetchall() if r[0]]
        profile = str(state.get("user_school_grade") or "")
        for q in prev:
            g = grade_from_text(q)
            if g and profile and str(g) != profile:
                return True
    except Exception as e:
        logger.warning("mismatch-noted lookup failed (%s) — treating as new", e)
    return False


def _with_curated(suggestions, state) -> list[dict]:
    """A document answer with NO chips borrows the curated ones qa_lexical parked.

    Chips beside a generated answer are built from SECTION HEADINGS of the
    answering documents, so a document whose chunks carry none offers nothing —
    measured 2026-09-04 on "Co obsahuje školní vzdělávací program?": five context
    chunks, five without a heading, zero chips, while qa_lexical had ranked 23
    candidates for the same query and parked its shortlist.

    `deferred_suggest` ONLY, never fallback_suggestions. The deferred list cleared
    t_suggest and was withheld purely to give the documents their chance; the
    fallback list is what fell below every bar, and offering that beside an answer
    was built once and reverted for producing "Jak funguje další kolo přijímacího
    řízení?" under a question about school lunches.

    Only when the list is EMPTY: a document's own unread sections are closer to
    what was just answered than a curated question, so they are never displaced.
    """
    if suggestions:
        return suggestions
    return list((state.get("deferred_suggest") or {}).get("suggestions") or [])


def merge_suggestions(*groups) -> list[dict]:
    """Concatenate suggestion lists, first occurrence of a question wins."""
    out, seen = [], set()
    for g in groups:
        for s in g or []:
            q = s.get("question")
            if q and q not in seen:
                seen.add(q)
                out.append(s)
    return out


def without_clarify(suggestions) -> list[dict]:
    """Drop domain clarifications — for the no_answer paths.

    prepare_generation builds the clarify from the document types in the pool,
    and the no_answer merges carry prep's suggestions through on purpose (the
    variants and parked fallbacks must survive). A clarify must not: it offers a
    choice between domains the model has just said do not answer the question,
    so the branches are unrelated to what was asked."""
    return [s for s in suggestions or [] if s.get("kind") != "clarify"]


def _probe_variant(state: SearchState, variant: str) -> str | None:
    """Run the DETERMINISTIC QA steps on a rephrasing; return the entry it lands on.

    Verification by actually asking, instead of by guessing. The steps that cost
    real time are deliberately not run: document_lexical takes 370-700 ms and
    qa_vector embeds the query (868-2500 ms measured), while route + qa_exact +
    qa_lexical together are 40-150 ms — a few percent of a no_answer turn that has
    already spent five seconds or more.

    What it proves is narrow and worth stating: that an answer WOULD come back, not
    that it would be right. "Kdo je učitelem českého jazyka?" answers with
    coverage 0.7351 — and the answer is the length of the Czech test (production
    2026-09-04). Which is why the caller offers the ENTRY'S OWN question rather
    than the model's phrasing: a chip reading "Jak dlouho trvá test z českého
    jazyka?" is one nobody clicks by mistake.
    """
    # domain_codes/domain_ids ARE in this list, and that is the point of it
    # being a list: the probe re-runs route + qa_exact + qa_lexical, so a copy
    # that dropped the area scope would search the whole corpus and then offer
    # the curated question it found there — the one place a suggestion could
    # leave the chosen area while every retrieval query stayed inside it.
    probe = {k: state.get(k) for k in
             ("legal_entity", "entity_scope", "organization_entities", "user_id",
              "user_role", "user_school_grade", "language_filter",
              "domain_codes", "domain_ids")}
    probe["user_question"] = variant
    try:
        probe.update(prepare(probe) or {})
        probe.update(route(probe) or {})
        if probe.get("resolved"):
            return None
        # related_suggestions off: the probe wants the MATCH, and building chips
        # for a question nobody asked would run the ranking a second time.
        probe.update(qa_exact(probe, {"related_suggestions": False}) or {})
        if not probe.get("resolved"):
            probe.update(qa_lexical(probe) or {})
        if probe.get("resolved") and probe.get("outcome") == "answer":
            return probe.get("matched_entry_id")
        return None
    except Exception as e:
        logger.warning("answer: variant probe failed (%s) — variant dropped", e)
        return None


def variant_chips(state: SearchState, variants) -> list[dict]:
    """Rephrasings, verified by asking — and offered as the CURATED question found.

    One gate for all four places a VARIANT line becomes a chip (the empty-pool
    generator, the model's NO_ANSWER protocol, the trailing variant, and the
    streaming branch's copy of the last two); gating only the first left the loop
    exactly as it was (2026-09-04).

    Each survivor is emitted as `kind: qa` carrying the matched entry's own
    canonical question, not the model's wording. Two reasons. A curated question
    is a text the user can judge — the reported harm was a chip promising a
    teacher's name and delivering the length of the Czech test, and the deception
    lived in the chip, not in the answer. And clicking one lands on qa_exact,
    which returns exactly the entry the probe already saw.

    The model's job shrinks to what it is good at: finding the curated question a
    badly-phrased search missed. It no longer invents questions nobody can answer.
    """
    variants = [v for v in (variants or []) if (v or "").strip()]
    if not variants:
        return []
    if subject_absent(state):
        logger.info("answer: %d variant(s) dropped — subject absent from both indexes",
                    len(variants))
        return []
    hits, seen = [], set()
    for v in variants:
        entry_id = _probe_variant(state, v)
        if entry_id and entry_id not in seen:
            seen.add(entry_id)
            hits.append(entry_id)
    if not hits:
        logger.info("answer: all %d variant(s) dropped — none reaches an answer",
                    len(variants))
        return []
    try:
        with get_cursor() as cur:
            cur.execute(
                f"SELECT entry_id::text, canonical_q FROM {config.QA_ENTRIES_TABLE} "
                f"WHERE entry_id::text = ANY(%s)", (hits,))
            q_of = {r[0]: r[1] for r in cur.fetchall()}
    except Exception as e:
        logger.warning("answer: variant lookup failed (%s) — none offered", e)
        return []
    logger.info("answer: %d of %d variant(s) reached a curated entry",
                len(hits), len(variants))
    return [{"kind": "qa", "entry_id": e, "question": q_of[e]}
            for e in hits if q_of.get(e)]


def generate_variants(state: SearchState, params: dict) -> list[dict]:
    """LLM question variants for an EMPTY pool — kind `variant` suggestions.

    Today an empty pool answers in ~ms with no LLM call at all; this optional
    call (params.llm_variants on the answer step, default on) spends one small
    completion so the no_answer still carries something actionable. Any failure
    returns [] — variants are an offer, never a condition."""
    lv = params.get("llm_variants")
    lv = lv if isinstance(lv, dict) else {}
    if not lv.get("enabled", True):
        return []
    # NOT A WORDING PROBLEM. Every content concept of the question is absent from
    # the corpus, so no rephrasing that keeps the subject can match — offering one
    # sends the user round a loop. resolve_unanswered says so instead.
    if subject_absent(state):
        logger.info("answer: no variants — the subject is absent from both indexes")
        return []
    try:
        lang = state.get("query_language")
        lang_line = f" Write them in {language_name(lang)}." if lang else ""
        # Effort explicitly, or the reasoning tiers run at the vendor's own
        # default; measured 2026-09-03 as noise-level here, see migration 204.
        llm = get_chat(str(lv.get("model") or params.get("model")
                           or config.ANSWER_MODEL),
                       int(lv.get("max_tokens") or 200),
                       lv.get("reasoning_effort",
                              params.get("reasoning_effort")) or None,
                       lv.get("temperature", params.get("temperature")))
        resp = llm.invoke([
            SystemMessage(content=_load_system_prompt()),
            HumanMessage(content=(
                "Context:\n(no documents were found)\n\nSources:\n[]\n\n"
                f"Question: {state.get('user_question', '')}\n\n"
                "There is no context, so follow the no-answer protocol: output "
                "NO_ANSWER and up to 4 VARIANT lines — rephrase or broaden the "
                "question so a search over school documents could answer it."
                + lang_line)),
        ])
        _noans, variants = parse_no_answer(resp.content or "")
        return variant_chips(state, variants)
    except Exception as e:
        logger.warning("answer: variant generation failed (%s) — none", e)
        return []


def _section_label(c: dict) -> str | None:
    """Full section breadcrumb for the model / client.

    heading_path is the complete path INCLUDING this chunk's subsection since the
    unified model (2026-08-07): "I. ZÁKLADNÍ ŠKOLA › … › Článek III › Docházka do
    školy". It is the ONLY thing telling the ZŠ copy of a section from the SŠ one.
    Return it as-is — do NOT append subsection_heading again (it is already the
    path's leaf). Documents without a path fall back to section + subsection."""
    path = c.get("heading_path")
    if not path:
        path = " › ".join(x for x in (c.get("section_heading"),
                                      c.get("subsection_heading")) if x) or None
    return _strip_list_numbering(path)


def _ctx_header(c: dict, doc_no: int) -> str:
    """The line that introduces a chunk in the prompt.

    Headings live in their own columns since migration 61 and are deliberately NOT
    copied into chunk_text, so the body of "**Třída – 7.** 4x 544, 2x 460 …" no
    longer says anywhere that this is the supply list for 2026/2027, 2nd stage.
    That is fine for retrieval (the heading columns are searched, and weighted) but
    not for generation: the model has to be told what it is reading, or it answers
    about supplies without the year or the grade.
    """
    head = _section_label(c)
    return (f"## Document {doc_no} | source: {c.get('file_name', 'unknown')}"
            + (f" | section: {head}" if head else "") + "\n")

def answer(state: SearchState, params: dict | None = None) -> dict:
    params = params or {}
    if state.get("resolved"):
        return {}

    prep = prepare_generation(state, params)
    if prep is None:
        logger.info("answer: empty chunk pool → deferred suggest / no_answer")
        res = resolve_unanswered(state)
        if res.get("outcome") == "no_answer":
            res["suggestions"] = merge_suggestions(
                generate_variants(state, params), res.get("suggestions"))
        return res

    llm = get_chat(prep["model"], prep["max_tokens"], prep.get("reasoning_effort"),
                   prep.get("temperature"))
    try:
        resp = llm.invoke(prep["messages"])
    except Exception as e:  # LLM/OpenAI unavailable → degrade, don't 500 the request
        logger.warning("answer: LLM generation failed (%s) — llm_unavailable", e)
        # NOT resolve_unanswered: that says "rephrase" and logs the outage as
        # no_answer, which is what made an outage unreadable from a corpus gap.
        # The parked deferred_suggest / fallback suggestions still survive —
        # resolve_llm_unavailable keeps that rule.
        return resolve_llm_unavailable(state)
    _answer = resp.content.strip()
    _noans, _variants = parse_no_answer(_answer)
    if _noans:
        # the model followed the v5 protocol: no apologetic prose reaches the
        # user — the uniform no_answer message does, and the model's better
        # question variants become suggestions (kind `variant`)
        logger.info("answer: model returned NO_ANSWER with %d variant(s)",
                    len(_variants))
        res = resolve_unanswered(state)
        res["suggestions"] = merge_suggestions(
            variant_chips(state, _variants),
            without_clarify(prep.get("suggestions")), res.get("suggestions"))
        res["trace_detail"] = {"context_chunks": prep["context_chunks"],
                               "pool": len(state.get("rag_chunks") or []),
                               "candidates": (prep.get("diag") or {}).get("candidates"),
                               "focus": {k: v for k, v in (prep.get("diag") or {}).items()
                                         if k != "candidates"},
                               "content_top_n": prep["content_top_n"],
                               "llm_verdict": "NO_ANSWER"}
        return res
    _answer, _trailing = extract_trailing_variants(_answer)
    _mm = prep.get("grade_mismatch")
    if _trailing and _mm:
        qg, pg = _mm
        prep["suggestions"] = merge_suggestions(
            [{"kind": "clarify",
              "question": system_message("clarify_domains",
                                         prep.get("language")),
              "options": [
                  {"label": f"{qg}. ročník",
                   "question": state.get("user_question", "")},
                  {"label": f"{pg}. ročník", "question": _trailing[0]},
              ]}],
            prep.get("suggestions"))
    elif _trailing:
        prep["suggestions"] = merge_suggestions(
            variant_chips(state, _trailing),
            prep.get("suggestions"))
    _answer, _empty_handed = drop_empty_handed_paragraphs(
        _answer, _noinfo_matcher(prep.get("language")))
    if _empty_handed:
        logger.info("answer: EARNED backstop dropped %d empty-handed paragraph(s): %s",
                    len(_empty_handed), _empty_handed)
    _parts = split_answer_parts(
        _answer, prep.get("doc_order") or [],
        attrib_positional_min=float(params.get("attrib_positional_min", 0.15)),
        attrib_best_min=float(params.get("attrib_best_min", 0.25)))
    logger.info("answer: generated %d chars, %d paragraph(s), %d linked to a document",
                len(resp.content), len(_parts),
                sum(1 for x in _parts if x.get("source_rank")))
    # CITE only the documents the answer actually drew on: a seeded document no
    # paragraph attributes to was context the model (rightly) ignored — listing
    # it under the answer misleads the reader. When attribution failed everywhere
    # (no paragraph carries a source_rank), keep the full seed list rather than
    # citing nothing the answer plainly came from.
    _sources_json = prep["sources_json"]
    try:
        _used = {x.get("source_rank") for x in _parts if x.get("source_rank")}
        if _used:
            _all = json.loads(_sources_json)
            _kept = [s for s in _all if s.get("rank") in _used]
            if _kept and len(_kept) < len(_all):
                _sources_json = json.dumps(_kept, ensure_ascii=False)
                logger.info("answer: sources filtered to used documents %s (of %d)",
                            sorted(_used), len(_all))
    except Exception as e:
        logger.warning("answer: source filtering failed (%s) — keeping all", e)
    return {
        "resolved": True, "outcome": "answer", "answer_source": "rag",
        "answer": _answer, "answer_parts": _parts,
        "suggestions": prep.get("suggestions") or [],
        "context_text": prep["context_text"],
        "sources_json": _sources_json,
        # the trace must say what the answer was built from, chunk by chunk
        "trace_detail": {"context_chunks": prep["context_chunks"],
                         "pool": len(state.get("rag_chunks") or []),
                         "candidates": (prep.get("diag") or {}).get("candidates"),
                         "focus": {k: v for k, v in (prep.get("diag") or {}).items()
                                   if k != "candidates"},
                         "content_top_n": prep["content_top_n"],
                         **({"earned_dropped": _empty_handed}
                            if _empty_handed else {})},
        "match_score": prep["match_score"],
        "user_language": prep["language"],
    }
