"""Try a question (CONTRACT.md §14.9): the school's chat cascade, asked from
the portal. Nothing is stored here. Logs carry school, query_id, outcome and
timing only — never the question or the answer (AGENTS.md)."""
from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .. import errors
from ..adapters.search import (SearchBadRequest, SearchClient, SearchNotFound,
                               SearchUnavailable)
from ..api.models import AskPreviewIn
from .validation import fetch_code_lists

logger = logging.getLogger("qa_portal.ask")

MAX_QUESTION_CHARS = 2000


def _upstream_message(body) -> str:
    """The search 4xx text for the administrator. A validation `detail` list
    keeps only loc+msg: its `input` would echo the question back."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            parts = [f"{'.'.join(str(p) for p in d.get('loc', ()) if p != 'body')}: {d.get('msg', '')}"
                     for d in detail if isinstance(d, dict)]
            if parts:
                return "; ".join(parts)
    return "the search service rejected the question"


def _canonical(codes) -> dict[str, str]:
    return {str(c).casefold(): str(c) for c in codes}


def _validate(body: AskPreviewIn, code_lists: dict) -> tuple[str, str, str | None, list[dict]]:
    """Returns (question, role, grade, 422 fields) with canonical codes."""
    fields: list[dict] = []
    question = body.question.strip()
    if not question:
        fields.append({"field": "question", "message": "the question must not be blank"})
    elif len(question) > MAX_QUESTION_CHARS:
        fields.append({"field": "question",
                       "message": f"the question is longer than {MAX_QUESTION_CHARS} characters"})
    roles = _canonical(c["code"] for c in code_lists.get("role", []))
    role = roles.get(body.user_role.casefold())
    if role is None:
        fields.append({"field": "user_role", "message": f"unknown role code {body.user_role!r}"})
    grade = None
    if body.user_school_grade is not None:
        grades = _canonical(c["code"] for c in code_lists.get("school_grade", []))
        grade = grades.get(body.user_school_grade.casefold())
        if grade is None:
            fields.append({"field": "user_school_grade",
                           "message": f"unknown school_grade code {body.user_school_grade!r}"})
    return question, role or "", grade, fields


def _safe_explain(search: SearchClient, payload: dict, school: str, query_id: str) -> dict | None:
    """§14.11: a failed explain never fails the answer; only the error type is logged."""
    try:
        out = search.explain(payload)
    except Exception as e:
        logger.info("ask-preview: school=%s query_id=%s explain failed (%s)",
                    school, query_id, type(e).__name__)
        return None
    return out if isinstance(out, dict) else None


def ask_preview(search: SearchClient, school: str, body: AskPreviewIn) -> dict:
    question, role, grade, fields = _validate(body, fetch_code_lists(search, school))
    if fields:
        raise errors.validation_error("The request body did not pass validation", fields)

    # Search refuses an unknown user_id (403); the school's technical account is a
    # registered user there, and its qa_portal__ prefix marks portal tests in the log.
    from .document_service import _uploader
    user_id = _uploader(search, school)["user_id"]
    query_id = str(uuid.uuid4())
    payload = {"question": question, "legal_entity": school, "user_role": role,
               "retrieval_only": body.retrieval_only, "user_id": user_id,
               "query_id": query_id}
    if grade is not None:
        payload["user_school_grade"] = grade
    explain_payload = dict(payload)   # same identity, no retrieval_only/debug
    explain_payload.pop("retrieval_only")
    if body.debug:
        payload["debug"] = True

    started = time.monotonic()
    explain = None
    try:
        if body.debug:
            # explain is deterministic and cheap: it runs beside the (slow) ask.
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(_safe_explain, search, explain_payload, school, query_id)
                try:
                    result = search.ask(payload)
                finally:
                    explain = pending.result()
        else:
            result = search.ask(payload)
    except SearchBadRequest as e:
        logger.info("ask-preview: school=%s query_id=%s rejected by search", school, query_id)
        raise errors.bad_request(_upstream_message(e.body))
    except SearchUnavailable as e:
        logger.warning("ask-preview: school=%s query_id=%s search failed (%s)",
                       school, query_id, type(e).__name__)
        raise errors.search_failure(e, "the search service is unavailable")

    logger.info("ask-preview: school=%s query_id=%s outcome=%s source=%s retrieval_only=%s "
                "took_ms=%d", school, query_id, result.get("outcome"), result.get("answer_source"),
                body.retrieval_only, (time.monotonic() - started) * 1000)
    return {
        "answer": result.get("answer"),
        "outcome": result.get("outcome"),
        "answer_source": result.get("answer_source"),
        "delivered_by": result.get("delivered_by"),
        "sources": result.get("sources") or [],
        "suggestions": result.get("suggestions") or [],
        "matched_entry_id": result.get("matched_entry_id"),
        "match_score": result.get("match_score"),
        "duration_ms": result.get("duration_ms"),
        "retrieval_only": body.retrieval_only,
        # §14.11: passed through unchanged, never stored or logged.
        "effective_scope": result.get("effective_scope"),
        "strategies": result.get("strategies") or [],
        "key_words_used": result.get("key_words_used") or [],
        "language": result.get("language"),
        "answer_parts": result.get("answer_parts") or [],
        "trace": result.get("trace") or [],
        "explain": explain,
    }


ENTRY_KEYS = ("pack_id", "entry_key", "canonical_q", "is_active")


def get_entry_by_id(search: SearchClient, school: str, entry_id: str) -> dict:
    """§14.11: where a matched QA entry lives (pack and key), for the debug panel."""
    try:
        entry_uuid = str(uuid.UUID(str(entry_id)))
    except ValueError:
        raise errors.bad_request("entry_id is not a UUID")
    try:
        data = search.get_entry_by_id(school, entry_uuid)
    except SearchNotFound:
        raise errors.not_found("no such entry for this school")
    except SearchBadRequest as e:
        raise errors.bad_request(_upstream_message(e.body))
    except SearchUnavailable as e:
        raise errors.search_failure(e, "the search service is unavailable")
    return {k: data.get(k) for k in ENTRY_KEYS}
