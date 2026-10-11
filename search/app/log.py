# last change in code by Jan Drozd and Martin Safranek (2026-08-13).
"""Append-only, fire-and-forget query/answer logging.

The API gateway (edu_ai_api) has no DB access, so the search component — which
already reads Postgres — owns logging. Writes are pure INSERTs (never UPDATE),
run as a FastAPI BackgroundTask after the response is sent, and any failure is
swallowed so logging can never affect the search result.
"""

import json
import logging
import uuid

from psycopg2.extras import Json

from .db import get_cursor
from . import config
from .settings import _FALLBACK_ID

logger = logging.getLogger(__name__)

# ── two-dimensional presentation of the answer origin (for reporting) ─────────
# answer_source_text = which knowledge base answered (QA | documents | guard |
# none); strategies_text = the concrete retrieval method (exact | lexical |
# keywords | vector). The raw answer_source / strategies columns keep the full
# granular values for debugging.
_SRC_PLANE = {"qa_exact": "QA", "qa_lexical": "QA", "qa_vector": "QA",
              "rag": "documents", "guard": "guard", "none": "none"}


def _source_plane(src):
    return _SRC_PLANE.get(src or "", "none")


# Step-name prefixes that say WHICH INDEX a step searches. strategies_text keeps
# only the method (lexical / keywords / vector), so these are stripped. Add a new
# family here when one appears — a missing prefix does not error, it silently
# logs the whole step name into a column that is supposed to hold the method
# (which is exactly what the rag_lexical -> document_lexical rename did).
_INDEX_PREFIXES = ("qa", "rag", "document")


def _strategy_text(strategies, src):
    """Concrete method(s): strip the index prefix; None for guard/none paths."""
    out = []
    for s in (strategies or []):
        if "_" in s and s.split("_", 1)[0] in _INDEX_PREFIXES:
            out.append(s.split("_", 1)[1])
        elif s not in ("guard", "none", "route"):
            out.append(s)
    seen = []
    for m in out:
        if m not in seen:
            seen.append(m)
    return ",".join(seen) or None


_INSERT = f"""
INSERT INTO {config.QUESTION_ANSWER_LOG_TABLE}
  (conversation_id, session_id, user_id, legal_entity_code,
   user_school_grade, user_role, user_school, channel,
   raw_query, norm_query, lemma_query, stem_query, question_language,
   answer_source, answer_source_text, stage, matched_entry_id, match_score,
   answer_md, sources, strategies, strategies_text, answer_language, latency_ms, setting_id,
   flow_trace, turn_seq, query_id, selected_answer_key, suggestions_offered, domains)
VALUES (%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s, %s, %s, %s,%s,%s, %s, %s)
"""


def _uuid_or_none(value, field: str):
    """A uuid column takes a UUID or NULL — never the caller's junk, and never at
    the price of the row. The API rejects a bad conversation_id with 422; this is
    what catches anything that reaches the insert anyway."""
    if not value:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        logger.warning("%s %r is not a UUID — logged as NULL", field, value)
        return None


def _log_flags() -> tuple[bool, bool]:
    """Platform-wide logging policy — GLOBAL settings row, config -> search
    (same home as max_result_documents / search_requires_user):
      query_log  — write question/answer rows at all (master switch),
      debug_log  — persist the flow_trace diagnostics column; off = NULL.
    Defaults keep today's behaviour; a read failure must never block logging
    policy INTO the permissive direction, so it falls back to (True, True)."""
    try:
        from .settings import get_active_setting
        s = (get_active_setting(None)["config"].get("search") or {})
        return bool(s.get("query_log", True)), bool(s.get("debug_log", True))
    except Exception as e:
        logger.warning("log flags lookup failed (%s) — defaults on", e)
        return True, True


def log_search(req, resp, duration_ms: int, setting_id: str | None = None) -> None:
    """Append one search turn to question_answer_log. Never raises into the caller."""
    try:
        query_log, debug_log = _log_flags()
        if not query_log:
            return
        norm = " ".join(req.query_norm_tokens) if req.query_norm_tokens else None
        lemma = " ".join(req.query_lemma_tokens) if req.query_lemma_tokens else None
        stem = " ".join(req.query_stem_tokens) if getattr(req, "query_stem_tokens", None) else None
        # SearchResponse.sources is a validated list since 2026-08-13; the json.loads
        # that used to undo the API's own json.dumps is gone with it.
        sources = resp.sources
        # matched_entry_id is a uuid column — only pass a value the QA branch set
        matched = getattr(resp, "matched_entry_id", None)
        # Belt to the API validator's braces. conversation_id is a uuid column, and a
        # non-UUID does not fail this column — it fails the whole INSERT, so one bad
        # id cost the entire row. Losing the grouping is bad; losing the question,
        # answer, timing and strategies with it is worse.
        conv = _uuid_or_none(req.conversation_id, "conversation_id")
        # granular strategies (document_lexical, qa_lexical, …); fall back to the coarse
        # answer_source for guard/no_answer paths that gather nothing
        strategies_val = (getattr(resp, "strategies", None)
                          or ([resp.answer_source] if resp.answer_source else None))
        # two-dimensional presentation: knowledge base + retrieval method
        source_text = _source_plane(resp.answer_source)
        strat_text = _strategy_text(strategies_val, resp.answer_source)
        # getattr twice: the eval harness builds its own response-like objects,
        # and a response from before this column existed has no effective_scope.
        _es = getattr(resp, "effective_scope", None)
        _dom = getattr(_es, "domains", None) if _es is not None else None
        domains = list(_dom) if _dom else None
        with get_cursor() as cur:
            cur.execute(_INSERT, (
                conv, req.session_id, req.user_id,
                # WHOSE search this was — without it the log cannot answer "what
                # are this entity's users asking", which is why it exists
                getattr(req, "legal_entity", None),
                # getattr, not req.user_school: the field was dropped from
                # SearchRequest on 2026-08-12 ("nothing read it" — this line did),
                # and the AttributeError killed EVERY insert from then until
                # 2026-08-13. Silently: log_search swallows its errors by design,
                # so the only symptom was a query log that stopped growing.
                req.user_school_grade, req.user_role,
                getattr(req, "user_school", None), "online",
                req.question, norm, lemma, stem,
                getattr(req, "language", None),
                resp.answer_source, source_text, resp.outcome, matched, resp.match_score,
                resp.answer,
                Json(sources) if sources else None,
                strategies_val, strat_text,
                resp.language, duration_ms,
                # the built-in fallback id isn't a real FK row — log NULL for it
                setting_id if setting_id and setting_id != _FALLBACK_ID else None,
                # per-step cascade trace (migration 54) — what `strategies`
                # cannot say: the skipped steps, the reasons, and where it ended.
                # Gated by the global debug_log flag: off → NULL, the row itself
                # still records the question/answer.
                Json(getattr(resp, "trace", None) or None)
                if debug_log and getattr(resp, "trace", None) else None,
                # client-side ids (migration 135). getattr, not req.x: the eval
                # harness builds its own request objects and does not carry them.
                getattr(req, "turn_seq", None), getattr(req, "query_id", None),
                # what the client PICKED, against matched_entry_id above = what it
                # actually got. The gap is the measurement, see migration 135.
                getattr(req, "selected_answer_key", None),
                # WHAT WAS OFFERED (migration 205) — the other half of "do not
                # repeat a suggestion". raw_query above already carries what was
                # ASKED, a clicked chip included, since the client resends its
                # text; this is the chips nobody took. Read back by
                # conversation.load_seen on the next turn.
                Json(resp.suggestions) if getattr(resp, "suggestions", None) else None,
                # THE AREA FILTER (migration 213), read off the RESPONSE's
                # effective_scope rather than off the request. That is the one
                # normalized representation both endpoints already build from the
                # resolver's output, so the logged value cannot disagree with the
                # value the search ran with or with the value the client was told
                # — and a raw request field could differ from all three (order,
                # case, duplicates). None → NULL = no thematic restriction.
                domains,
            ))
    except Exception as e:  # fire-and-forget: logging must never break search
        logger.warning("question_answer_log insert failed (ignored): %s", e)
