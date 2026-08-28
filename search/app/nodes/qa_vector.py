# last change in code by Jan Drozd and Martin Safranek (2026-08-21).
"""Node — qa_vector: semantic QA fallback that still returns a curated answer.

Runs only after qa_lexical fell through (C) AND document_lexical gathered its chunks
— it is the LAST curated attempt, the gate right before the generated RAG
fallback (variant A: curated beats generated). Embeds the query with the active
registry model, cosine-searches active qa_embedding, and if the best hit clears
QA_VECTOR_COSINE_MIN returns that entry's curated answer (no LLM). Otherwise it
falls through to rag_vector → answer(LLM).
"""

import logging

from ..llm import get_embedder

from ..state import SearchState
from ..db import get_cursor
from .. import config
from .. import scope
from .qa_common import pack_filter, sources_json

logger = logging.getLogger(__name__)


def qa_vector(state: SearchState, params: dict | None = None) -> dict:
    params = params or {}
    cosine_min = float(params.get("cosine_min", config.QA_VECTOR_COSINE_MIN))
    # How many neighbours proximity gets to choose among, and how much further than
    # the nearest one a candidate may sit and still count as equally close.
    fetch_k = int(params.get("fetch_k", config.QA_VECTOR_FETCH_K))
    tier_band = float(params.get("tier_band", config.QA_VECTOR_TIER_BAND))
    question = state.get("user_question", "") or ""
    if not question.strip():
        return {"resolved": False}

    pack_ids = state.get("pack_ids", [])
    frag, pack_params = pack_filter(pack_ids, alias="e", language=state.get("language_filter"))
    # On the EMBEDDING row, not on the joined entry: the nearest-neighbour scan walks
    # outwards and joins per candidate, so a predicate on qa_entries would be
    # evaluated after the index had already offered rows the caller may not see.
    scope_cond, scope_params = scope.where(
        "q", state.get("entity_scope") or [],
        state.get("organization_entities") or [], state.get("user_id"),
        state.get("user_role"), validity=True)   # migration 140

    try:
        with get_cursor() as cur:
            emb_model = config.EMBEDDING_MODEL
            cur.execute(
                f"SELECT embedding_model FROM {config.EMBEDDING_REGISTRY_TABLE} "
                "WHERE status = 'active' LIMIT 1"
            )
            row = cur.fetchone()
            if row and row[0]:
                emb_model = row[0]

            # Is there anything to search? Embedding the query is the single most
            # expensive act in the whole cascade — 868 to 2500 ms measured, against
            # 1 to 12 ms for every deterministic step — and with an empty QA pack it
            # buys nothing: the query below can only return no rows. One indexed
            # existence check costs under a millisecond and skips it.
            #
            # This is not a corner case: a deployment that ingests documents but has
            # not loaded a QA pack paid that latency on EVERY query.
            cur.execute(
                f"SELECT 1 FROM {config.QA_EMBEDDING_TABLE} q "
                f"WHERE q.is_active AND q.embedding_model = %s LIMIT 1", [emb_model])
            if cur.fetchone() is None:
                logger.info("qa_vector: no active QA embeddings for model %s — "
                            "skipped without embedding the query", emb_model)
                return {"resolved": False}

            # reuse the query embedding if an earlier step already computed it;
            # otherwise embed once and hand it downstream via state["query_vec"]
            vec = state.get("query_vec")
            if vec is None:
                vec = get_embedder(emb_model).embed_query(question)
            vec_str = "[" + ",".join(map(str, vec)) + "]"

            # WHICH pack wins when several in scope answer the same question. This
            # was ORDER BY distance LIMIT 1, which migration 142 already named as
            # arbitrary ("qa_exact/qa_vector return LIMIT 1") and b127ea9 fixed for
            # qa_exact only. It matters here because what is embedded is the
            # QUESTION, and the question is deliberately identical across an
            # organisation's pack and its units' — only the answer differs, and the
            # answer is not embedded. Measured on production: the three candidates
            # for "Jak se omlouvá absence?" sat within 1.1e-4 of each other, two of
            # them bit-identical, so the winner was float noise from the embedding
            # provider — stable noise, so a ZŠ pupil got the organisation's generic
            # answer every single time until the pack was re-embedded.
            #
            # So: proximity decides, but ONLY among candidates that are equally
            # close. Unlike qa_exact, where the matched text is identical by
            # definition, here a genuinely better semantic match must keep winning —
            # hence the band rather than an outright tier. entry_id last, so two
            # packs of ONE entity do not flip between requests (still the ročník
            # axis, still not built — see migration 159).
            #
            # Ordering only. Eligibility remains scope_cond's and pack_filter's, so
            # this cannot surface a row that was not already visible.
            own = state.get("legal_entity")
            org = list(state.get("organization_entities") or [])
            sql = f"""
                WITH cand AS (
                    SELECT q.entry_id, e.answer_md, e.asset_refs, q.legal_entity_code,
                           q.embedding <=> %s::vector AS dist
                    FROM {config.QA_EMBEDDING_TABLE} q
                    JOIN {config.QA_ENTRIES_TABLE} e ON e.entry_id = q.entry_id
                    WHERE q.is_active AND {frag}{scope_cond}
                      AND q.embedding_model = %s
                    ORDER BY q.embedding <=> %s::vector
                    LIMIT %s
                )
                SELECT entry_id, answer_md, asset_refs, 1 - dist AS sim
                FROM cand
                WHERE dist <= (SELECT min(dist) FROM cand) + %s
                ORDER BY CASE
                           WHEN legal_entity_code = %s      THEN 0
                           WHEN legal_entity_code = ANY(%s) THEN 1
                           ELSE 2
                         END,
                         dist, entry_id
                LIMIT 1
            """
            cur.execute(sql, [vec_str, *pack_params, *scope_params,
                              emb_model, vec_str, fetch_k, tier_band, own, org])
            hit = cur.fetchone()
    except Exception as e:
        logger.warning("qa_vector: failed (%s) — falling through to RAG", e)
        return {"resolved": False}

    if not hit:
        logger.info("qa_vector: no active QA embeddings for scope")
        return {"resolved": False, "query_vec": vec}

    entry_id, answer_md, asset_refs, sim = str(hit[0]), hit[1], hit[2], float(hit[3])
    logger.info("qa_vector: best sim=%.3f (min=%.2f) entry=%s",
                sim, cosine_min, entry_id)
    if sim < cosine_min:
        return {"resolved": False, "match_score": round(sim, 4), "query_vec": vec}

    # Related questions next to the answer: qa_vector fetches only the single
    # nearest entry, so it has no ranking of its own to offer — but qa_lexical
    # ran before it and parked its nearest entries (deferred_suggest /
    # fallback_suggestions). Reused here, minus the entry being answered.
    _parked = ((state.get("deferred_suggest") or {}).get("suggestions")
               or state.get("fallback_suggestions") or [])
    return {
        "resolved": True, "outcome": "answer", "answer_source": "qa_vector", "query_vec": vec,
        "matched_entry_id": entry_id, "match_score": round(sim, 4),
        "answer": answer_md or "", "sources_json": sources_json(asset_refs),
        "suggestions": [s for s in _parked if s.get("entry_id") != entry_id],
        "user_language": state.get("query_language"),
    }
