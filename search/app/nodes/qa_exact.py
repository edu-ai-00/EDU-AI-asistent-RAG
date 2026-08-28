# last change in code by Jan Drozd and Martin Safranek (2026-08-20).
"""Node — qa_exact: exact QA match (online fast path).

Deterministic. If an active variant's whole normalized/lemmatized form equals
the query's, return that entry's curated answer immediately — no scoring, no
LLM. Order-sensitive string equality on the space-joined token forms is a strong
"the user typed (almost) the canonical question" signal.
"""

import logging

from ..state import SearchState
from ..db import get_cursor
from .. import config
from .. import scope
from .qa_common import (pack_filter, fetch_answer, sources_json, resolve_form,
                        form_text_col, form_token_key)

logger = logging.getLogger(__name__)


def qa_exact(state: SearchState, params: dict | None = None) -> dict:
    form = resolve_form(params)
    conds, vals = [], []
    for f in form:
        toks = " ".join(state.get(form_token_key(f), []) or [])
        if toks:
            conds.append(f"v.{form_text_col(f)} = %s")
            vals.append(toks)
    if not conds:
        return {"resolved": False}

    pack_ids = state.get("pack_ids", [])
    frag, pack_params = pack_filter(pack_ids, alias="e", language=state.get("language_filter"))
    # A curated answer is as much this entity's material as a document is; the same
    # three levels apply and are read off the VARIANT row, which carries them
    # (migration 79) so the join to qa_entries is not what protects them.
    scope_cond, scope_params = scope.where(
        "v", state.get("entity_scope") or [],
        state.get("organization_entities") or [], state.get("user_id"),
        # validity: the columns landed with migration 140 — before that this
        # call could not have carried it, which is why an expired curated answer
        # could outrank a valid document (QA wins over RAG in the cascade)
        state.get("user_role"), validity=True)

    # v.is_active is not redundant with e.is_active in `frag`: the ts_*/trgm
    # indexes are PARTIAL WHERE is_active (migration 149), and Postgres can only
    # use a partial index when the query itself implies its predicate. A condition
    # on the JOINED entry does not. Drop this and the scan silently goes
    # sequential — right answers, wrong plan.
    # WHICH pack wins when several in scope hold the same question. Until now this
    # was LIMIT 1 with no ORDER BY, so the answer was whatever the planner returned
    # first — and migration 142 said as much ("qa_exact/qa_vector return LIMIT 1,
    # so the winner would be arbitrary") while only adding a report for the
    # one-school case. Measured on production: a ZŠ pupil asking the exact question
    # got the ORGANISATION's generic "podle školního řádu příslušné součásti"
    # instead of their own school's concrete answer.
    #
    # Same proximity axis qa_lexical already applies in _tier(): own school before
    # the organisation before anyone else. Not the ročník axis — the text is
    # identical here by definition, so entity is what distinguishes the candidates;
    # a school holding both a general and a ročník-specific pack for one question
    # is the second-order case and wants the full tier, deliberately not smuggled
    # in with this fix.
    #
    # Ordering only. Which rows are ELIGIBLE is scope_cond's job, so this cannot
    # surface a row that was not already visible — it only makes the choice among
    # them deterministic instead of plan-dependent.
    own = state.get("legal_entity")
    org = list(state.get("organization_entities") or [])
    sql = f"""
        SELECT v.entry_id
        FROM {config.QA_VARIANTS_TABLE} v
        JOIN {config.QA_ENTRIES_TABLE} e ON e.entry_id = v.entry_id
        WHERE v.is_active AND {frag}{scope_cond} AND ({' OR '.join(conds)})
        ORDER BY CASE
                   WHEN v.legal_entity_code = %s          THEN 0
                   WHEN v.legal_entity_code = ANY(%s)     THEN 1
                   ELSE 2
                 END,
                 -- a stable last resort, so two packs of ONE entity holding the
                 -- same question do not flip between requests either
                 v.entry_id
        LIMIT 1
    """
    try:
        with get_cursor() as cur:
            cur.execute(sql, [*pack_params, *scope_params, *vals, own, org])
            row = cur.fetchone()
            if not row:
                logger.info("qa_exact: no exact match")
                return {"trace_detail": {"exact_match": False}, "resolved": False}
            entry_id = str(row[0])
            _q, answer_md, _tags, _refs = fetch_answer(cur, entry_id)
    except Exception as e:
        logger.warning("qa_exact: failed (%s) — continuing cascade", e)
        return {"resolved": False}

    logger.info("qa_exact: hit entry %s", entry_id)
    return {
        "trace_detail": {"exact_match": True},
        "resolved": True, "outcome": "answer", "answer_source": "qa_exact",
        "matched_entry_id": entry_id, "match_score": 1.0, "match_margin": 1.0,
        "answer": answer_md or "", "sources_json": sources_json(_refs),
        "user_language": state.get("query_language"),
    }
