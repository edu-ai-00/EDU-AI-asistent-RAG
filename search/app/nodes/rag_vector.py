# last change in code by Martin Safranek (2026-08-22).
"""Node — rag_vector: semantic chunk retrieval over document_chunks (gather-only).

Absolute last-resort retrieval (vector = last resort). Runs only after QA lexical
(C), document_lexical, and qa_vector all failed to resolve. Embeds the query with the
active registry model and appends the nearest chunks to state["rag_chunks"]; the
terminal `answer` LLM then generates from the whole pool.

Adapted from the previous parallel-RRF vector_search: same registry lookup and
'[...]'::vector literal, but it only gathers (no scoring/RRF, no early answer).
document_chunks is empty until ingestion, so today this is a no-op.
"""

import logging

from ..llm import get_embedder

from ..state import SearchState
from ..db import get_cursor
from .. import config
from .. import scope
from .. import topical

logger = logging.getLogger(__name__)

# documents whose TYPE never answers (search_role='excluded', migration 172)
# must not enter retrieval at all — see scope.not_excluded()
_NOEXCL = scope.not_excluded("m")


def rag_vector(state: SearchState, params: dict | None = None) -> dict:
    if state.get("resolved"):
        return {}
    params = params or {}
    fetch_k = int(params.get("fetch_k", config.VECTOR_FETCH_K))
    top_k = int(params.get("top_k", config.VECTOR_TOP_K))
    # relevance gate: drop neighbours below this cosine similarity so an off-topic
    # query (no lexical/keyword hit either) ends with an empty pool → no_answer.
    # None / absent = no gate (previous behaviour).
    cosine_min = params.get("cosine_min", None)
    question = state.get("user_question", "") or ""
    # QUERY-SIDE grade enrichment: when the caller's grade is known but the
    # question text does not name it, prepend it so the embedding carries the
    # grade the chunk's heading text already does ("Pro 6. ročník: …" matches a
    # chunk headed "… 2. stupeň / Třída – 6."). Measured: raw "Jaké pomůcky si
    # máme vzít do školy?" put the 6th-grade supply list at cosine rank 48 (below
    # fetch_k), while naming the grade moved it to rank 1. This is the query-side
    # mirror of the ingest's chunk_embed_text (which already embeds the heading).
    # Off → raw question. Skipped when the grade is already written in the text.
    _grade = state.get("question_grade") or state.get("user_school_grade")
    _grade_enrich = params.get("grade_query_enrich", True)
    _embed_question = question
    if _grade_enrich and _grade and str(_grade) not in question:
        _tmpl = params.get("grade_query_prefix", "Pro {grade}. ročník: ")
        _embed_question = _tmpl.format(grade=_grade) + question
    lang = state.get("language_filter")
    # filter on the language the tokens were ACTUALLY reduced in (chunk-level
    # langdetect_result = user code > shared detection), metadata as fallback
    lang_cond = (" AND coalesce(t.langdetect_result, m.document_language_user,"
                 " m.document_language_llm) = %s") if lang else ""
    lang_params = [lang] if lang else []
    scope_cond, scope_params = scope.where(
        "t", state.get("entity_scope") or [],
        state.get("organization_entities") or [], state.get("user_id"),
        state.get("user_role"), validity=True)
    # TOPICAL LABELS (see topical.py): the gates in `answer` read heading_match
    # and concepts_covered, and this step used to set neither — so a pool whose
    # top chunk came from the vector left the focus gate without an anchor. The
    # context is built by document_lexical (it already has the concept sets and
    # the df map) and travels in the state; without it labelling is skipped and
    # behaviour is exactly as before.
    _tctx = state.get("topical_ctx")
    _form = (_tctx or {}).get("form") or []
    _red_body = topical.body_sql(_form, "t") if _form else "''"
    _red_head = topical.heading_sql(_form, "t") if _form else "''"
    chunks = list(state.get("rag_chunks", []))
    if not question.strip():
        return {"rag_chunks": chunks}

    try:
        with get_cursor() as cur:
            emb_model, chunks_table = config.EMBEDDING_MODEL, config.CHUNKS_TABLE
            cur.execute(
                f"SELECT embedding_model, target_table FROM {config.EMBEDDING_REGISTRY_TABLE} "
                "WHERE status = 'active' LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                emb_model = row[0] or emb_model
                chunks_table = row[1] or chunks_table

            # reuse qa_vector's embedding ONLY when we embed the RAW question;
            # a grade-enriched query must be embedded fresh (query_vec is the raw one)
            if _embed_question != question:
                vec = get_embedder(emb_model).embed_query(_embed_question)
            else:
                vec = state.get("query_vec")
                if vec is None:
                    vec = get_embedder(emb_model).embed_query(question)
            vec_str = "[" + ",".join(map(str, vec)) + "]"

            sql = f"""
                SELECT t.id, t.file_uuid, t.file_item_uuid, t.chunk_index,
                       t.output_row_number, t.file_name,
                       m.source_url, m.original_file_name, m.ldts_gmt,
                       t.chunk_text,
                       1 - (t.embedding <=> %s::vector) AS sim,
                       t.section_heading, t.subsection_heading, t.heading_path,
                       t.school_grade_ids, t.school_level,
                       {_red_body} AS chunk_reduced,
                       {_red_head} AS heading_reduced
                FROM {chunks_table} t
                JOIN {config.METADATA_TABLE} m
                  ON t.file_uuid = m.file_uuid
                 AND t.processing_uuid = m.processing_uuid
                 AND m.is_active = true
                 {_NOEXCL}
                WHERE t.embedding_model = %s{lang_cond}{scope_cond}
                ORDER BY t.embedding <=> %s::vector
                LIMIT %s
            """
            cur.execute(sql, [vec_str, emb_model, *lang_params, *scope_params,
                              vec_str, fetch_k])
            rows = cur.fetchall()
        # A chunk already gathered by a lexical step used to be SKIPPED here, so
        # the semantic opinion about it was thrown away: "Muze se ve skole fotit?"
        # has the right chunk 3rd of 503 by cosine, yet it kept the lexical score
        # it shared with ~30 others and never reached the prompt. The vector step
        # now ANNOTATES what the pool already holds (rank + cosine) instead of
        # ignoring it; the ranking downstream fuses the positions.
        # chunk identity is (file_item_uuid, chunk_index) — the uuid alone names
        # a SECTION: keyed on it, a DIFFERENT chunk of an element already in the
        # pool was never added, only mis-annotated onto the pool's chunk
        # (document_lexical's _seen learned the same lesson long ago)
        by_item = {(c.get("file_item_uuid"), c.get("chunk_index")): c for c in chunks}
        # ELEMENT-level corroboration: before the exact-chunk keying above, a
        # vector hit on a DIFFERENT chunk of an element already in the pool
        # silently annotated the pool's chunk — wrong text, but it made
        # lexical+vector agreement on the SECTION count in the fusion, and Q2
        # ("uvolnění na 14 dní") lived off exactly that. Keep the agreement as
        # an explicit, separate signal: pool chunks of the same element get
        # vector_element_rank (fused with its own weight), the REAL chunk is
        # still added below.
        by_elem: dict = {}
        for c in chunks:
            by_elem.setdefault(c.get("file_item_uuid"), []).append(c)
        _vec_added_elems: set = set()
        added = 0
        for rank, (chunk_id, file_uuid, file_item_uuid, chunk_index, output_row_number, file_name,
                   source_url, original_file_name, ldts_gmt,
                   chunk_text, sim, section_heading,
                   subsection_heading, heading_path, school_grade_ids, school_level,
                   chunk_reduced, heading_reduced) in enumerate(rows, 1):
            if cosine_min is not None and float(sim) < float(cosine_min):
                continue  # below relevance gate -> skip (off-topic protection)
            known = by_item.get((str(file_item_uuid), chunk_index))
            if known is not None:
                # first (= best) semantic hit wins; later siblings must not
                # overwrite the rank with a worse one
                if not known.get("vector_rank"):
                    known["vector_rank"] = rank
                    known["cosine"] = float(sim)
                continue
            _elem = str(file_item_uuid)
            for _sib in by_elem.get(_elem, []):
                if not _sib.get("vector_rank"):
                    _sib["vector_element_rank"] = min(
                        _sib.get("vector_element_rank") or rank, rank)
            # ONE VECTOR-ADDED chunk per element: siblings of an element the
            # vector already voted for don't consume the top_k budget — without
            # this cap one similar SECTION (SŠ Článek IX = 4 chunks) filled the
            # whole budget and the scan never reached the element holding the
            # answer (Q2 regression, 2026-08-09). The cap counts VECTOR adds
            # only: an element seeded by the LEXICAL step may still receive one
            # vector chunk — the lexical pick can be a DIFFERENT part of the
            # section than the answer (grade round pulled the docházka intro
            # 1369 while the 14-day uvolnění text lives in 1370; folding the
            # vector hit into a corroboration annotation erased the answer).
            if _elem in _vec_added_elems:
                continue
            # same two labels the lexical paths produce, so the focus gate and the
            # heading tier can judge a vector-found chunk too (topical.py)
            _v_hm, _v_cc = topical.label(chunk_reduced, heading_reduced, _tctx)
            chunks.append({
                "vector_rank": rank,
                "cosine": float(sim),
                "heading_match": _v_hm,
                "concepts_covered": _v_cc,
                "retrieval_type": "rag_vector",
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
                "score": float(sim),
            })
            by_item[(str(file_item_uuid), chunk_index)] = chunks[-1]
            by_elem.setdefault(_elem, []).append(chunks[-1])
            _vec_added_elems.add(_elem)
            added += 1
            if added >= top_k:
                break
        logger.info("rag_vector: +%d chunks (pool=%d, cosine_min=%s)",
                    added, len(chunks), cosine_min)
    except Exception as e:
        logger.warning("rag_vector: failed (%s)", e)
    return {"rag_chunks": chunks}
