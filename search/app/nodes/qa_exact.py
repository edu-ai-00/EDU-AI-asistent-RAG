# last change in code by Jan Drozd (2026-09-03).
"""Node — qa_exact: exact QA match (online fast path).

Deterministic. If an active variant's whole normalized/lemmatized form equals
the query's, return that entry's curated answer immediately — no scoring, no
LLM. Order-sensitive string equality on the space-joined token forms is a strong
"the user typed (almost) the canonical question" signal.

A hit ends the cascade, so the questions qa_lexical would have offered beside its
own answer were never built. `related_suggestions` (on by default) builds them by
running that ranking for its SUGGESTIONS only — the exact answer is unaffected.
"""

import logging

from ..state import SearchState
from ..db import get_cursor
from .. import config
from .. import scope
from ..settings import get_active_setting
from .qa_common import (pack_filter, domain_filter, fetch_answer, sources_json,
                        resolve_form, form_text_col, form_token_key,
                        finalize_chips)
from .qa_lexical import qa_lexical

logger = logging.getLogger(__name__)


def _exact_match(state: SearchState, params: dict | None = None) -> tuple[dict, str | None]:
    form = resolve_form(params)
    conds, vals = [], []
    for f in form:
        toks = " ".join(state.get(form_token_key(f), []) or [])
        if toks:
            conds.append(f"v.{form_text_col(f)} = %s")
            vals.append(toks)
    if not conds:
        return {"resolved": False}, None

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
    # On the VARIANT row, for the same reason the visibility predicate is: this
    # is the table being scanned, and migration 144 denormalised domain_ids onto
    # it. An exact match is returned VERBATIM and ends the cascade, so an
    # out-of-area hit here is not a worse ranking — it is the wrong answer with
    # no later step able to soften it.
    dom_cond, dom_params = domain_filter(state, "v")

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
        WHERE v.is_active AND {frag}{scope_cond}{dom_cond} AND ({' OR '.join(conds)})
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
            cur.execute(sql, [*pack_params, *scope_params, *dom_params, *vals,
                              own, org])
            row = cur.fetchone()
            if not row:
                logger.info("qa_exact: no exact match")
                return {"trace_detail": {"exact_match": False}, "resolved": False}, None
            entry_id = str(row[0])
            _q, answer_md, _tags, _refs = fetch_answer(cur, entry_id)
    except Exception as e:
        logger.warning("qa_exact: failed (%s) — continuing cascade", e)
        return {"resolved": False}, None

    logger.info("qa_exact: hit entry %s", entry_id)
    return {
        "trace_detail": {"exact_match": True},
        "resolved": True, "outcome": "answer", "answer_source": "qa_exact",
        "matched_entry_id": entry_id, "match_score": 1.0, "match_margin": 1.0,
        "answer": answer_md or "", "sources_json": sources_json(_refs),
        "user_language": state.get("query_language"),
    }, _q


def _lexical_step(state: SearchState) -> dict | None:
    """The active setting's qa_lexical params, or None when that step is off.

    The suggestions have to be the ones qa_lexical WOULD have offered, so they are
    ranked with its configured form and thresholds — not with qa_exact's. Reading
    the pipeline is a cached lookup; the executor loaded the same row a moment ago.
    """
    setting = get_active_setting(state.get("legal_entity"))
    for step in (setting["config"].get("pipeline") or []):
        if step.get("step") == "qa_lexical":
            return (step.get("params") or {}) if step.get("enabled", True) else None
    return None


def _related_suggestions(state: SearchState, params: dict,
                         entry_id: str, canonical_q: str | None) -> list[dict]:
    """The runners-up qa_lexical would have offered, minus the entry just answered.

    Reuses that node instead of ranking again: its floors (t_suggest, the ročník
    and entity tier, topical admission, the other-ročník net) are three production
    regressions' worth of tuning that a second ranking here would have to repeat.
    Only its FLOORED lists are taken — decision C parks entries below t_suggest for
    a possible no_answer, and next to a verbatim curated answer those are junk.
    """
    lex_params = _lexical_step(state)
    if lex_params is None:
        return []
    # Chip knobs set on THIS step win over the ranking's own config. They are
    # settings for the chips, and the chips are built here — without this they
    # were unreachable on the exact path, which is the path most answers take,
    # so an A/B of one of them measured nothing at all (2026-09-04).
    lex_params = {**lex_params,
                  **{k: v for k, v in params.items() if k.startswith("related_")}}
    top_n = int(params.get("suggest_top_n")
                or lex_params.get("suggest_top_n") or config.QA_SUGGEST_TOP_N)
    try:
        res = qa_lexical(state, lex_params) or {}
    except Exception as e:
        # a suggestion is an offer, never the answer — it must not cost one
        logger.warning("qa_exact: related suggestions failed (%s) — none offered", e)
        return []
    items = list(res.get("suggestions")
                 or (res.get("deferred_suggest") or {}).get("suggestions") or [])
    # Top up a SHORT row, not just an empty one, when the step params say so. A
    # single chip above the bar leaves two slots empty while the category holds a
    # dozen siblings — "V kolik se otevírá škola?" came back with one, and it was
    # "Smějí žáci otevírat okna nebo přetáčet topení?" (the ranking cannot tell
    # opening a school from opening a window; both are the lemma `otevírat`).
    # Here rather than in qa_lexical's branches: that chip arrived through the
    # DEFERRED list, which has no fill of its own, and the chips belong to the
    # answer this step gave either way.
    items += list(res.get("other_grade_suggestions") or [])
    # dedupe on the TEXT, not the entry_id: qa_exact picks one of N entries holding
    # the same question (own school before the organisation), so the rest are the
    # question just answered under a different id.
    seen = {(canonical_q or "").strip().casefold()}
    out: list[dict] = []
    for it in items:
        key = (it.get("question") or "").strip().casefold()
        if not key or key in seen or it.get("entry_id") == entry_id:
            continue
        seen.add(key)
        out.append(it)
    # AFTER the dedupe, and uncapped until here: the shared rules top a short row
    # up and order it, and a self-suggestion left in the list would have eaten a
    # slot before either could happen.
    return _finalize(state, params, entry_id, out, top_n)


def _finalize(state: SearchState, params: dict, entry_id: str,
              items: list[dict], top_n: int) -> list[dict]:
    """The shared chip rules (qa_common.finalize_chips), with a cursor.

    Here and in qa_vector rather than copied into both: the three rules landed one
    path at a time and the paths drifted — qa_exact had all of them while qa_vector
    had none, which is what a user saw as two chips in the wrong order with one
    from the wrong pack (2026-09-04).
    """
    try:
        with get_cursor() as cur:
            return finalize_chips(cur, state, params, entry_id, items, top_n,
                                  resolve_form(params))
    except Exception as e:
        # a suggestion is an offer, never the answer — it must not cost one
        logger.warning("qa_exact: chips failed (%s) — as harvested", e)
        return items[:top_n]


def qa_exact(state: SearchState, params: dict | None = None) -> dict:
    """Wrapper: the exact match, plus the related questions offered beside it.

    Off with `related_suggestions: false` on this step — the ranking costs the
    fast path three extra queries, and dropping it never changes the answer.
    """
    params = params or {}
    out, canonical_q = _exact_match(state, params)
    if not out.get("resolved") or not params.get("related_suggestions", True):
        return out
    sugg = _related_suggestions(state, params, out.get("matched_entry_id"), canonical_q)
    if sugg:
        out["suggestions"] = sugg
    out.setdefault("trace_detail", {})["related"] = len(sugg)
    return out
