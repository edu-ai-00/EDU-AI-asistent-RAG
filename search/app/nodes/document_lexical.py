# last change in code by Martin Safranek (2026-08-22).
"""Node — document_lexical: lexical chunk retrieval over document_chunks (gather-only).

Runs after the QA lexical step. Builds an OR tsquery from the query's reduced-form
tokens against document_chunks (ts_lemma||ts_stem) and appends candidate
chunks to state["rag_chunks"]. It NEVER answers on its own — the single terminal
`answer` LLM consumes the gathered pool.

Precision gate = **coverage**, not absolute ts_rank. An OR tsquery hits a chunk on
one incidental shared token (kdy / škola / město …), so an off-topic query still
"matches". A raw ts_rank floor can't separate them across documents because
ts_rank_cd scales with chunk size / term frequency (a 2-chunk doc inflates a
single-token hit to ~1.4–2.5, a many-small-chunk doc keeps it ≤0.8). Coverage —
the fraction of the query's CONTENT concepts actually present in the chunk — is
chunk-size independent (mirrors qa_lexical): an off-topic query covers ~0–0.2 of
its concepts, a real one most of them. Chunks below `min_coverage` are dropped so
the pool stays empty and the cascade falls through to no_answer.

NOTE: document_chunks is empty until a PDF/Office doc is ingested.
"""

import logging

from ..state import SearchState
from ..db import get_cursor
from .. import config
from .. import scope
from ..settings import coverage_stopwords, bm25_stopwords
from .. import topical
from .qa_common import (
    form_ts as _qa_form_ts,
    resolve_form, form_ts, form_query_tokens, content_tokens,
    expand_query, coverage, weighted_coverage, load_synonym_groups,
)

logger = logging.getLogger(__name__)

# documents whose TYPE never answers (search_role='excluded', migration 172)
# must not enter retrieval at all — see scope.not_excluded()
_NOEXCL = scope.not_excluded("m")


def _doc_concept_idf(cur, concept_sets, tsexpr, lang_cond, lang_params,
                     scope_cond, scope_params) -> list[float]:
    """One IDF weight per query concept, measured over the VISIBLE chunks.

    df = chunks (in the caller's scope + language filter) whose weighted ts
    vector matches any of the concept's tokens; weight = ln((N+1)/(df+1)) + 1 —
    identical formula to qa_common.concept_idf_weights so the two lexical steps
    read coverage on the same scale. One aggregate query per request; on a
    large corpus this is a scan, which is why `idf_coverage` is a switch.
    """
    import math
    if not concept_sets:
        return []
    selects, args = [], []
    for cs in concept_sets:
        selects.append(
            f"count(*) FILTER (WHERE {tsexpr} @@ to_tsquery('simple', %s))")
        args.append(" | ".join(sorted(cs)))
    cur.execute(
        f"SELECT count(*), {', '.join(selects)} "
        f"FROM {config.CHUNKS_TABLE} t "
        f"JOIN {config.METADATA_TABLE} m "
        f"  ON t.file_uuid = m.file_uuid "
        f" AND t.processing_uuid = m.processing_uuid "
        f" AND m.is_active = true {_NOEXCL}"
        f"WHERE true{lang_cond}{scope_cond}",
        args + list(lang_params) + list(scope_params))
    row = cur.fetchone()
    total = max(int(row[0] or 0), 1)
    return [math.log((total + 1) / (int(df or 0) + 1)) + 1.0 for df in row[1:]]

# document_chunks reduced-form columns are chunk_-prefixed (qa_variants are not,
# so qa_common.form_text_concat / _FORM_TEXT can't be reused here).
_CHUNK_TEXT = {"norm": "chunk_norm_text", "lemma": "chunk_lemma_text", "stem": "chunk_stem_text"}
# Section / subsection headings live in their own columns (migration 61) with one
# tsvector per form. They are searched TOGETHER with the body but weighted
# heavier: "pomůcka" occurs in 21 chunk bodies and in only 3 element headings, so
# a heading match is a far more selective signal and must not rank as prose.
_FORM_TS_BODY = {"norm": "ts_norm", "lemma": "ts_lemma", "stem": "ts_stem"}
_HEAD_TS = {"norm": "ts_heading_norm", "lemma": "ts_heading_lemma", "stem": "ts_heading_stem"}
_HEAD_TEXT = {"norm": ("section_heading_norm", "subsection_heading_norm"),
              "lemma": ("section_heading_lemma", "subsection_heading_lemma"),
              "stem": ("section_heading_stem", "subsection_heading_stem")}


def _weighted_ts(form, alias: str, w_head: str, w_body: str) -> str:
    """tsvector for match+rank: heading columns at `w_head`, body at `w_body`."""
    parts = [f"setweight({alias}.{_HEAD_TS[f]}, '{w_head}')" for f in form
             if f in _HEAD_TS]
    parts += [f"setweight({alias}.{_FORM_TS_BODY[f]}, '{w_body}')" for f in form]
    return "(" + " || ".join(parts) + ")"


def _chunk_text_concat(form, alias: str) -> str:
    """Body + heading text, for the Python coverage gate. The heading has to be in
    here too: without it a chunk that only its SECTION heading identifies scores
    0.000 — measured on the supplies list, where chunks 1 and 2 went from 0.000 to
    1.000 once the heading was visible to coverage."""
    cols = [f"{alias}.{_CHUNK_TEXT[f]}" for f in form]
    for f in form:
        cols += [f"{alias}.{c}" for c in _HEAD_TEXT.get(f, ())]
    return "concat_ws(' ', " + ", ".join(cols) + ")"


def _weighted_ts_heading_only(form, alias: str) -> str:
    """Heading tsvectors only, UNWEIGHTED — used to ask "does this concept sit in
    the heading?", where a weight would change nothing."""
    parts = [f"{alias}.{_HEAD_TS[f]}" for f in form if f in _HEAD_TS]
    return "(" + " || ".join(parts) + ")" if parts else ""


def _heading_text_concat(form, alias: str) -> str:
    """Heading columns only, for the topical gate below."""
    cols = []
    for f in form:
        cols += [f"{alias}.{c}" for c in _HEAD_TEXT.get(f, ())]
    return "concat_ws(' ', " + ", ".join(cols) + ")" if cols else "''"


def _heading_df_share(cur, concept_sets, form) -> list[float]:
    """For each query concept: the share of chunks whose HEADING contains it.

    A concept found in a heading is a statement about the chunk's TOPIC, not a
    quarter of a fraction — but only while the concept actually distinguishes
    topics. Measured on the school corpus: "pomucka" is in 5 of 35 chunk headings
    (0.14) and in NO chunk body, so it is the whole signal; "skola" and "trida"
    are in 13 of 35 headings (0.37) and say nothing about what a section is about.
    The share separates the two cases from the data, so no word list is needed.

    One aggregate over the chunks table per request.
    """
    if not concept_sets:
        return []
    head_ts = " || ".join(f"t.{_HEAD_TS[f]}" for f in form if f in _HEAD_TS)
    if not head_ts:
        return [1.0] * len(concept_sets)
    selects, args = [], []
    for cs in concept_sets:
        selects.append(f"count(*) FILTER (WHERE ({head_ts}) "
                       f"@@ to_tsquery('simple', %s))")
        args.append(" | ".join(sorted(cs)))
    cur.execute(f"SELECT count(*), {', '.join(selects)} "
                f"FROM {config.CHUNKS_TABLE} t", args)
    row = cur.fetchone()
    total = int(row[0] or 0)
    if not total:
        return [1.0] * len(concept_sets)
    return [float(df or 0) / total for df in row[1:]]

def _tokens(text: str) -> set[str]:
    return {t for t in (text or "").split() if t}


def _fold_token_set(terms) -> set:
    """Diacritics-folded ascii word tokens. document_chunks.bm25_text is stored
    FOLDED (norm/lemma/stem concatenated), so the BM25 query must be folded too."""
    import unicodedata
    out: set = set()
    for t in terms or ():
        f = "".join(c for c in unicodedata.normalize("NFD", str(t).lower())
                    if unicodedata.category(c) != "Mn")
        for w in "".join(ch if ch.isalnum() else " " for ch in f).split():
            out.add(w)
    return out


def _fold_terms(terms) -> str:
    """The folded tokens joined as a pg_search BM25 query — each token an OR clause."""
    return " OR ".join(sorted(_fold_token_set(terms)))


def document_lexical(state: SearchState, params: dict | None = None) -> dict:
    if state.get("resolved"):
        return {}
    params = params or {}
    # Declared here, not where it is computed: that block sits behind several
    # early returns, and an unbound name in a node that must never crash is a
    # worse bug than a missing signal.
    _subject_here = None
    fetch_k = int(params.get("fetch_k", config.RAG_LEXICAL_FETCH_K))
    # coverage floor: keep a chunk only if it covers at least this fraction of the
    # query's content concepts. 0 / absent = no floor (previous behaviour).
    min_coverage = float(params.get("min_coverage", 0.0))
    use_synonyms = params.get("synonyms", True)
    # A concept hit in the HEADING opens the gate on its own, bypassing the
    # coverage fraction — but only for a concept discriminative enough among
    # headings. Without this, five chunks whose heading is literally "Seznam
    # pomucek …" were dropped at coverage 0.25 on "Jake pomucky potrebuje
    # prvnacek?", because the query also held `jaky`, `potrebovat` and
    # `prvnak` — three concepts no document can contain. The fraction punished
    # the QUESTION's wording, not the chunk.
    heading_pass = bool(params.get("heading_pass", True))
    heading_max_df_share = float(params.get("heading_max_df_share", 0.25))
    # A purely NUMERIC concept must not open the heading gate. "Jaké mám mít pomůcky
    # do 7. třídy?" yields the concept `7`, which is statistically rare among
    # headings and therefore looked discriminative — so a section headed
    # "5. Vzdělávání učitelů v kariérovém poradenství > 7. …" passed at coverage
    # 0.25 and earned its own paragraph in an answer about school supplies. A digit
    # is an ordinal fragment, not a topic: it says WHICH item, never WHAT ABOUT. It
    # still counts towards coverage, where it belongs.
    heading_numeric_pass = bool(params.get("heading_numeric_pass", False))
    # How many query concepts reach the SQL (one @@ per concept) and the coverage
    # gate. On a long question the concepts beyond this cap are invisible to both,
    # so the cap decides which parts of the question are allowed to matter.
    max_concepts = int(params.get("max_concepts") or 8)
    # score = coverage + a fraction of ts_rank. The fraction is a POLICY: at 0.001
    # coverage always beats word density, which is what put the exam sections
    # (ts_rank 3.6) ahead of the school supply lists (1.8) at equal coverage. Raise
    # it to let density speak; the cap keeps one unbounded rank from crossing a
    # whole coverage step.
    rank_weight = float(params.get("rank_tiebreak_weight") or 0.001)
    rank_cap = float(params.get("rank_tiebreak_cap") or 0.999)
    # ── typo fallback (the documents' version of qa_lexical's trgm net) ──────
    # Fires only when NOTHING survived the ts channels: a typo'd token defeats
    # them both (the lemma dictionary needs the real word, the stem of a typo
    # is a typo). word_similarity is pg_trgm's "short word vs LONG text"
    # measure — plain similarity normalises by both lengths and never fires
    # against a 900-char chunk. Indexes: init 100.
    trgm_fallback = bool(params.get("trgm_fallback", True))
    trgm_min_sim = float(params.get("trgm_min_sim", 0.35))          # per-token gate (index GUC)
    trgm_min_typo_coverage = float(params.get("trgm_min_typo_coverage", 0.5))  # avg over tokens
    trgm_fetch_k = int(params.get("trgm_fetch_k", 15))
    # ── IDF-weighted coverage (the documents' version of qa_lexical's) ──────
    # Unweighted coverage treats every concept as equal, and that is exactly
    # wrong for short school questions: on "Jaké mám mít pomůcky?" the concepts
    # `jaky`, `mit` and `pomucka` weigh the same, so a chunk full of modal
    # filler covers 2/3 while the one topical word carries 1/3. The weights are
    # plain IDF computed over the chunks THE CALLER MAY SEE (same scope
    # predicate), so common corpus words weigh little and the rare topical
    # concept decides — data-driven, per language, no hand lists.
    idf_coverage = bool(params.get("idf_coverage", True))
    # BM25 re-rank (ParadeDB pg_search): after the coverage gate has chosen the
    # lexical candidates, reorder them by BM25 over document_chunks.bm25_text so a
    # rare topical term outweighs a common word a chatty document repeats (ts_rank_cd
    # has no corpus IDF). Reorders lexical_rank ONLY; the coverage gate and every
    # downstream step are untouched. Switch off → falls back to the ts_rank order.
    bm25_lexical = bool(params.get("bm25_lexical", config.BM25_LEXICAL))
    # RELATIVE floor for the BM25 mode ("utažení šroubů"): keep a chunk only if
    # its BM25 reaches this share of the ROUND's best hit — else drop it here so
    # it never pollutes the answer context AND the pool stays lean enough for the
    # rag_vector gates (only_if_empty / max_coverage_below) to let the semantic
    # net actually run; what falls out lexically can come back with a vector_rank
    # through its own cosine gate. Per round: the grade round's score range is far
    # below the grade-less round's, a global threshold would silence it. Escape
    # hatches: a heading_match (the supply list's BM25 is tiny but its heading IS
    # the query) or genuine concept coverage keep the chunk regardless.
    # 0.65 measured (2026-08-09): cuts the preparation-logistics tail on "co se
    # hodnotí u přijímaček" (chunks at 42–61 % of the round's top) with zero
    # change on the 9-question suite; 0.4 dropped nothing anywhere.
    bm25_min_ratio = float(params.get("bm25_min_ratio", 0.65))
    # TOPICAL ADMISSION: without a heading match a chunk must cover at least one
    # concept that is DISCRIMINATIVE in the caller's corpus (df share within
    # bm25_max_df_share, not a bare number). A corpus-generic word ("škola", 54 %
    # df) can never admit a chunk on its own — and NO round is owed a minimum of
    # one chunk: the grade round's best hit with only generic coverage is evidence
    # of nothing, and an empty round is the honest result (decision 2026-08-09).
    topical_admission = bool(params.get("topical_admission", True))
    form = resolve_form(params)

    tokens = form_query_tokens(state, form)      # tsquery recall over the chosen form(s)
    content = content_tokens(state, form)        # coverage 'concepts' (reduced form)
    if not tokens:
        return {"rag_chunks": state.get("rag_chunks", [])}

    # /search language filter: effective document language = user code, else LLM code
    lang = state.get("language_filter")
    # synonym expansion follows the QUERY language (always resolved: filter >
    # detection) — not the filter, which may be absent
    qlang = state.get("query_language") or lang
    # filter on the language the tokens were ACTUALLY reduced in (chunk-level
    # langdetect_result = user code > shared detection), metadata as fallback
    lang_cond = (" AND coalesce(t.langdetect_result, m.document_language_user,"
                 " m.document_language_llm) = %s") if lang else ""
    lang_params = [lang] if lang else []
    # who may see which rows — one predicate, one place (see scope.py)
    scope_cond, scope_params = scope.where(
        "t", state.get("entity_scope") or [],
        state.get("organization_entities") or [], state.get("user_id"),
        state.get("user_role"), validity=True)
    # THE CHOSEN AREAS, on the CHUNK row (document_chunks.document_domain_id,
    # the effective label the ingest propagated from the document: user
    # declaration if any, else the classifier's — see spec §6). Appended to
    # scope_cond because every query in this node interpolates that one string,
    # so the candidate query, the trgm fallback AND the two df/statistics
    # queries below all get it — a df counted over a wider corpus than the
    # candidates it weighs is the denominator bug the spec warns about.
    #
    # A LONG DOCUMENT IS NOT SPLIT for this. It carries all of its areas and
    # every one of its chunks inherits them, so the whole document is reachable
    # in each area and the existing ranking picks the passage the question is
    # about. There is no per-chunk topical label and this change does not invent
    # one.
    dom_cond, dom_params = scope.domain_where("t", "document_domain_id",
                                              state.get("domain_ids"))
    # TWO PREDICATES, and the split is the whole subtlety of this step.
    #
    #   cand_cond  identity + area. WHICH ROWS MAY ANSWER. Every candidate query
    #              and the trgm fallback use it, so nothing outside the chosen
    #              areas can reach the pool.
    #   scope_cond identity ONLY, exactly as before. The df/IDF STATISTICS use
    #              it, and they must: `bm25_max_df_share` (0.25) asks "is this
    #              word generic in this school's language" — "škola" sits in
    #              54 % of the corpus — and that is a property of the school, not
    #              of the area the user happens to have picked.
    #
    # Measured while building the fixture, and this is why the split exists: with
    # the statistics narrowed too, a three-chunk area made EVERY query token
    # exceed 0.25 df, the gate marked them all generic, and the topical
    # admission in `answer` then rejected the very document the area was about —
    # a correct filter turned into a no_answer by a moved threshold. Narrowing
    # eligibility is the feature; moving a calibrated bar with it is not, and
    # spec §5 says as much ("aby se po zúžení nezměnily nechtěně prahy a
    # denominátory"). Inventing a new threshold instead was the alternative, and
    # a new production number needs a measurement and an owner's decision.
    #
    # This costs nothing in correctness: a denominator cannot make a row
    # eligible. It only decides weights and genericity among rows cand_cond has
    # already admitted.
    cand_cond = scope_cond + dom_cond
    cand_params = [*scope_params, *dom_params]

    # weights are config, not code: A/B by default (heading over body), and
    # w_head == w_body turns the preference off without a rebuild
    _w_head = str(params.get("heading_weight") or "A")
    _w_body = str(params.get("body_weight") or "B")
    tsexpr = _weighted_ts(form, "t", _w_head, _w_body)
    entry_text = _chunk_text_concat(form, "t")   # chunk's reduced text, for coverage
    chunks = list(state.get("rag_chunks", []))
    all_terms: list = []                          # query terms, for the BM25 re-rank below
    _syn_hint: list = []                          # synonym groups the query started

    try:
        with get_cursor() as cur:
            groups, kind_of, _phrase_g, _terms = (
                load_synonym_groups(cur, qlang) if use_synonyms
                else ({}, {}, set(), {}))
            # WHICH groups this query started, in their ORIGINAL spelling. The
            # expansion helps retrieval and then stops there: on "Kdy je nejbližší
            # škola v přírodě?" the events row was found, reranked first and the
            # focus gate threw the tangential documents out — and the answer model
            # still said no_answer, because the row reads "OZP" and nothing in the
            # prompt says that OZP IS a škola v přírodě. The knowledge lived in the
            # search layer only. These terms travel to `answer` so the prompt can
            # state the equivalence the retrieval already acted on.
            # the SAME term-level rule as the matching below, so the prompt hint
            # is not built for a group the retrieval never actually started
            _hits = [g for g, tl in (_terms or {}).items()
                     if kind_of.get(g) == "entity"
                     and any(ts and ts <= tokens for ts in tl)]
            if _hits:
                cur.execute(
                    f"SELECT group_id, term FROM {config.QA_SYNONYM_TABLE} "
                    "WHERE is_active AND group_id = ANY(%s) ORDER BY group_id, is_key DESC, term",
                    (_hits,))
                _by_group: dict = {}
                for _g, _t in cur.fetchall():
                    _by_group.setdefault(_g, []).append(_t)
                _syn_hint = [terms for terms in _by_group.values() if len(terms) > 1]
            all_terms, concept_sets = expand_query(
                tokens, content, groups, kind_of,
                coverage_stop=(coverage_stopwords().get(qlang or "") or None),
                phrase_groups=_phrase_g, terms=_terms)
            tsq = " | ".join(sorted(all_terms))
            # CANDIDATE ORDER: concepts covered first, ts_rank_cd second.
            # Ordering by raw ts_rank_cd alone buried rare discriminative terms:
            # the tsquery is an OR of every token, ts_rank_cd rewards term
            # FREQUENCY, and `simple` has no IDF — so on the school-code PDF the
            # title chunk holding the document's motto (the query's one rare
            # concept, coverage 1.0) ranked 70/132, far below fetch_k, drowned
            # by long paragraphs repeating "školní řád" (2026-07-31). Coverage
            # was already the precision gate in Python, but it never saw the
            # chunk: the LIMIT had cut it. So the cut itself must be
            # coverage-aware: one @@ per query concept (a concept = a token OR
            # its synonyms), counted in SQL. Concepts are capped to keep the
            # query bounded; beyond the cap the rank tie-break still applies.
            concept_qs = [" | ".join(sorted(cs)) for cs in concept_sets[:max_concepts]]
            concept_expr = " + ".join(
                f"(CASE WHEN {tsexpr} @@ to_tsquery('simple', %s) THEN 1 ELSE 0 END)"
                for _ in concept_qs) or "0"
            head_ts = _weighted_ts_heading_only(form, "t")
            head_expr = " + ".join(
                f"(CASE WHEN {head_ts} @@ to_tsquery('simple', %s) THEN 1 ELSE 0 END)"
                for _ in concept_qs) if head_ts else "0"
            head_text = _heading_text_concat(form, "t")
            idf_w = (_doc_concept_idf(cur, concept_sets, tsexpr, lang_cond,
                                      lang_params, scope_cond, scope_params)
                     if idf_coverage else [])
            # topical gate needs to know which concepts are discriminative
            df_share = (_heading_df_share(cur, concept_sets[:max_concepts], form)
                        if heading_pass else [])
            # BM25 as the lexical RANKER (end of the lexical phase): when on, the
            # candidates are RETRIEVED and ORDERED by paradedb BM25 over bm25_text.
            # IDF down-weights the common word ("škola") and lifts the rare topical
            # term ("pomůcka"), so the right chunk is not drowned by a big ts set
            # (measured: the 6th-grade supply list went from off-pool to top-3). Off
            # → the original coverage/ts_rank candidate order. ts_rank/concepts_hit
            # are still SELECTed (the coverage/heading gate + score reuse them).
            # ── BM25 query, corpus-generic tokens removed ────────────────────
            # A token in more than bm25_max_df_share of the caller's VISIBLE chunks
            # carries almost no signal — "škola" sits in 54 % of this corpus (IDF≈0),
            # yet summed over our norm+lemma+stem triple on a long chunk it still
            # inflated BM25, letting the školní řád (matched only via "škola/žák")
            # outscore the pomůcky list. df is measured over the SAME scope + language
            # predicate as retrieval (per tenant, data-driven — a static stoplist would
            # not catch a domain-common word like "škola"). Never strip to empty: if
            # every token is generic, keep them all and let RRF/IDF sort it out.
            # generic-token removal has TWO configurable layers:
            #  1. AUTOMATIC df-gate — drop a token in more than bm25_max_df_share of
            #     the caller's visible corpus (a per-tenant, data-driven threshold in
            #     the search_setting pipeline params; catches "škola" at 54 % df).
            #  2. EXPLICIT list — qa_stopword(scope='bm25'), a human-curated set force-
            #     dropped regardless of df (junk the gate misses, or a word just under
            #     the threshold). Lives in the číselník, not code, per MD §2.2.
            # A token is dropped if EITHER fires; never strip the query to empty.
            bm25_max_df_share = float(params.get("bm25_max_df_share", 0.25))
            _bm25_stop = {t for term in (bm25_stopwords("bm25").get(qlang or "") or set())
                          for t in _fold_token_set([term])}
            # grade round only: grade is already a HARD metadata filter there, so a
            # grade/structural token ("ročník/třída/stupeň") and a bare digit ("6")
            # distinguish NOTHING among the round's chunks — they only reward a chunk
            # that repeats the grade number (the event calendar beat the supply list on
            # "6. a 7. ročník"). Kept out of the grade round's BM25 so its scoring is
            # pure topic: a chunk with zero "pomůcka" then simply does not match.
            _bm25_grade_stop = {t for term in (bm25_stopwords("bm25_grade").get(qlang or "") or set())
                                for t in _fold_token_set([term])}
            # folded tokens that carry no topical signal — the explicit list plus
            # whatever the df-gate drops below. Reused by the heading match so a
            # generic concept ("škola") cannot grant one (see the by_heading loop).
            _generic_folded = set(_bm25_stop)
            _bm25_q = _bm25_q_grade = ""
            # corpus size + per-token df: filled by the gate below, but declared
            # here because the TRGM FALLBACK path reads them too (it must label its
            # chunks with the same topical signals — see there).
            _ntot = 0
            _df: dict = {}
            if bm25_lexical:
                _toks = sorted(_fold_token_set(all_terms))
                if _toks:
                    cur.execute(
                        f"SELECT count(*) FROM {config.CHUNKS_TABLE} t "
                        f"JOIN {config.METADATA_TABLE} m ON t.file_uuid = m.file_uuid "
                        f" AND t.processing_uuid = m.processing_uuid AND m.is_active = true {_NOEXCL}"
                        f"WHERE true{lang_cond}{scope_cond}", [*lang_params, *scope_params])
                    _ntot = cur.fetchone()[0] or 0
                    cur.execute(
                        f"SELECT tok, (SELECT count(*) FROM {config.CHUNKS_TABLE} t "
                        f"  JOIN {config.METADATA_TABLE} m ON t.file_uuid = m.file_uuid "
                        f"   AND t.processing_uuid = m.processing_uuid AND m.is_active = true {_NOEXCL}"
                        f"  WHERE t.bm25_text @@@ tok{lang_cond}{scope_cond}) AS df "
                        f"FROM unnest(%s::text[]) AS tok",
                        [*lang_params, *scope_params, _toks])
                    _df = {r[0]: r[1] for r in cur.fetchall()}
                    # ARE THE MISSING WORDS MISSING HERE TOO? Only the concepts
                    # qa_lexical found absent (df 0 in the packs) are asked about.
                    # Asked about the whole query this answers "present" on any
                    # content-ish word the bigger corpus happens to hold —
                    # "jmenuje" is in the school documents, "školník" is in
                    # neither, and the question is about the caretaker. The df
                    # scan already ran, so this costs nothing (2026-09-04).
                    # EVERY missing word has to turn up here, not just one. With
                    # `any`, "Jak se jmenuje školník?" was rescued by `jmenuje`
                    # (absent from 210 curated questions, ordinary in the school's
                    # documents) while `školník` is in neither — and the question
                    # is about the caretaker. A word that exists in NO index is the
                    # subject far more often than it is noise, and no rephrasing
                    # that keeps it can succeed.
                    _miss = _fold_token_set(state.get("query_absent_concepts") or [])
                    _subject_here = (all(_df.get(t, 0) > 0 for t in _miss)
                                     if (_ntot and _miss) else None)
                    _keep = [t for t in _toks
                             if t not in _bm25_stop
                             and _ntot and (_df.get(t, 0) / _ntot) <= bm25_max_df_share]
                    # grade round drops, ON TOP of the general keep, the grade vocab
                    # and any bare-numeric token (an ordinal, never a topic)
                    _keep_grade = [t for t in _keep
                                   if t not in _bm25_grade_stop and not t.isdigit()]
                    _bm25_q = " OR ".join(_keep if _keep else _toks)
                    _bm25_q_grade = " OR ".join(_keep_grade if _keep_grade
                                                else (_keep if _keep else _toks))
                    _dropped = [t for t in _toks if t not in _keep]
                    _generic_folded |= set(_dropped)
                    if _dropped or _keep_grade != _keep:
                        logger.info("document_lexical: bm25 gate kept %d/%d (max_df=%.2f, "
                                    "list=%d), dropped %s; grade-round also drops %s",
                                    len(_keep), len(_toks), bm25_max_df_share, len(_bm25_stop),
                                    _dropped, [t for t in _keep if t not in _keep_grade])
            # LABELLING CONTEXT — built once here (this step already computed the
            # concept sets, the df map and the heading df shares) and handed to the
            # OTHER retrievers through the state, so rag_vector and rag_keywords can
            # label their chunks with the same two fields the gates in `answer` read.
            # Before this, only this step's main loop produced them, which left every
            # other path a silent precision blind spot (see topical.py).
            _tctx = topical.context(
                form=form, concept_sets=concept_sets, max_concepts=max_concepts,
                df=_df, ntot=_ntot, generic=_generic_folded, df_share=df_share,
                heading_pass=heading_pass, heading_numeric_pass=heading_numeric_pass,
                heading_max_df_share=heading_max_df_share)
            _bm25_on = bool(_bm25_q)
            _score_expr = "paradedb.score(t.id)" if _bm25_on else "NULL::real"
            _where_sql = ("t.bm25_text @@@ %s" if _bm25_on
                          else f"{tsexpr} @@ to_tsquery('simple', %s)")
            _order_sql = ("bm25 DESC NULLS LAST" if _bm25_on
                          else "heading_hits DESC, concepts_hit DESC, rank DESC")
            # ── grade-gated TWO ROUNDS ───────────────────────────────────────
            # Round 1 retrieves ONLY chunks carrying the caller's grade (or the
            # universal 9999); round 2 ONLY grade-LESS chunks. A grade the caller
            # is NOT (a grade-7-only chunk for a 6th-grader) falls through both —
            # the grade-pollution guard. Both rounds ALWAYS run and their results
            # are UNION-ed; the grade round's chunks win the final order through
            # the GRADE TIER in answer._sortkey (there is no additive boost —
            # the 2026-08-09 single-score change removed those) ("ročníkové chunky mají vyšší
            # score"). This is the only way a grade-specific list the free-text
            # query cannot reach — "Seznam pomůcek … 2. stupeň", body = bare item
            # codes, BM25 rank 93 among ALL chunks — enters the pool: among the
            # grade's own chunks alone it sits at #2. Round 1 is SKIPPED when the
            # grade is unknown (no profile grade, none inferred from the question)
            # → a single unfiltered round runs, the previous behaviour.
            _grade_val = state.get("question_grade") or state.get("user_school_grade")
            _gids: list = []
            if _grade_val:
                # EXACT ročník rows only — 9999 moved to the universal round below.
                # "9999" means all ročníky OF ONE ENTITY (1..9 under a ZŠ, 10..13
                # under a SŠ, 1..13 under the organisation), so it is not an exact
                # match for anybody; putting it in the grade round handed it the
                # grade tier it does not deserve.
                cur.execute(
                    f"SELECT c.id FROM {config.PG_SCHEMA}.code_list c"
                    f" WHERE c.list_code = 'school_grade' AND c.is_active"
                    f"   AND c.code = %s"
                    f"   AND c.legal_entity_code = ANY(%s)",
                    (str(_grade_val),
                     list(state.get("organization_entities")
                          or [state.get("legal_entity")])))
                _gids = [r[0] for r in cur.fetchall()]
                # UNIVERSAL: every 9999 row whose entity's grade scope covers this
                # ročník. The organisation's does (1..13), a sibling SŠ's does not.
                # Without this an organisation-wide chunk tagged "all ročníky" is
                # retrieved by NEITHER round — its array is non-empty, so the
                # grade-less round rejects it too, and the document disappears for
                # every caller whose ročník is known.
                cur.execute(
                    f"SELECT c.id FROM {config.PG_SCHEMA}.code_list c"
                    f" WHERE c.list_code = 'school_grade' AND c.is_active"
                    f"   AND c.code = '9999'"
                    f"   AND c.legal_entity_code = ANY(%s)"
                    f"   AND EXISTS (SELECT 1"
                    f"        FROM {config.PG_SCHEMA}.entity_grade_scope(c.legal_entity_code) s"
                    f"        JOIN {config.PG_SCHEMA}.code_list c2 ON c2.id = s.id"
                    f"       WHERE c2.code = %s)",
                    (list(state.get("organization_entities")
                          or [state.get("legal_entity")]), str(_grade_val)))
                _uids = [r[0] for r in cur.fetchall()]
            if _grade_val and (_gids or _uids):
                _rounds = [("grade", " AND t.school_grade_ids && %s::bigint[]", [_gids]),
                           ("universal",
                            " AND (t.school_grade_ids IS NULL"
                            " OR cardinality(t.school_grade_ids) = 0"
                            " OR t.school_grade_ids && %s::bigint[])", [_uids])]
            else:
                _rounds = [("all", "", [])]

            kept = 0
            kept_by_heading = 0
            _rows_total = 0
            _floor_dropped = 0
            _topical_dropped = 0
            # dedup across rounds (disjoint by grade, but also guards a chunk an
            # earlier step already placed) — one chunk = (file_item_uuid, index)
            _seen = {(c.get("file_item_uuid"), c.get("chunk_index")) for c in chunks}
            for _round_label, _grade_cond, _grade_params in _rounds:
                sql = f"""
                    SELECT t.id, t.file_uuid, t.file_item_uuid, t.chunk_index,
                           t.output_row_number, t.file_name,
                           m.source_url, m.original_file_name, m.ldts_gmt,
                           t.chunk_text,
                           {entry_text} AS chunk_reduced,
                           ts_rank_cd({tsexpr}, to_tsquery('simple', %s)) AS rank,
                           {concept_expr} AS concepts_hit,
                           {head_expr} AS heading_hits,
                           {head_text} AS heading_reduced,
                           t.section_heading, t.subsection_heading, t.heading_path,
                           t.school_grade_ids, t.school_level,
                           {_score_expr} AS bm25
                    FROM {config.CHUNKS_TABLE} t
                    JOIN {config.METADATA_TABLE} m
                      ON t.file_uuid = m.file_uuid
                     AND t.processing_uuid = m.processing_uuid
                     AND m.is_active = true
                 {_NOEXCL}
                    WHERE {_where_sql}{lang_cond}{cand_cond}{_grade_cond}
                    ORDER BY {_order_sql}
                    LIMIT %s
                """
                # the grade round scores on the grade-stripped query (grade is its
                # hard filter); every other round on the full generic-gated query
                _round_bm25 = _bm25_q_grade if _round_label == "grade" else _bm25_q
                _where_param = _round_bm25 if _bm25_on else tsq
                cur.execute(sql, [tsq, *concept_qs, *concept_qs, _where_param,
                                  *lang_params, *cand_params, *_grade_params, fetch_k])
                rows = cur.fetchall()
                _rows_total += len(rows)
                # rows come ordered bm25 DESC — the first row carries this
                # ROUND's best score, the reference for the relative floor
                _round_top_bm25 = (float(rows[0][-1]) if _bm25_on and rows
                                   and rows[0][-1] is not None else 0.0)
                for _pos, (chunk_id, file_uuid, file_item_uuid, chunk_index, output_row_number, file_name,
                           source_url, original_file_name, ldts_gmt, chunk_text, chunk_reduced,
                           rank, _concepts_hit, _heading_hits, heading_reduced,
                           section_heading, subsection_heading, heading_path,
                           school_grade_ids, school_level, bm25) in enumerate(rows, 1):
                    if (str(file_item_uuid), chunk_index) in _seen:
                        continue
                    cov = weighted_coverage(concept_sets, _tokens(chunk_reduced), idf_w)
                    # A discriminative query concept sitting in this chunk's HEADING.
                    # Computed ALWAYS (not only under the coverage floor): it feeds two
                    # jobs — (a) admission of a low-coverage chunk whose heading is on
                    # topic, and (b) the heading tier in answer._fused, where "the chunk
                    # whose HEADING is about the query" beats a same-grade chunk that
                    # merely repeats "ročník/6.". Gating it on cov<min_coverage hid it
                    # exactly when the chunk DID cover the query well — so on "pomůcky
                    # pro 6. ročník" the supply list lost the tier to the event calendar
                    # (grade 6, heading "Termíny akcí", zero "pomůcka"). df-share keeps
                    # it discriminative; a purely numeric concept never opens it.
                    by_heading, _cov_concepts = topical.label(
                        chunk_reduced, heading_reduced, _tctx)
                    # TOPICAL ADMISSION (see param above): no heading match and no
                    # covered concept the corpus considers discriminative → out,
                    # regardless of BM25 position. This intentionally applies to the
                    # round's TOP hit too — "škola" alone carried the grade round's
                    # best chunks on 'kdy je nejbližší škola v přírodě?' (Scio
                    # results, přihláška SŠ) and both were noise.
                    if (topical_admission and _bm25_on and _ntot and by_heading is None
                            and not any(_sh <= bm25_max_df_share
                                        and not str(_t).strip(".").isdigit()
                                        for _t, _sh in _cov_concepts)):
                        _topical_dropped += 1
                        continue
                    # BM25 mode: BM25 relevance is the gate, with a RELATIVE floor —
                    # a chunk far below the round's best hit is dropped unless a
                    # heading match or genuine concept coverage vouches for it (see
                    # bm25_min_ratio above). ts mode keeps the plain coverage floor.
                    if _bm25_on:
                        _b = float(bm25) if bm25 is not None else 0.0
                        if (_round_top_bm25 > 0 and _b < bm25_min_ratio * _round_top_bm25
                                and by_heading is None and cov < min_coverage):
                            _floor_dropped += 1
                            continue
                    elif cov < min_coverage and by_heading is None:
                        continue
                    if by_heading is not None:
                        kept_by_heading += 1
                    _seen.add((str(file_item_uuid), chunk_index))
                    chunks.append({
                        "retrieval_type": "document_lexical",
                        "chunk_id": chunk_id,
                        "file_uuid": str(file_uuid),
                        "file_item_uuid": str(file_item_uuid),
                        # (file_item_uuid, chunk_index) is the real chunk identity —
                        # one element is split into several chunks, so the uuid alone
                        # names a SECTION, not a chunk. output_row_number orders the
                        # elements inside the document (verified 1:1 with the element).
                        "chunk_index": chunk_index,
                        "output_row_number": output_row_number,
                        "file_name": str(file_name),
                        "source_url": source_url,
                        "original_file_name": original_file_name,
                        "ldts_gmt": ldts_gmt,
                        "chunk_text": chunk_text or "",
                        # headings live in their own columns (migration 61), so the
                        # body no longer contains them — carry them along or the
                        # prompt loses which section/grade the text belongs to.
                        "section_heading": section_heading,
                        "subsection_heading": subsection_heading,
                        "heading_path": heading_path,
                        "school_grade_ids": list(school_grade_ids or []),
                        "school_level": school_level,
                        "bm25": (round(float(bm25), 4) if bm25 is not None else None),
                        # score orders the CONTEXT selection in answer.py — it must
                        # agree with the candidate order above, or the motto chunk
                        # would be fetched and then out-sorted again. Coverage
                        # dominates; rank only breaks ties (rank is unbounded, so cap).
                        "score": round(cov + min(float(rank), rank_cap) * rank_weight, 6),
                        "coverage": round(cov, 3),
                        "heading_match": by_heading,
                        "concepts_covered": _cov_concepts,
                        # which round produced this chunk (grade | gradeless | all) —
                        # a diagnostic and a downstream tie-break signal
                        "grade_round": _round_label,
                        # position in this step's own candidate order — the fused
                        # ranking downstream compares POSITIONS, not the incomparable
                        # scores of the different gatherers. Per-round (both start at 1);
                        # the grade boost, not the position, decides grade vs grade-less.
                        "lexical_rank": _pos,
                    })
                    kept += 1
            logger.info("document_lexical: +%d/%d chunks kept over %d round(s) "
                        "(min_coverage=%.2f, by_heading=%d, bm25_floor=%.2f dropped=%d, "
                        "topical_dropped=%d, max_df_share=%.2f, pool=%d)",
                        kept, _rows_total, len(_rounds), min_coverage, kept_by_heading,
                        bm25_min_ratio, _floor_dropped, _topical_dropped,
                        heading_max_df_share, len(chunks))

        # ── typo fallback — see the parameter block at the top ───────────────
        # The score is a TYPO COVERAGE: mean over the query's surface tokens of
        # the best word_similarity against body or heading, so it reads on the
        # same 0..1 scale as ordinary coverage and the executor's pool-quality
        # gate treats it identically (a weak trgm pool still lets rag_vector
        # run and the fusion decide).
        if kept == 0 and trgm_fallback:
            ttoks = [t for t in (state.get("query_norm_tokens") or [])
                     if len(t) >= 4][:6]
            if ttoks:
                sim_parts, conds = [], []
                for _ in ttoks:
                    sim_parts.append(
                        "GREATEST(word_similarity(%s, t.chunk_norm_text), "
                        "word_similarity(%s, coalesce(t.section_heading_norm, '')))")
                    conds.append("(%s <%% t.chunk_norm_text "
                                 "OR %s <%% coalesce(t.section_heading_norm, ''))")
                sim_expr = "(" + " + ".join(sim_parts) + f") / {len(ttoks)}"
                tsql = f"""
                    SELECT t.id, t.file_uuid, t.file_item_uuid, t.chunk_index,
                           t.output_row_number, t.file_name,
                           m.source_url, m.original_file_name, m.ldts_gmt,
                           t.chunk_text, t.section_heading, t.subsection_heading,
                           t.heading_path, t.school_grade_ids, t.school_level,
                           {sim_expr} AS sim,
                           {entry_text} AS chunk_reduced,
                           {head_text} AS heading_reduced
                    FROM {config.CHUNKS_TABLE} t
                    JOIN {config.METADATA_TABLE} m
                      ON t.file_uuid = m.file_uuid
                     AND t.processing_uuid = m.processing_uuid
                     AND m.is_active = true
                 {_NOEXCL}
                    WHERE ({' OR '.join(conds)}){lang_cond}{cand_cond}
                    ORDER BY sim DESC
                    LIMIT %s
                """
                _pair = [p for tok in ttoks for p in (tok, tok)]
                with get_cursor() as cur:
                    # the <% operator matches at the session GUC threshold; LOCAL
                    # so the pooled connection hands it back clean
                    cur.execute("SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)",
                                (str(trgm_min_sim),))
                    # cand_params, NOT scope_params — the SQL above interpolates
                    # cand_cond, which carries the AREA fragment and therefore an
                    # extra placeholder. Passing the identity params alone left
                    # the statement one argument short, so under an explicit
                    # filter this branch raised `list index out of range`, the
                    # step's own except swallowed it, and the WHOLE document
                    # branch was silently lost — worse than the wrong rows,
                    # because nothing said so. Found 2026-09-06 in review; the
                    # tests had never driven the typo fallback with a scope.
                    cur.execute(tsql, [*_pair, *_pair, *lang_params,
                                       *cand_params, trgm_fetch_k])
                    trows = cur.fetchall()
                t_kept = 0
                for _pos, (chunk_id, file_uuid, file_item_uuid, chunk_index, output_row_number,
                           file_name, source_url, original_file_name, ldts_gmt,
                           chunk_text, section_heading, subsection_heading,
                           heading_path, school_grade_ids, school_level, sim,
                           chunk_reduced, heading_reduced) in enumerate(trows, 1):
                    sim = float(sim)
                    if sim < trgm_min_typo_coverage:
                        continue
                    # SAME TOPICAL LABELS AS THE MAIN PATH. Without them this branch
                    # was a precision blind spot: the focus gate and the heading tier
                    # in answer.py read heading_match / concepts_covered, so a pool
                    # made of trgm chunks left both inert (focus == []) and nothing
                    # could filter a tangential document out. Measured on "můžeme ve
                    # škole fotit?" — "fotit" is nowhere in the corpus ("fotografování"
                    # is), so the main path kept 0 chunks, trgm bridged the gap by
                    # trigram similarity (good) and then delivered 13 unlabelled
                    # chunks, which is how career counselling and a student interview
                    # earned paragraphs next to the school rules.
                    _t_by_heading, _t_cov_concepts = topical.label(
                        chunk_reduced, heading_reduced, _tctx)
                    # THE SAME TOPICAL GATE AS THE MAIN LOOP. It was applied only
                    # under `_bm25_on` above, so the typo fallback walked straight
                    # past it — and the fallback is exactly where a chunk enters on
                    # LETTER similarity rather than meaning. Measured 2026-08-26 on
                    # "můžeme ve škole fotit?" (question_answer_log 3295): the word
                    # `fotit` is nowhere in the corpus, trgm matched it against
                    # `fotodokumentace` in "Kavky Environmental Challenge", and the
                    # chunk arrived covering only `moci` (df 34 %) and `skola`
                    # (65 %) — both generic. It reached the context and the answer
                    # was written from it, while the actual photography rule sits
                    # in the SŠ chapter and is correctly out of a ZŠ pupil's reach.
                    # The right answer for that pupil is silence.
                    if (topical_admission and _ntot and _t_by_heading is None
                            and not any(_sh is not None and _sh <= bm25_max_df_share
                                        and not str(_t).strip(".").isdigit()
                                        for _t, _sh in (_t_cov_concepts or ()))):
                        _topical_dropped += 1
                        continue
                    chunks.append({
                        "retrieval_type": "document_trgm",
                        "chunk_id": chunk_id,
                        "file_uuid": str(file_uuid),
                        "file_item_uuid": str(file_item_uuid),
                        "chunk_index": chunk_index,
                        "output_row_number": output_row_number,
                        "file_name": str(file_name),
                        "source_url": source_url,
                        "original_file_name": original_file_name,
                        "ldts_gmt": ldts_gmt,
                        "chunk_text": chunk_text or "",
                        "section_heading": section_heading,
                        "subsection_heading": subsection_heading,
                        "heading_path": heading_path,
                        "school_grade_ids": list(school_grade_ids or []),
                        "school_level": school_level,
                        "score": round(sim, 6),
                        "coverage": round(sim, 3),
                        "heading_match": _t_by_heading,
                        "concepts_covered": _t_cov_concepts,
                        "lexical_rank": _pos,
                    })
                    t_kept += 1
                logger.info("document_lexical: trgm fallback +%d/%d (min_sim=%.2f, "
                            "typo_coverage>=%.2f, tokens=%s)",
                            t_kept, len(trows), trgm_min_sim,
                            trgm_min_typo_coverage, ttoks)
    except Exception as e:
        logger.warning("document_lexical: failed (%s)", e)

    # (BM25 is applied as the RETRIEVER + ranker inside the SELECT above when
    # bm25_lexical is on — no separate post-hoc re-rank step is needed here.)
    return {"rag_chunks": chunks, "topical_ctx": _tctx,
            "query_in_documents": _subject_here,
            "synonym_hint": _syn_hint}
