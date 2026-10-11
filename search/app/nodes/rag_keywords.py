# last change in code by Jan Drozd and Martin Safranek (2026-08-22).
"""Node — rag_keywords: keyword-array retrieval over document_chunks.keywords (gather-only).

Matches the query's reduced-form tokens against the LLM-extracted `keywords[]` of
each chunk; a chunk is a candidate when at least `min_overlap` query tokens appear
among its keywords. The stored keywords are surface forms, so they are reduced
through the SAME build_forms() contract as the query before comparison (query↔index
parity) — no re-ingest needed.

Keywords are a first-class, HIGH-PRECEDENCE signal: they are distilled from the
chunk's real content, so — unlike a heading, which can carry boilerplate that the
body never mentions — a keyword hit means the chunk is genuinely about the concept.
This step therefore runs on EVERY query (not a last-resort fallback) and ANNOTATES
the chunks the pool already holds with `keyword_rank` (best keyword overlap first),
adding keyword-only matches too. answer.py's fusion weights `keyword_rank` above the
lexical/vector ranks, so a keyword match stands on its own as strong evidence.

Gather-only: writes to state["rag_chunks"]; the keywords column lives ON
document_chunks (merged 2026-08-07), so there is no separate table to join.
"""

import json
import logging
from functools import lru_cache

from ..state import SearchState
from ..db import get_cursor
from .. import config
from .. import scope
from .. import topical
from ..text_norm import build_forms
from ..languages import active_languages
from .qa_common import resolve_form, form_query_tokens, load_synonym_groups

logger = logging.getLogger(__name__)

# documents whose TYPE never answers (search_role='excluded', migration 172)
# must not enter retrieval at all — see scope.not_excluded()
_NOEXCL = scope.not_excluded("m")


@lru_cache(maxsize=65536)
def _kw_tokens_cached(keyword: str, form_key: tuple, lang: str, cfg_key: str) -> frozenset:
    """Reduce one stored keyword to the chosen form(s), same contract as the query.

    Cached for the process lifetime: the corpus keyword vocabulary is small and
    repeats across chunks, but the reduction (simplemma + stemmer) used to run
    for EVERY keyword of EVERY candidate row on EVERY query — the reason the
    candidate window had to stay small in the first place.

    `cfg_key` is the stoplist + engines as a canonical JSON string: hashable, so
    it can live in the cache key, which means editing edu_ai.qa_stopword
    invalidates the affected entries instead of leaving a process-lifetime cache
    built from the old vocabulary. Until 2026-09-05 this passed cfg=None while
    the module docstring claimed the SAME build_forms() contract as the query —
    the query carried the číselník, this did not."""
    forms = build_forms(keyword or "", json.loads(cfg_key), lang)
    toks: set[str] = set()
    for f in form_key:
        toks |= set(forms.get(f, []) or [])
    return frozenset(toks)


def _kw_tokens(keyword: str, form, lang: str, cfg_key: str) -> frozenset:
    return _kw_tokens_cached(keyword or "", tuple(form), lang or "", cfg_key)


def _cfg_key(cfg: dict | None) -> str:
    """Canonical, hashable form of the parts of cfg that change the reduction."""
    c = cfg or {}
    return json.dumps({"normalization": c.get("normalization") or {},
                       "engines": c.get("engines") or []}, sort_keys=True)


def rag_keywords(state: SearchState, params: dict | None = None) -> dict:
    if state.get("resolved"):
        return {}
    params = params or {}
    form = resolve_form(params)
    min_overlap = int(params.get("min_overlap", 1))
    fetch_k = int(params.get("fetch_k", config.KEYWORD_FETCH_K))
    # TOPICAL ADMISSION (2026-08-09, same rule as document_lexical): an overlap
    # made ONLY of corpus-generic tokens is evidence of nothing — "škola" is a
    # stored keyword on half the corpus, and it alone pulled five šikana chunks
    # into 'kdy je nejbližší škola v přírodě?'. Require at least one token the
    # corpus considers discriminative (df share within topical_max_df_share).
    topical_admission = bool(params.get("topical_admission", True))
    topical_max_df_share = float(params.get("topical_max_df_share", 0.25))

    # The stoplist + engines the STORED keywords must be reduced with — the same
    # ones the query went through in prepare. Resolved once per call: it is the
    # cache key for every keyword below.
    from ..settings import get_active_setting
    _ckey = _cfg_key(get_active_setting(state.get("legal_entity"))["config"])

    qtokens = form_query_tokens(state, form)
    chunks = list(state.get("rag_chunks", []))
    # bound before the try below: the except path returns it too
    keywords_used: list[str] = []
    if not qtokens:
        return {"rag_chunks": chunks, "keywords_used": keywords_used}

    lang = state.get("language_filter")
    # filter on the language the tokens were ACTUALLY reduced in (chunk-level
    # langdetect_result = user code > shared detection), metadata as fallback
    lang_cond = (" AND coalesce(c.langdetect_result, m.document_language_user,"
                 " m.document_language_llm) = %s") if lang else ""
    lang_params = [lang] if lang else []
    scope_cond, scope_params = scope.where(
        "c", state.get("entity_scope") or [],
        state.get("organization_entities") or [], state.get("user_id"),
        state.get("user_role"), validity=True)
    # Disabled in every active setting today (migration 200) — and filtered
    # anyway, deliberately: a step that is off is a step somebody will switch on,
    # and an area filter missing from the one path nobody exercises is exactly
    # how this kind of rule rots. Tested with the step forced on in an isolated
    # fixture, not by changing a production setting.
    dom_cond, dom_params = scope.domain_where("c", "document_domain_id",
                                              state.get("domain_ids"))
    # candidates get the area; the df statistics below keep the identity-only
    # predicate — see the long note in document_lexical for why the two differ
    cand_cond = scope_cond + dom_cond
    cand_params = [*scope_params, *dom_params]
    # Stored keywords are kept RAW and reduced HERE, so they must be reduced in
    # the QUERY's language for their tokens to meet the query tokens on the same
    # forms. When the query language is undecided, reduce in EVERY candidate
    # language and union — same reasoning as prepare()'s _union_forms: picking
    # one would silently favour it (this line used to end in `or "cs"`).
    qlangs = ([state.get("query_language")] if state.get("query_language")
              else ([lang] if lang else list(active_languages())))

    # TOPICAL LABELS — see topical.py; ctx comes from document_lexical via state
    _tctx = state.get("topical_ctx")
    _form = (_tctx or {}).get("form") or []
    _red_body = topical.body_sql(_form, "c") if _form else "''"
    _red_head = topical.heading_sql(_form, "c") if _form else "''"
    # keywords are a COLUMN on document_chunks now (merged 2026-08-07 — the old
    # document_chunk_keywords table was 1:1 and ~90% duplicated), so no JOIN.
    sql = f"""
        SELECT c.id, c.file_uuid, c.file_item_uuid, c.chunk_index, c.output_row_number,
               c.file_name, m.source_url, m.original_file_name, m.ldts_gmt,
               c.chunk_text, c.keywords,
               c.section_heading, c.subsection_heading, c.heading_path,
               c.school_grade_ids, c.school_level,
               {_red_body} AS chunk_reduced,
               {_red_head} AS heading_reduced
        FROM {config.CHUNKS_TABLE} c
        JOIN {config.METADATA_TABLE} m
          ON c.file_uuid = m.file_uuid
         AND c.processing_uuid = m.processing_uuid
         AND m.is_active = true
         {_NOEXCL}
        WHERE c.keywords IS NOT NULL AND array_length(c.keywords, 1) > 0{lang_cond}{cand_cond}
        ORDER BY c.output_row_number
        LIMIT %s
    """
    use_synonyms = params.get("synonyms", True)
    try:
        with get_cursor() as cur:
            cur.execute(sql, [*lang_params, *cand_params, fetch_k])
            rows = cur.fetchall()
            # the SQL window is ordered by output_row_number (document order),
            # not by relevance — a FULL window means candidates were cut blind
            if len(rows) >= fetch_k:
                logger.warning("rag_keywords: candidate window FULL (%d rows) — "
                               "chunks beyond the cap were cut in DOCUMENT order, "
                               "not relevance; raise fetch_k / KEYWORD_FETCH_K",
                               len(rows))
            # SYNONYM EXPANSION (parity with document_lexical, 2026-08-09): an
            # entity group (JPZ = přijímačky = jednotná přijímací zkouška) whose
            # token appears in the query extends the matching vocabulary — the
            # stored keyword may use the other name of the same thing. Only
            # entity groups: interrogatives never carry content (same reasoning
            # as expand_query's tsquery).
            _expansion: set[str] = set()
            if use_synonyms:
                try:
                    _groups, _kind, _, _terms = load_synonym_groups(
                        cur, next((l for l in qlangs if l), None))
                    # term-level wake-up, same rule as the other two retrievers
                    for _gid, _tl in (_terms or {}).items():
                        if _kind.get(_gid) == "entity" and any(
                                ts and ts <= qtokens for ts in _tl):
                            _expansion |= _groups.get(_gid, set())
                except Exception as e:
                    logger.warning("rag_keywords: synonym load failed (%s)", e)
            _match_tokens = qtokens | _expansion
            # which query tokens are discriminative in the caller's visible corpus
            # (same df measure as document_lexical's bm25 gate; digits are ordinals,
            # never topics). If EVERY query token is generic the guard stands down —
            # never strip the query to empty.
            _topical_q: set = set()
            if topical_admission:
                # measured over the EXPANDED vocabulary: a rare synonym of a
                # topical query token ("přijímačky" for "JPZ") is topical too
                _qt = sorted({t for t in _match_tokens if not str(t).strip(".").isdigit()})
                if _qt:
                    cur.execute(
                        f"SELECT count(*) FROM {config.CHUNKS_TABLE} c "
                        f"JOIN {config.METADATA_TABLE} m ON c.file_uuid = m.file_uuid "
                        f" AND c.processing_uuid = m.processing_uuid AND m.is_active = true {_NOEXCL}"
                        f"WHERE true{lang_cond}{scope_cond}", [*lang_params, *scope_params])
                    _ntot = cur.fetchone()[0] or 0
                    if _ntot:
                        cur.execute(
                            f"SELECT tok, (SELECT count(*) FROM {config.CHUNKS_TABLE} c "
                            f"  JOIN {config.METADATA_TABLE} m ON c.file_uuid = m.file_uuid "
                            f"   AND c.processing_uuid = m.processing_uuid AND m.is_active = true {_NOEXCL}"
                            f"  WHERE c.bm25_text @@@ tok{lang_cond}{scope_cond}) AS df "
                            f"FROM unnest(%s::text[]) AS tok",
                            [*lang_params, *scope_params, _qt])
                        _topical_q = {t for t, d in cur.fetchall()
                                      if (d or 0) / _ntot <= topical_max_df_share}
        # Compute the keyword overlap for every candidate, then rank the MATCHES by
        # how many query concepts their keywords cover. keyword_rank drives the fused
        # ranking downstream (weighted HIGHEST, above lexical/vector): the extracted
        # keywords are distilled from the chunk's real content, so — unlike a heading,
        # which can say anything — a keyword hit means the chunk is genuinely about
        # the concept. A keyword match therefore stands on its own as strong evidence.
        matched = []
        _generic_only = 0
        for (chunk_id, file_uuid, file_item_uuid, chunk_index, output_row_number, file_name,
             source_url, original_file_name, ldts_gmt, chunk_text, keywords,
             section_heading, subsection_heading, heading_path, school_grade_ids, school_level,
             chunk_reduced, heading_reduced) in rows:
            kwset: set[str] = set()
            for kw in (keywords or []):
                for ql in qlangs:
                    kwset |= _kw_tokens(kw, form, ql, _ckey)
            overlap = _match_tokens & kwset
            if len(overlap) < min_overlap:
                continue
            if _topical_q and not (overlap & _topical_q):
                _generic_only += 1
                continue
            matched.append((len(overlap), output_row_number, {
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
                # carried only to compute the topical labels below, then popped
                "_reduced_body": chunk_reduced,
                "_reduced_head": heading_reduced,
                "keyword_match": sorted(overlap),
                # capped: synonym expansion can make the raw overlap exceed the
                # query's own token count, and coverage is a 0..1 fraction
                "coverage": round(min(1.0, len(overlap) / max(1, len(qtokens))), 3),
            }))
        # best overlap first; output_row_number only breaks ties (stable)
        matched.sort(key=lambda t: (-t[0], t[1]))
        # annotate the chunk the pool ALREADY holds, or add it — same pattern as
        # rag_vector: a chunk found by BOTH a lexical step and by keywords must
        # carry both ranks so the fusion rewards the agreement, not overwrite one.
        by_key = {(c.get("file_item_uuid"), c.get("chunk_index")): c for c in chunks}
        added = 0
        for rank, (_ov, _orn, cand) in enumerate(matched, 1):
            key = (cand["file_item_uuid"], cand["chunk_index"])
            known = by_key.get(key)
            if known is not None:
                known["keyword_rank"] = rank
                known["keyword_match"] = cand["keyword_match"]
                # keep the stronger coverage signal
                if (cand["coverage"] or 0) > (known.get("coverage") or 0):
                    known["coverage"] = cand["coverage"]
                continue
            cand["retrieval_type"] = "rag_keywords"
            # the two labels the gates in `answer` read (topical.py)
            cand["heading_match"], cand["concepts_covered"] = topical.label(
                cand.pop("_reduced_body", ""), cand.pop("_reduced_head", ""), _tctx)
            cand["keyword_rank"] = rank
            cand["score"] = float(_ov)
            chunks.append(cand)
            added += 1
        logger.info("rag_keywords: %d matched (+%d new, generic_only_dropped=%d, pool=%d)",
                    len(matched), added, _generic_only, len(chunks))
        # Which query concepts actually hit — surfaced as the response's
        # key_words_used so a caller can see WHY these documents came back.
        keywords_used = sorted({k for _ov, _orn, c in matched
                                for k in (c.get("keyword_match") or [])})
    except Exception as e:
        logger.warning("rag_keywords: failed (%s)", e)
    return {"rag_chunks": chunks, "keywords_used": keywords_used}
