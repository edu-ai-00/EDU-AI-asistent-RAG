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
from .qa_common import (pack_filter, domain_filter, sources_json,
                        finalize_chips, resolve_form)

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
    # ON THE EMBEDDING ROW, and inside the `cand` CTE below — i.e. BEFORE
    # `ORDER BY embedding <=> query LIMIT fetch_k`. The whole point of migration
    # 144 was that this scan hands candidates outwards from the index: a
    # predicate applied after the LIMIT would silently shrink an already-chosen
    # top-k, so a narrow area could come back empty while the area holds a
    # perfectly good answer that sat at position k+1.
    dom_cond, dom_params = domain_filter(state, "q")

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
                    WHERE q.is_active AND {frag}{scope_cond}{dom_cond}
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
            cur.execute(sql, [vec_str, *pack_params, *scope_params, *dom_params,
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
    # nearest entry, so it has no ranking of its own to offer — but qa_lexical ran
    # before it and parked its nearest entries (deferred_suggest). Those go through
    # the same three rules as every other path: a cross-pack chip needs a higher
    # score, a short row is topped up from the answered entry's category, and the
    # row is ordered by the number it shows. Until 2026-09-04 this path had none of
    # them, and it is the one that answers the vaguest questions.
    #
    # fallback_suggestions is deliberately NOT the second choice: it is qa_lexical's
    # nearest entries with no bar at all, and next to a curated answer that showed
    # as "Lze mít na lavici pití?" under a question about an IVP (0.15, production).
    _parked = [s for s in ((state.get("deferred_suggest") or {}).get("suggestions") or [])
               if s.get("entry_id") != entry_id]
    _top_n = int(params.get("suggest_top_n") or config.QA_SUGGEST_TOP_N)
    try:
        with get_cursor() as cur:
            _chips = finalize_chips(cur, state, params, entry_id, _parked, _top_n,
                                    resolve_form(params))
    except Exception as e:
        # a suggestion is an offer, never the answer — it must not cost one
        logger.warning("qa_vector: chips failed (%s) — parked list as it came", e)
        _chips = _parked[:_top_n]
    return {
        "resolved": True, "outcome": "answer", "answer_source": "qa_vector", "query_vec": vec,
        "matched_entry_id": entry_id, "match_score": round(sim, 4),
        "answer": answer_md or "", "sources_json": sources_json(asset_refs),
        "suggestions": _chips,
        "user_language": state.get("query_language"),
    }
