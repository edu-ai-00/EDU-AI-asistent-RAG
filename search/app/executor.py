# last change in code by Jan Drozd and Martin Safranek (2026-08-09).
"""Config-driven search executor — replaces the hard-coded LangGraph cascade.

Each search step is an independently-callable function `fn(state, params) -> dict`
registered in STEP_REGISTRY. The runner loads the active settings, runs
`prepare` (token build), then walks `config.pipeline` in order:

    for step in pipeline:
        if not step.enabled: skip
        state |= fn(state, step.params)
        if state.resolved and step.on_hit != "continue": stop

This gives the three properties the settings layer needs: steps are callable on
their own, the order is data (reorderable), and any step can be disabled or made
a fallback purely by editing the JSON — no code change. `resolved` still marks a
terminal decision, so gather-only steps (rag_*) never stop the walk. A final
safety net turns an unresolved walk into a polite no_answer.
"""

from __future__ import annotations

import logging
import time

from .settings import get_active_setting
from .messages import system_message, resolve_unanswered
from . import config
from .nodes.prepare import prepare
from .nodes.resolve_context import resolve_context
from .nodes.route import route
from .nodes.qa_exact import qa_exact
from .nodes.qa_lexical import qa_lexical
from .nodes.qa_vector import qa_vector
from .nodes.document_lexical import document_lexical
from .nodes.document_siblings import document_siblings
from .nodes.rag_keywords import rag_keywords
from .nodes.rag_vector import rag_vector
from .nodes.answer import answer

logger = logging.getLogger(__name__)

# Steps that GATHER document chunks. An explicit set, not a name prefix: the
# behaviour below (does this step report what it added to the pool? does it count
# as a contributing strategy?) hung on names starting with "rag_", so renaming
# one of them would silently drop it from `strategies` and from the trace.
# Behaviour belongs in the registry, not in a naming convention.
GATHERERS = {"document_lexical", "rag_keywords", "rag_vector", "document_siblings"}

STEP_REGISTRY = {
    "route": route,
    "qa_exact": qa_exact,
    "qa_lexical": qa_lexical,
    "document_lexical": document_lexical,
    "document_siblings": document_siblings,
    "rag_keywords": rag_keywords,
    "qa_vector": qa_vector,
    "rag_vector": rag_vector,
    "answer": answer,
}


def _pool_quality(chunks, heading_quality: float = 1.0) -> tuple[float, str]:
    """How good is the best evidence gathered so far, and what makes it good.

    Coverage alone became the wrong measure the moment the heading gate existed. A
    chunk admitted because a DISCRIMINATIVE concept sits in its heading carries
    coverage 0.25 — five chunks whose heading is literally "Seznam pomůcek …"
    scored 0.25 on "Jaké pomůcky potřebuje prvňáček?" — and the executor read that
    as a weak pool and kept falling through to the alternative retrievers. The
    heading match IS the quality: it states that the chunk is ABOUT the topic,
    which is the strongest claim any retriever here makes. So it counts as full
    evidence and the cascade stops gathering.

    Returns (quality, reason) so the trace can say which of the two decided.
    """
    best, why = 0.0, "empty"
    for c in chunks or []:
        if c.get("heading_match"):
            return heading_quality, f"heading:{c['heading_match']}"
        cov = c.get("coverage") or 0.0
        if cov > best:
            best, why = cov, "coverage"
    return best, why


def run_search(initial: dict, stop_before: str | None = None) -> tuple[dict, str]:
    """Run the active-setting cascade over `initial` state. Returns (state, setting_id).

    `stop_before="answer"` stops the walk right BEFORE that step when the state
    is still unresolved, returning with `_stopped_before` + `_stopped_params`
    set — the /search/stream endpoint then performs the generation itself
    (token streaming) from exactly the same state and step params. A cascade
    resolved earlier (QA hit, guard, suggest) never reaches the marker and
    returns finished as usual; the no_answer safety net is NOT applied to a
    stopped walk (the caller decides).
    """
    # the ORGANISATION's setting: a settings row belongs to a top-level entity
    # and applies to its whole tree; global row when the org has none (init 98)
    setting = get_active_setting(initial.get("legal_entity"))
    cfg = setting["config"]
    setting_id = setting["setting_id"]

    state: dict = dict(initial)
    # resolve_context = follow-up rewriting; must precede prepare, which derives
    # the token forms every later step matches on. Returns {} when it does not
    # apply, so a plain question costs one dict update and no LLM call.
    _ctx = resolve_context(state, cfg) or {}
    state.update(_ctx)
    # prepare = token build (norm/lemma/stem forms per the setting's normalization
    # config); always runs before the pipeline.
    state.update(prepare(state, cfg) or {})

    # granular list of the retrieval steps that actually contributed, in order
    # (rag_* that grew the chunk pool, qa_* that reached a terminal decision).
    strategies: list[str] = []

    # Per-step trace for the test page / debugging: EVERY configured step, in
    # order, with what it did — including the ones that never ran and why. The
    # `strategies` list above deliberately records only contributors, so on its
    # own it cannot answer "where did the cascade stop, and what did the steps
    # before it actually return". Facts only; no scoring, no interpretation.
    trace: list[dict] = []

    def _mark(step_name, status, **extra):
        trace.append({"step": step_name, "status": status, **extra})

    # First row of the trace: what the cascade actually searched for. Without it
    # a rewritten query looks like the user asked something they never typed.
    if _ctx:
        _mark("resolve_context", "ran", turns=_ctx.get("context_turns"),
              raw_question=_ctx.get("raw_question"),
              resolved_question=_ctx.get("user_question"))
    else:
        _mark("resolve_context", "skipped",
              note="self-contained question, no prior turns, or disabled")

    for step in cfg.get("pipeline", []):
        name = step.get("step")
        if not step.get("enabled", True):
            _mark(name, "disabled", note="enabled=false in the active setting")
            continue
        fn = STEP_REGISTRY.get(name)
        if fn is None:
            logger.warning("executor: unknown step '%s' — skipping", name)
            _mark(name, "unknown", note="no such step in STEP_REGISTRY")
            continue
        if stop_before and name == stop_before and not state.get("resolved"):
            state["strategies"] = strategies
            state["trace"] = trace     # facts gathered so far (retrieval_only diag)
            state["_stopped_before"] = name
            # merge the dev/eval override here too — the stopped step never runs
            # through the merge below, so /search/stream + retrieval_only would
            # otherwise generate/rank with the un-overridden params.
            state["_stopped_params"] = {**(step.get("params") or {}),
                                        **(state.get("_params_override") or {})}
            return state, setting_id
        # sequential RAG fallback: a step marked only_if_empty runs solely when no
        # RAG chunk has been gathered yet, so rag_keywords/rag_vector fire only if
        # the earlier retriever(s) came up empty.
        #
        # only_if_max_coverage_below (optional, config): ALSO run the step when
        # the pool is non-empty but every gathered chunk is WEAK evidence — max
        # coverage below the threshold. Without it, lexical junk blocks the
        # vector step exactly on the questions that need it: "Jak přesně se
        # jmenuje naše škola?" gathers chunks covering only [skola] (0.5), the
        # one chunk holding the school's name shares no token with the query,
        # and the semantic step that WOULD find it never fired (2026-07-31).
        if step.get("only_if_empty") and state.get("rag_chunks"):
            # run_on_timespan: a deterministically-detected time span in the query
            # ("na 14 dní", "na týden") FORCES this semantic step. Duration questions
            # phrase the answer differently ("uvolnění" ↔ heading "Docházka do
            # školy") AND the number pollutes the lexical rank, so vector must run
            # even on a pool that looks strong by a heading match.
            _forced = bool(step.get("run_on_timespan") and state.get("query_has_timespan"))
            _th = step.get("only_if_max_coverage_below")
            # how much a heading match is worth as evidence — it is what decides
            # whether the cascade stops gathering, so it belongs to the step's config
            _hq = float((step.get("params") or {}).get("heading_quality") or 1.0)
            _maxcov, _why = _pool_quality(state.get("rag_chunks"), _hq)
            # SUFFICIENCY vs RELEVANCE. A heading match KEEPS a chunk — it states the
            # chunk is ABOUT the topic — but it does NOT prove the pool can ANSWER:
            # a prose section (klasifikační řád, "co je JPZ") names the topic in its
            # heading while the substantive text lives in body chunks that only the
            # semantic step surfaces. So the coverage gate is measured on genuine
            # BODY coverage; a heading-only pool no longer suppresses rag_vector.
            # (Before: pool quality read 1.000 "by heading" and the vector step that
            # holds the answer never ran → LLM returned NO_ANSWER.) When the step has
            # NO coverage threshold (pure only_if_empty), behaviour is unchanged.
            # trgm chunks are excluded: their "coverage" is a TYPO similarity, a
            # guess about near-matching words, not evidence the pool can answer —
            # counting it suppressed rag_vector exactly when the query said
            # "fotit" and the one right chunk (saying "fotografování") was only
            # reachable semantically.
            _body = max((c.get("coverage") or 0.0
                         for c in (state.get("rag_chunks") or [])
                         if c.get("retrieval_type") != "document_trgm"),
                        default=0.0)
            if not _forced and (_th is None or _body >= float(_th)):
                logger.info("executor: skip '%s' (only_if_empty, pool=%d, "
                            "body_cov=%.3f, quality=%.3f by %s)",
                            name, len(state.get("rag_chunks") or []), _body, _maxcov, _why)
                _mark(name, "skipped", pool=len(state.get("rag_chunks") or []),
                      max_coverage=round(_maxcov, 3), body_coverage=round(_body, 3),
                      quality_by=_why,
                      note=("only_if_empty and the pool is not empty" if _th is None
                            else f"only_if_empty; body coverage {_body:.3f} >= {_th} "
                                 f"(heading match alone no longer suppresses the "
                                 f"semantic step)"))
                continue
            logger.info("executor: '%s' runs (%s)", name,
                        "query has a time span" if _forced
                        else f"WEAK body coverage={_body:.3f} (quality {_maxcov:.3f} "
                             f"by {_why}) < {_th} — semantic step runs")
        params = step.get("params") or {}
        # dev/eval: a request-level params_override is merged into EVERY step's
        # params (sweeps A/B fusion/BM25 weights without touching search_setting).
        if state.get("_params_override"):
            params = {**params, **state["_params_override"]}
        pool_before = len(state.get("rag_chunks") or [])
        # per-phase chunk provenance for the trace: the pool objects survive the
        # step (gatherers copy the LIST, not the dicts), so a before-snapshot of
        # the per-chunk ranks tells which existing chunks the step ANNOTATED —
        # added ones are simply everything past pool_before.
        _pre_marks = ({id(c): (c.get("keyword_rank"), c.get("vector_rank"))
                       for c in (state.get("rag_chunks") or [])}
                      if name in GATHERERS else None)
        was_resolved = bool(state.get("resolved"))
        # snapshot: the trace must attribute a decision/score to the step that
        # SET it. Both live in the shared state, so reading them afterwards would
        # otherwise repeat qa_lexical's verdict on every later row.
        _dec_before = state.get("decision")
        _score_before = state.get("match_score")
        _t0 = time.perf_counter()
        try:
            state.update(fn(state, params) or {})
        except Exception as e:  # a step must never crash the whole cascade
            logger.warning("executor: step '%s' raised (%s) — continuing", name, e)
            _mark(name, "error", ms=int((time.perf_counter() - _t0) * 1000),
                  note=f"{type(e).__name__}: {e}")
            continue
        _ms = int((time.perf_counter() - _t0) * 1000)
        _pool_after = len(state.get("rag_chunks") or [])
        _resolved_here = bool(state.get("resolved")) and not was_resolved
        # The chunk pool is a concept of the DOCUMENT steps only. Reporting it on
        # route/qa_* would suggest it means something there — a QA answer has
        # nothing to do with chunks, and "0 chunks in pool" next to a QA hit
        # reads as if the pool were the reason. Gatherers report what they added;
        # `answer` reports what it built the context from; QA steps report neither.
        _is_gatherer = name in GATHERERS
        # A step may hand the trace STRUCTURED facts about what it decided
        # (thresholds it compared against, which guard fired, whether an exact
        # match existed). Structured, never prose: the test page renders them
        # with translated labels, so a node must not emit user-facing text.
        _detail = state.pop("trace_detail", None)
        # which chunks THIS phase selected (testing/eval): DB ids of the chunks it
        # added, plus the ids it re-found and annotated on the existing pool
        _added_ids = _annotated_ids = None
        if _is_gatherer:
            _pool_list = state.get("rag_chunks") or []
            _added_ids = [c.get("chunk_id") for c in _pool_list[pool_before:]]
            _annotated_ids = [
                c.get("chunk_id") for c in _pool_list[:pool_before]
                if (c.get("keyword_rank"), c.get("vector_rank"))
                != (_pre_marks or {}).get(id(c))]
            if _added_ids or _annotated_ids:
                logger.info("executor: '%s' selected chunk ids %s%s", name,
                            _added_ids,
                            f" (annotated {_annotated_ids})" if _annotated_ids else "")
        _mark(name, "ran", ms=_ms, detail=_detail or None,
              pool=_pool_after if (_is_gatherer or name == "answer") else None,
              pool_added=(_pool_after - pool_before) if _is_gatherer else None,
              added_chunk_ids=_added_ids or None,
              annotated_chunk_ids=_annotated_ids or None,
              matched_entry_id=(state.get("matched_entry_id")
                                if _resolved_here and name.startswith("qa_") else None),
              resolved=_resolved_here,
              outcome=state.get("outcome") if _resolved_here else None,
              decision=(state.get("decision")
                        if state.get("decision") != _dec_before else None),
              match_score=(state.get("match_score")
                           if state.get("match_score") != _score_before else None))
        # record the granular strategy this step contributed:
        #  - a rag_* gatherer that actually grew the chunk pool, or
        #  - a qa_* step that flipped the cascade to a terminal decision.
        if name in GATHERERS and len(state.get("rag_chunks") or []) > pool_before:
            strategies.append(name)
        elif name.startswith("qa_") and state.get("resolved") and not was_resolved:
            strategies.append(name)
        if state.get("resolved") and step.get("on_hit", "stop") != "continue":
            logger.info("executor: resolved at '%s' (outcome=%s)", name, state.get("outcome"))
            trace[-1]["stopped_here"] = True
            break

    state["strategies"] = strategies
    state["trace"] = trace

    if not state.get("resolved"):
        logger.info("executor: pipeline exhausted unresolved → deferred suggest / no_answer")
        state.update(resolve_unanswered(state))
    return state, setting_id
