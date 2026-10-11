# last change in code by Jan Drozd and Martin Safranek (2026-08-21).
"""FastAPI server wrapping the LangGraph search cascade."""

import json
import logging
import time
import uuid
from datetime import datetime
from fastapi import (FastAPI, HTTPException, BackgroundTasks, Header, Response,
                     APIRouter)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import config
from . import domain_scope
from . import idempotency
from .executor import run_search
from .llm import get_chat
from .log import log_search
from .nodes.answer import (prepare_generation, parse_no_answer, variant_chips,
                           merge_suggestions, without_clarify,
                           extract_trailing_variants)
# `system_message` was used on the /search/stream no-answer path without ever
# being imported — that path raised NameError and the client got an empty SSE
# body (found 2026-07-29 while wiring the language registry).
from .messages import system_message, resolve_unanswered
from .languages import ui_default_language
from . import conversation
from .settings import (organization_entities, platform_int, platform_setting,
                       subtree_entities,
                       ancestor_entities, verify_user, get_active_setting)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

api = FastAPI(
    title="edu_ai Search Service",
    description="Deterministic QA→RAG search cascade (LangGraph)",
    version="2.0.0",
)

# Versioned surface, 2026-08-13. Everything except /health lives under /v1, the
# same shape edu_ai_api already uses (/api/health + /api/v1/ingest) — the two
# services disagreed only because they were written months apart.
#
# The prefix is on the ROUTER, not typed into fourteen decorators: a /v2 is then
# a second router, and no route can be forgotten. include_router sits at the
# bottom of this file — the decorators must have run first.
#
# Public paths are /search/ (nginx) + these, so the question is
# POST /search/v1/ask. It used to be POST /search/search.
v1 = APIRouter(prefix="/v1")

# CORS: CLOSED by default since 2026-08-13. Nothing in production needs it — the
# web chat calls its BFF same-origin through nginx, the mobile app is a native
# client CORS does not apply to, and Laravel would be server-to-server. It was "*"
# for the tools/*.html dev pages, which reach :8090 directly.
#
# Not exploitable as it stood (no allow_credentials, so a browser attaches neither
# cookies nor Basic auth, and the API key has to be written in by script) — but "*"
# on an authenticated endpoint becomes a hole the moment credentials are switched
# on or auth moves into a cookie, and nobody would connect the two changes.
#
# Set CORS_ORIGINS=https://a,https://b to open it for named origins.
import os as _os
CORS_ORIGINS = [o.strip() for o in _os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
from fastapi.middleware.cors import CORSMiddleware
api.add_middleware(
    CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"],
    # a browser cannot read a response header that is not exposed, so without this
    # the JS client has no way to tell a replay from a fresh answer
    expose_headers=["Idempotency-Replayed"],
)


# ── Chybová obálka ────────────────────────────────────────────
# One shape for every error, so a client can branch on error.code instead of
# guessing from the status. Before this, `detail` was sometimes a string,
# sometimes a dict and sometimes pydantic's list of field errors — three shapes
# on one endpoint. Same shape web_chat already returns.
#
#   {"error": {"code": "...", "message": "...", "retryable": bool}}
#
# retryable says whether repeating the identical request can help: a rate limit
# or an outage yes, a malformed body never. The client had to infer that from
# the status code, and that inference sent people hunting for outages that were
# not happening (see the 4xx/5xx note in web_chat.py).

_ERROR_CODES = {
    400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED", 409: "CONFLICT", 413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE", 422: "VALIDATION_ERROR", 429: "RATE_LIMITED",
    500: "INTERNAL_ERROR", 502: "UPSTREAM_ERROR", 503: "UNAVAILABLE",
    504: "TIMEOUT",
}
_RETRYABLE = {429, 500, 502, 503, 504}


def _error_body(code: str, message: str, retryable: bool, **extra) -> dict:
    return {"error": {"code": code, "message": message, "retryable": retryable,
                      **extra}}


# Registered on STARLETTE's HTTPException, not FastAPI's. FastAPI's subclasses it,
# but the router raises the base one for 404/405 — and a handler bound to the
# subclass never sees those. Both were escaping the envelope until a test asked.
@api.exception_handler(StarletteHTTPException)
async def _http_exception_handler(_request, exc) -> JSONResponse:
    detail = exc.detail
    extra: dict = {}
    # a handler that already built the envelope (idempotency) keeps its own code
    if isinstance(detail, dict) and "code" in detail:
        code = str(detail["code"])
        message = str(detail.get("message") or "")
        retryable = bool(detail.get("retryable", exc.status_code in _RETRYABLE))
        # a 422 raised by hand carries the same `fields` a pydantic one does —
        # the contract promises them on every VALIDATION_ERROR, whoever raised it
        if detail.get("fields"):
            extra["fields"] = detail["fields"]
    else:
        code = _ERROR_CODES.get(exc.status_code, "ERROR")
        message = detail if isinstance(detail, str) else json.dumps(
            detail, ensure_ascii=False)
        retryable = exc.status_code in _RETRYABLE
    return JSONResponse(status_code=exc.status_code,
                        content=_error_body(code, message, retryable, **extra),
                        headers=getattr(exc, "headers", None))


@api.exception_handler(RequestValidationError)
async def _validation_exception_handler(_request, exc) -> JSONResponse:
    # `fields` keeps what pydantic knew — which field and why — that a single
    # message would throw away. The envelope stays the same shape either way.
    fields = [{"field": ".".join(str(p) for p in e.get("loc", ())[1:]) or "body",
               "message": e.get("msg", "")}
              for e in exc.errors()]
    first = fields[0] if fields else {"field": "body", "message": "invalid"}
    return JSONResponse(
        status_code=422,
        content=_error_body("VALIDATION_ERROR",
                            f"{first['field']}: {first['message']}",
                            False, fields=fields))


# ── Request / Response models ─────────────────────────────────

class SearchRequest(BaseModel):
    # CLOSED: an unknown field is a 422, never a shrug. The contract schema has
    # said additionalProperties:false since 2026-08-13 and only the model had not
    # caught up — so a client that misspelled `user_role` got a cheerful
    # role_required telling it to send the role it had just sent, and went
    # looking in the wrong place. Verified 2026-08-21: no live client sends an
    # unknown field (web_chat sends seven, all named below), so this costs
    # nothing today and turns every future typo into one legible error.
    model_config = ConfigDict(extra="forbid")

    # 2000 CHARACTERS (not bytes — with diacritics that is up to 4 kB UTF-8), the cap
    # web_chat already applies to its own input and passes through verbatim. A
    # 422, never a truncation: answering a shortened question is answering a
    # different one, and nothing in the response would say so. min_length is
    # deliberately absent — it cannot see "   ", which .strip() below still can.
    question: str = Field(max_length=2_000)
    # WHOSE search this is. Required — a missing entity is a 400, never "all of
    # them": a permissive default here is a silent cross-tenant leak. A UNIT may be
    # given (SKOLA_NOVA_VES_ZS), in which case the caller also sees what the legal
    # person issued as `organization`; see settings.organization_entities.
    legal_entity: str
    session_id: str | None = None
    # ── caller / personalization context ──────────────────────
    # WHO is asking. Whether it is MANDATORY is the organisation's policy
    # (settings visibility.search_requires_user, default true — see init 99).
    # A SUPPLIED user_id is always verified against the entity: without that the
    # entity in the request is an unbacked claim, and anyone could read another
    # school's documents by naming it — measured, 10 chunks of a school the
    # caller had nothing to do with. An ALLOWED anonymous caller (policy false)
    # searches without the private branch of the visibility predicate.
    user_id: str | None = None
    # Ročník on ONE unified axis (init 120): ZŠ '1'..'9', SŠ '10'..'13'. The
    # caller's UNIT decides which range is real, so a value the unit does not own
    # is ignored downstream — out of range is never an error. OPTIONAL twice over:
    # when absent, prepare() derives it from the question text ("pro 6. třídu",
    # "šesťák", "sekunda"); failing that the grade does not participate at all.
    # See _grade_code — it accepts 1..13, NOT the 1..9 an earlier comment claimed.
    user_school_grade: int | str | None = None
    # Audience role — a HARD filter since init 127, not the ukol1.md passthrough it
    # started as: validated against code_list('role') (STUDENT|TEACHER|PARENT, not
    # zak/ucitel/rodic), and when the policy requires one, a missing or unknown
    # value short-circuits with outcome='role_required' before any retrieval.
    user_role: str | None = None
    # user_school (ukol1.md) was removed 2026-08-12: nothing read it, and its three
    # single-school values were superseded by legal_entity. Clients still sending
    # it are unaffected — pydantic ignores unknown fields.
    #
    # language filter: číselník code (code_list 'language', active rows — cs|en).
    # Restricts BOTH document retrieval (effective doc language = user code, else
    # LLM code) and the QA packs (manifest lang). None = no restriction.
    language: str | None = None
    # THEMATIC AREAS the search is restricted to — DOMAIN codes from
    # GET /v1/domains for THIS entity (spec.search-domain-scope §3). Absent or
    # null = the search that ran before this field existed; a non-empty set is
    # OR between the areas and AND with every existing filter. `[]`, an empty
    # element, an unknown/inactive code or a wrong type is a 422 naming the
    # offending element — NEVER a silently narrowed or widened set.
    #
    # A preference, not a permission: it can only remove sources, and the entity,
    # role, visibility and validity predicates are unchanged behind it.
    #
    # Typed list[str] rather than list[DomainCode]: which codes exist is DATA
    # (taxonomy_rules, per school), so the server validates membership against
    # the school's own catalog and a frozen enum here could only drift from it.
    domains: list[str] | None = None
    # Groups turns of one conversation. MUST be a UUID — validated below, because
    # question_answer_log.conversation_id is a uuid column and anything else used to
    # kill the whole log row on insert. log_search swallows its errors by design, so
    # the only symptom was a table that stopped growing.
    conversation_id: str | None = None
    # ── client-side identifiers of ONE question (QA webchat contract) ────────
    # Recorded, never trusted: the client owns them. turn_seq says where in the
    # conversation this sits, query_id lets a report be traced from the app's log
    # to a question_answer_log row without matching on question text.
    turn_seq: int | None = None
    query_id: str | None = None
    # ACCEPTED AND STORED BUT NOT HONOURED. The cascade still re-matches on the
    # question text, so sending this changes nothing today — it is stored next to
    # matched_entry_id so the disagreement can be measured before the cascade is
    # taught to jump straight to the picked entry. Logged on arrival (see /search)
    # rather than silently absorbed: a field that looks like it works and does not
    # is the failure mode this contract keeps producing.
    selected_answer_key: str | None = None
    # query tokens in BOTH index forms (mirror qa_variants norm_text + lemma_text).
    # The client may compute them on-device (shipped simplemma dict) and send them
    # for parity with the index, or leave null and let the server derive them.
    query_norm_tokens: list[str] | None = None    # surface: lower, no diacritics, tokenized
    query_lemma_tokens: list[str] | None = None   # PURE simplemma lemmas
    query_stem_tokens: list[str] | None = None    # PURE Dolamic-Savoy light stems
    # ── dev / eval only (ignored by normal clients) ──────────────────────────
    # params_override: merged into EVERY cascade step's params, so a sweep can A/B
    # fusion / BM25 weights without editing search_setting or restarting.
    # retrieval_only: stop before the answer LLM and return the ranked sources —
    # deterministic, token-free retrieval scoring for the eval harness.
    params_override: dict | None = None
    retrieval_only: bool = False
    # `trace` measured 15.3 kB of a 21.2 kB answer — 72 % of what a client
    # downloads for diagnostics its own comment says it ignores. Off by default,
    # on per request. The query log still gets the full trace either way: it is
    # stripped on the way out, after log_search has the object.
    debug: bool = False

    @field_validator("conversation_id")
    @classmethod
    def _conversation_id_is_uuid(cls, value: str | None) -> str | None:
        """422 rather than a silently missing log row.

        Refusing a whole search over a field the cascade never reads looks harsh —
        but the alternative measured worse: the answer came back fine and the query
        log quietly lost the row, which is only noticed when someone wonders why the
        table stopped growing. The same choice the language filter makes (unknown
        code → 400, not ignored), and deliberately NOT the one user_school_grade
        makes: a grade only PREFERS content, an id that cannot be stored destroys
        the correlation it exists for.
        """
        if value is None or not value.strip():
            return None
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError, TypeError):
            raise ValueError(
                "conversation_id must be a UUID (e.g. "
                "11111111-1111-1111-1111-111111111111); it is stored in a uuid "
                "column. Omit the field if you have no conversation to group by.")
        return value


class EffectiveScope(BaseModel):
    """What the search ACTUALLY ran with — the "škola, role, ročník" diagnostic.

    Deliberately NOT the spec's memberships[] with an `applied` flag: that shape
    presumes a token the server can enumerate memberships from, and this service has
    none — entity, role and ročník arrive per request. One synthetic membership with
    applied=true forever would be structure without information.
    """
    legal_entity: str | None = None
    role: str | None = None              # code_list('role') code, as validated
    school_grade: str | None = None      # the ročník the cascade USED (may be text-derived)
    language: str | None = None          # language filter, null = no restriction
    # The thematic areas the search WAS restricted to, canonical (trimmed,
    # upper-cased, deduplicated, sorted) — or null for no restriction. Codes,
    # never the internal taxonomy_rules ids: those are per-entity and no client
    # has any use for them.
    #
    # ONLY EVER A VALIDATED VALUE. It is filled from what domain_scope.resolve
    # returned, not from the request, so a response can never present an
    # unchecked filter as the one that ran — including on the early returns
    # (role_required), where validation has deliberately already happened.
    domains: list[str] | None = None
    # WHETHER the caller was identified: the private branch of the visibility
    # predicate depends on it, so it answers "why do I not see my own document".
    identified: bool = False


class SearchResponse(BaseModel):
    answer: str
    # role_required (init 127) is answered by _role_gate BEFORE the cascade runs,
    # so it never appears in SearchState.outcome — but clients do see it and must
    # render it like any other answer.
    outcome: str                # answer|suggest|blocked|self_harm|unusable|no_answer|role_required
    answer_source: str          # guard|qa_exact|qa_lexical|qa_vector|rag|none
    strategies: list[str] = []  # granular retrieval steps that contributed (document_lexical, qa_lexical, …)
    # ALWAYS present; empty only on guard outcomes or a genuinely empty corpus —
    # an exact QA hit carries related questions too (qa_exact.related_suggestions). Contract: every item's `question` (and each string in `options`) can
    # be sent straight back to /search as the next question. kinds: qa | clarify |
    # related — see contracts/schemas/search-response.schema.json.
    suggestions: list[dict] = []
    matched_entry_id: str | None = None
    match_score: float | None = None
    # A real array since 2026-08-13, not the JSON STRING it used to be. The cascade
    # still carries it stringified in state (`sources_json`), so the parse happens
    # once at this boundary instead of in every client — see _sources_list.
    sources: list[dict] = []
    # The same answer, one entry per paragraph, each carrying the document it came
    # from: {index, lead_in, text, markdown, source_rank, title, url, file_uuid}.
    # A multi-document answer is written as one paragraph per document, so a client
    # can render each next to a link to its source instead of parsing the markdown
    # and guessing. `answer` is unchanged and remains the whole text; source_rank is
    # null when a paragraph could not be attributed with certainty.
    answer_parts: list[dict] = []
    language: str = "cs"
    # Which query concepts the keyword step actually matched (union of keyword_match
    # over the chunks it returned). Empty on every other path — a QA hit or a purely
    # lexical/vector answer used no keywords, and saying [] is the honest answer.
    key_words_used: list[str] = []
    # Always "online" from here; the constant exists so a client that also answers
    # from an on-device library can tag both kinds of turn the same way.
    delivered_by: str = "online"
    effective_scope: EffectiveScope | None = None
    duration_ms: int = 0
    # Per-step cascade trace: EVERY configured step in order, with what it did —
    # ran / skipped / disabled / error, its timing, what it added to the chunk
    # pool, and which one ended the walk. `strategies` lists only contributors,
    # so it cannot show where the cascade stopped or what the steps before it
    # returned. Diagnostic; the mobile client ignores it.
    trace: list[dict] = []


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "edu_ai_search"


def _effective_scope(req: "SearchRequest", result: dict | None = None,
                     role: str | None = None, identified: bool = False,
                     domains: list[str] | None = None) -> "EffectiveScope":
    """The scope as the cascade USED it, not as the request stated it. The ročník in
    particular may be text-derived ("pro 6. třídu") when the caller sent none, so
    echoing the request back would report a value that never applied.

    `domains` comes from the resolver, never from `req`: the same rule, applied
    to the field where getting it wrong is worst — a response claiming a filter
    that was never validated would be a claim about which sources could have
    answered.
    """
    result = result or {}
    # NOT result.get(key, _grade_code(...)): the default would be evaluated on every
    # call, and _grade_code hits the code_list — an extra query per response, and a
    # database blip taking the whole answer down with it. Fall back only when the
    # cascade really has no grade, and never let that fallback raise.
    grade = result.get("user_school_grade")
    if grade is None and req.user_school_grade is not None:
        try:
            grade = _grade_code(req.user_school_grade)
        except Exception as e:
            logger.warning("effective_scope: grade lookup failed (%s)", e)
    return EffectiveScope(
        legal_entity=req.legal_entity,
        role=role,
        school_grade=str(grade) if grade is not None else None,
        language=result.get("language_filter") or None,
        # the cascade's own copy wins, so a step could in principle report what
        # it really applied; today they only carry it through unchanged
        domains=result.get("domain_codes") or domains,
        identified=identified,
    )


def _sources_list(raw) -> list[dict]:
    """State's `sources_json` → the wire's `sources` array. Every consumer used to
    json.loads this itself (log.py, web_chat, the mobile client), each with its own
    silent fallback; doing it once here is the whole point. Malformed → []."""
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        logger.warning("sources_json is not valid JSON — returning []")
        return []
    return parsed if isinstance(parsed, list) else []


def _sources_cell(asset_refs) -> str:
    """qa_entries.asset_refs -> the export CSV's `sources` cell.

    Inverse of the reader's parse: one source per line, `Titulek | URL`, and the
    title omitted when it is only a copy of the URL. Round-trip stability is the
    whole requirement here â€” a curator who downloads, edits one answer and
    re-uploads must not silently wipe every citation in the pack.
    """
    if isinstance(asset_refs, str):
        try:
            asset_refs = json.loads(asset_refs)
        except (TypeError, ValueError):
            return ""
    if not isinstance(asset_refs, list):
        return ""
    lines = []
    for item in asset_refs:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip() if isinstance(item.get("url"), str) else ""
        title = (item.get("title") or "").strip() if isinstance(item.get("title"), str) else ""
        if url and title and title != url:
            lines.append(f"{title} | {url}")
        elif url or title:
            lines.append(url or title)
    return "\n".join(lines)


def _public_suggestions(items, req=None, user_id=None,
                        domains: list[str] | None = None) -> list[dict]:
    """The suggestion list as a CLIENT may treat it — every `question` sendable.

    `req`/`user_id` turn on the per-conversation dedupe (see conversation.py): a
    question this chat already asked is never offered back, and a chip already
    shown and ignored goes to the end rather than into the bin. Passing neither
    is the old behaviour, which is what the eval harness and any caller without a
    conversation_id get anyway.

    Two things the wire promises and the cascade did not keep:

    `kind=clarify` is OFF by default (2026-08-21). Its `question` is the asking
    sentence ("your question may span several areas"), not a query — the sendable
    texts are in `options[]`. A client that follows the documented rule "send
    `question` straight back" therefore shows the prompt as a chip and searches
    for it. Until a client understands `kind`, the honest surface is to not offer
    it: platform_setting('suggest_clarify') flips it back on with an UPDATE, no
    rebuild, once the app handles options[].

    `entry_id` is always PRESENT, "" where there is no QA entry behind the
    suggestion. It used to be absent on the document-derived kinds, which is a
    different thing to a typed client than an empty string.
    """
    if not platform_setting("suggest_clarify", False):
        items = without_clarify(items)
    # the lookup costs a query, so it is not spent on a response with nothing to
    # filter — which is every guard outcome and most of the eval traffic
    if items and req is not None and getattr(req, "conversation_id", None):
        # `domains` is the VALIDATED set, so the dedupe reads back only the turns
        # of this conversation asked in the same area — see conversation.load_seen.
        asked, offered = conversation.load_seen(
            req.conversation_id, getattr(req, "legal_entity", None), user_id,
            domains)
        items = conversation.drop_repeats(items, asked, offered)
    # last: the builders above dedupe within themselves, never against each other
    items = conversation.unique_questions(items)
    return [{**s, "entry_id": s.get("entry_id") or ""} for s in items or []]


def _apply_result_document_cap(result: dict) -> dict:
    """Trim the FINISHED result to the platform's document cap.

    GLOBAL (config -> search -> max_result_documents on the global settings
    row, like search_requires_user): how much a client shows is one
    platform-wide presentation decision, unlike sources_top_n on the answer
    step, which decides what the answer is BUILT from and stays per
    organisation. The sources list keeps its first N entries; a paragraph
    whose document fell over the cap keeps its text but loses the link —
    a dangling source_rank would point at a source the response no longer
    carries. Absent / null = no cap. Mutates and returns `result`.
    """
    cap = (get_active_setting(None)["config"].get("search") or {}) \
        .get("max_result_documents")
    if not cap:
        return result
    cap = int(cap)
    try:
        sources = json.loads(result.get("sources_json") or "[]")
    except Exception:
        return result
    if len(sources) <= cap:
        return result
    result["sources_json"] = json.dumps(sources[:cap], ensure_ascii=False)
    parts = result.get("answer_parts") or []
    for part in parts:
        rank = part.get("source_rank")
        if rank is not None and rank > cap:
            part.update({"source_rank": None, "title": None, "url": None,
                         "file_uuid": None, "excerpt": None})
    return result


def _resolve_search_caller(req: "SearchRequest") -> str | None:
    """WHO is searching — verified user id, or None for an allowed anonymous.

    Policy per organisation (settings visibility.search_requires_user, init 99):

      * user_id SUPPLIED → verified against the entity or one above it, policy
        or no policy. A mismatch is 403, never a silent downgrade to anonymous:
        the request would still run with that organisation's scope and hand
        back its documents — measured once, 10 chunks of a school the caller
        had nothing to do with.
      * user_id MISSING + policy requires one → 403 with the same one generic
        sentence (the reason goes to the log; a specific message would make the
        error a way to probe users and entities).
      * user_id MISSING + policy allows it → anonymous: the private branch of
        the visibility predicate simply does not exist for this caller.
    """
    _lang = (_validate_language(req.language)
             if (req.language or "").strip() else None)
    if req.user_id:
        if not verify_user(req.user_id, ancestor_entities(req.legal_entity)):
            logger.warning("search: refused — user_id %r is not an active user "
                           "of %s or of an entity above it",
                           req.user_id, req.legal_entity)
            raise HTTPException(status_code=403,
                                detail=system_message("unknown_caller", _lang))
        return req.user_id
    # GLOBAL by decision (2026-08-03), unlike every other visibility switch:
    # whether the PLATFORM admits anonymous callers is one property of the
    # deployment — for all existing schools and any school added later — so it
    # is read from the global settings row only and an organisation's own row
    # cannot override it.
    _requires = bool((get_active_setting(None)["config"]
                      .get("visibility") or {})
                     .get("search_requires_user", True))
    if _requires:
        logger.warning("search: refused — no user_id and organisation of %s "
                       "requires one (search_requires_user)", req.legal_entity)
        raise HTTPException(status_code=403,
                            detail=system_message("unknown_caller", _lang))
    return None


def _grade_code(value) -> str | None:
    """Normalise the request's ročník onto a code_list('school_grade') code.

    The codebook speaks plain numbers on ONE unified axis (init 120): ZŠ years
    are '1'..'9', SŠ years are '10'..'13' (10 = 1st year SŠ … 13 = maturita).
    The caller sends their year on that axis; the resolved codebook of their
    UNIT decides which of the two ranges is real (a ZŠ unit has 1..9, an SŠ unit
    10..13), so a value outside the unit's set is simply ignored downstream.
    Everything non-numeric is ignored with a log line, never an error: the grade
    only ever PREFERS content, so a bad value must not fail the search (most
    chunks carry no grade at all).
    """
    if value is None or value == "":
        return None
    # Cheap sanity bound before touching the DB, NOT a statement about meaning:
    # which ročníky exist is the codebook's answer (the existence check below),
    # and which level they belong to is legal_entity.grade_levels via
    # edu_ai.grade_level() (migration 138). Keep the bound wide — the old
    # 1..9-only cap silently dropped every SŠ caller's ročník, so an SŠ student's
    # grade never reached the grade/level preference.
    s = str(value).strip()
    m = int(s) if s.isdigit() else None
    _lo = platform_int("grade_min", 1)
    _hi = platform_int("grade_max", 13)
    if m is None or not (_lo <= m <= _hi):
        logger.info("grade %r is outside the accepted range %d..%d — ignored",
                    value, _lo, _hi)
        return None
    from .db import get_cursor
    with get_cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {config.PG_SCHEMA}.code_list "
            "WHERE list_code = 'school_grade' AND code = %s AND is_active",
            (str(m),))
        if cur.fetchone() is None:
            logger.info("grade %r maps to %r which is not in the code list — ignored",
                        value, str(m))
            return None
    return str(m)


def _search_requires_role() -> bool:
    """GLOBAL policy (settings visibility.search_requires_role, init 127): must
    the caller state a role? One value platform-wide, on the global row."""
    return bool((get_active_setting(None)["config"].get("visibility") or {})
                .get("search_requires_role", True))


def _valid_role(role) -> str | None:
    """The caller's role coerced to a code_list('role') code, or None."""
    from .db import get_cursor
    try:
        with get_cursor() as cur:
            cur.execute(f"SELECT code FROM {config.PG_SCHEMA}.code_list "
                        "WHERE list_code = 'role' AND is_active")
            allowed = {r[0] for r in cur.fetchall()}
    except Exception as e:
        logger.warning("role lookup failed (%s) — treating as invalid", e)
        return None
    r = str(role or "").strip().upper()
    return r if r in allowed else None


def _role_gate(req: "SearchRequest",
               domains: list[str] | None = None) -> tuple[str | None, "SearchResponse | None"]:
    """(validated role, error-response-or-None). When a role is required and the
    caller gave none (or an unknown one), the SECOND item is a ready 200
    response carrying the localized 'role_required' message — the caller renders
    it like any answer. Role is a HARD audience filter (init 127); without it we
    cannot tell which documents the caller may see, so we ask rather than guess.

    `domains` is the ALREADY VALIDATED code set — _prepare_search runs the domain
    resolver BEFORE this gate for exactly this reason. So the early return
    reports the filter that would have applied instead of omitting it, and it is
    never the raw request value: this response cannot claim a filter nobody
    checked. No retrieval happens on this path, so the field is scope, not a
    statement about which sources were consulted.
    """
    role = _valid_role(req.user_role)
    if role or not _search_requires_role():
        return role, None
    lang = (req.language or "").strip().lower() or ui_default_language()
    return None, SearchResponse(
        answer=system_message("role_required", lang),
        outcome="role_required", answer_source="guard",
        language=lang, suggestions=[],
        # role stays null — that IS the finding here
        effective_scope=_effective_scope(req, domains=domains))


def _resolve_domains(req: "SearchRequest", org: list[str]) -> tuple[list[str] | None,
                                                                   list[int] | None]:
    """(canonical codes, filter ids) for the request — the two 422/503 boundaries.

    The translation from domain_scope's exceptions into this service's error
    envelope, in one place because three endpoints need it and a fourth will.

    422 carries `fields` pointing at `domains` or at the offending element, like
    every other rejected field VALUE here. 503 is the case the spec is emphatic
    about: the caller asked for a NARROWER search and the catalog could not be
    read, so the honest answer is "try again", never the wider search they did
    not ask for. Retryable, and raised before any retrieval — and, at every call
    site, before the idempotency claim, so a 422/503 leaves no claim hanging for
    the client's retry to collide with.
    """
    try:
        codes = domain_scope.normalize(req.domains)
        return domain_scope.resolve(req.legal_entity, codes, org)
    except domain_scope.DomainValidationError as e:
        raise HTTPException(status_code=422, detail={
            "code": "VALIDATION_ERROR", "message": e.message,
            "retryable": False, "fields": e.fields}) from e
    except domain_scope.DomainCatalogUnavailable as e:
        raise HTTPException(status_code=503, detail={
            "code": "UNAVAILABLE",
            "message": "The domain catalog could not be read, so the requested "
                       "area filter cannot be applied. Retry shortly.",
            "retryable": True}) from e


def _hashable_body(req: "SearchRequest",
                   domains: list[str] | None) -> dict:
    """The request body as the idempotency digest sees it.

    ONE rule, and it is a compatibility rule, not a new semantic: a NULL
    `domains` is dropped before hashing, everything else is left exactly as
    model_dump produces it.

    Why it has to be dropped. `body_hash` hashes the canonical model_dump, so
    adding a field with a default of None changes the digest of every request
    that does not send it — including the one a client sent five minutes ago and
    is now retrying. Its stored claim was hashed by the OLD code, from a dict
    with no `domains` key at all, and the retry would come back
    IDEMPOTENCY_CONFLICT: a client that did nothing wrong told its answer
    belongs to someone else. Dropping the key when it is null reproduces that
    old dict byte for byte.

    Why ONLY this field. Every other default (retrieval_only=False, debug=False,
    …) was already in the digest when the stored claims were written, so
    omitting one now would break the compatibility this exists to keep. The
    exemption is for fields ADDED after a claim could exist, and it is not a
    general "skip the defaults".

    An EXPLICIT filter goes in, in CANONICAL form (from the resolver, not the
    request): ["B","a"] and ["A","b"] are the same set and must replay each
    other, while the same key with a genuinely different scope must 409 — which
    is what including the codes achieves.
    """
    body = req.model_dump()
    if domains is None:
        body.pop("domains", None)
    else:
        body["domains"] = list(domains)
    return body


def _prepare_search(req: "SearchRequest") -> dict:
    """Identity, visibility scope and thematic scope — everything the cascade
    needs, resolved once and identically for /ask, /ask/stream and /explain.

    It existed three times, and the copies had drifted: the stream forgot
    params_override for a while, and /explain never resolved an entity at all —
    it ran the deterministic prefix with no entity_scope, so scope.where() saw
    nothing resolved and the diagnostic answered about a corpus nobody could
    reach. One function, so a new field cannot land on two of the three.

    ORDER MATTERS and is asserted by the tests:
      1. entity (an unknown one is a 400 — nothing else can be resolved)
      2. caller (403 for an unbacked user_id, before anything is read)
      3. domains (422/503) — BEFORE the role gate and before any idempotency
         claim, so the early `role_required` return can report a VALIDATED
         filter and a rejected request leaves no claim behind
      4. role (a 200 carrying `role_required`, returned by the caller)

    Returns the initial cascade state plus `_role`, `_role_err`, `_uid`,
    `_domain_codes` — the four things the endpoints themselves have to branch on.
    """
    # Two lists, because the levels read in different directions. `organization`
    # matches the whole tree of the legal person. `entity` reads UPWARD from the
    # publisher — my own documents plus those of everyone under me, so the head
    # office sees what its units published for themselves while siblings stay
    # invisible to each other. Empty for an unknown entity, and scope.where turns
    # that into "see nothing" rather than into no predicate at all.
    _org = organization_entities(req.legal_entity)
    # STRICT three-level model (2026-08-07):
    #   entity        → ONLY the owning entity. Nobody else: not the parent, not a
    #                   child unit, not a sibling. A document that must reach the
    #                   whole school is ingested as `organization`, not `entity`.
    #   organization  → everyone within the organisation (the whole tenant tree).
    #   (private       → the owner alone, handled in scope.where.)
    # A foreign organisation never sees anything — _org is resolved within one
    # legal person and never crosses. Empty _org (unknown entity) → see nothing.
    _scope = [req.legal_entity] if _org else []
    # WHO is asking — verified user, or an anonymous caller where the
    # organisation's policy allows one (see _resolve_search_caller).
    _uid = _resolve_search_caller(req)
    if not _org:
        raise HTTPException(
            status_code=400,
            detail=f"unknown or inactive legal entity '{req.legal_entity}'")

    _codes, _ids = _resolve_domains(req, _org)
    _role, _role_err = _role_gate(req, domains=_codes)

    initial = {
        "user_question": req.question,
        "legal_entity": req.legal_entity,
        "entity_scope": _scope,
        "organization_entities": _org,
        "user_id": _uid,
        "user_school_grade": _grade_code(req.user_school_grade),
        "user_role": _role,
        "language_filter": _validate_language(req.language) if (req.language or "").strip() else None,
        "domain_codes": _codes,
        "domain_ids": _ids,
        "conversation_id": req.conversation_id,
        "session_id": req.session_id,
    }
    if req.query_norm_tokens:
        initial["query_norm_tokens"] = req.query_norm_tokens
    if req.query_lemma_tokens:
        initial["query_lemma_tokens"] = req.query_lemma_tokens
    if req.query_stem_tokens:
        initial["query_stem_tokens"] = req.query_stem_tokens
    if req.params_override:
        initial["_params_override"] = req.params_override
    return {"initial": initial, "_uid": _uid, "_role": _role,
            "_role_err": _role_err, "_domain_codes": _codes}


def _validate_language(code: str) -> str:
    """Coerce + validate the language filter against the číselník (active rows)."""
    from .db import get_cursor
    with get_cursor() as cur:
        cur.execute(
            f"SELECT code FROM {config.PG_SCHEMA}.code_list "
            "WHERE list_code = 'language' AND is_active"
        )
        allowed = {r[0] for r in cur.fetchall()}
    c = code.strip().lower()
    if c not in allowed:
        # 422 with `fields`, like every other rejected field VALUE — 400 is for an
        # empty question and an unknown legal_entity. The old dict detail carried
        # no `code`, so the handler json.dumps()ed it into `message` and the client
        # got an escaped JSON object where the contract promises a sentence.
        # The allowed codes stay one call away: GET /v1/code-lists.
        msg = f"Unknown language {code!r}"
        raise HTTPException(status_code=422, detail={
            "code": "VALIDATION_ERROR", "message": f"language: {msg}",
            "retryable": False,
            "fields": [{"field": "language", "message": msg}]})
    return c


# ── Endpoints ─────────────────────────────────────────────────

@api.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse()


@v1.post("/ask", response_model=SearchResponse)
def search(req: SearchRequest, background_tasks: BackgroundTasks,
           http_response: Response,
           idempotency_key: str | None = Header(default=None,
                                                alias="Idempotency-Key")):
    """Run the full search cascade.

    Idempotency-Key is OPTIONAL, unlike spec §10.2 which requires it: making it
    mandatory would 400 every client that exists today, and the whole point of this
    round of changes was that they stay additive. Without the header the endpoint
    behaves exactly as before.
    """
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required")

    t0 = time.time()
    # no question text here: the full question goes to question_answer_log (log.py),
    # the container log gets identifiers and size only (AGENTS.md)
    logger.info("Search request (entity=%.64s, role=%.32s, chars=%d)",
                req.legal_entity, req.user_role, len(req.question))
    if req.selected_answer_key:
        # visible, not silent: whoever sends this expects the answer to come from
        # that entry, and today it does not
        logger.info("selected_answer_key=%s received — stored, NOT honoured "
                    "(cascade re-matches on the question text)",
                    req.selected_answer_key[:80])

    # entity → caller → domains → role, resolved once and shared with
    # /ask/stream and /explain. Domains are validated in here, i.e. BEFORE the
    # idempotency claim below, so a 422 or 503 cannot leave a claim the client's
    # retry would collide with.
    _prep = _prepare_search(req)
    initial = _prep["initial"]
    _uid, _role, _role_err = _prep["_uid"], _prep["_role"], _prep["_role_err"]
    _domains = _prep["_domain_codes"]
    if _role_err is not None:
        return _role_err

    # ── idempotent replay ──────────────────────────────────────
    # After the entity and caller are known, because the key is scoped to them, and
    # before the cascade runs — the point is not to run it twice. role_required above
    # is deliberately outside: it costs nothing to recompute and caching it would
    # outlive the client picking a role.
    _acct = _key = None
    if idempotency_key:
        _acct = idempotency.account_of(req.legal_entity, _uid)
        _key = idempotency_key
        _digest = idempotency.body_hash(_hashable_body(req, _domains))
        _state, _cached = idempotency.claim(_acct, _key, _digest)
        if _state == "replay":
            http_response.headers["Idempotency-Replayed"] = "true"
            logger.info("Search replayed from idempotency key")
            return SearchResponse(**_cached)
        if _state == "conflict":
            raise HTTPException(status_code=409, detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "Idempotency key was already used with a different request.",
                "retryable": False})
        if _state == "in_progress":
            raise HTTPException(status_code=409, detail={
                "code": "IDEMPOTENCY_IN_PROGRESS",
                "message": "The same request is still being processed.",
                "retryable": True})
        http_response.headers["Idempotency-Replayed"] = "false"

    # dev/eval: retrieval-only path — run the cascade up to (not incl.) the answer
    # LLM and return the ranked sources. Deterministic + token-free, for sweeps.
    if req.retrieval_only:
        # a dev sweep is not a client retry: hand the key back rather than caching it
        if _key:
            idempotency.release(_acct, _key)
        try:
            result, setting_id = run_search(initial, stop_before="answer")
        except Exception:
            # str(e) used to go out on the wire — the traceback is already in the log
            # above, and the caller can do nothing with an internal message anyway.
            logger.exception("Search cascade failed")
            raise HTTPException(status_code=500, detail="search failed")
        if result.get("outcome"):        # resolved early (QA hit / guard / empty pool)
            _src = result.get("sources_json", "[]")
        else:
            _prep = prepare_generation(result, result.pop("_stopped_params", {}) or {})
            _src = _prep["sources_json"] if _prep else "[]"
        return SearchResponse(
            answer="", outcome=result.get("outcome") or "retrieval",
            answer_source="retrieval", strategies=result.get("strategies", []),
            key_words_used=result.get("keywords_used", []),
            effective_scope=_effective_scope(req, result, _role, bool(_uid), _domains),
            sources=_sources_list(_src), language=ui_default_language(),
            duration_ms=int((time.time() - t0) * 1000),
            # same rule as the normal path — one behaviour, no special case to
            # remember. A sweep that wants the trace asks for it.
            trace=result.get("trace", []) if req.debug else [])

    try:
        result, setting_id = run_search(initial)
        _apply_result_document_cap(result)
    except Exception:
        logger.exception("Search cascade failed")
        # give the key back: the client's natural reaction to a 500 is to retry, and
        # a held claim would answer that retry with 'in_progress' for 5 minutes
        if _key:
            idempotency.release(_acct, _key)
        raise HTTPException(status_code=500, detail="search failed")

    duration_ms = int((time.time() - t0) * 1000)
    logger.info("Search completed in %d ms (outcome=%s via %s, setting=%s)",
                duration_ms, result.get("outcome"), result.get("answer_source"), setting_id)

    resp = SearchResponse(
        answer=result.get("answer", ""),
        outcome=result.get("outcome", "no_answer"),
        answer_source=result.get("answer_source", "none"),
        strategies=result.get("strategies", []),
        answer_parts=result.get("answer_parts", []),
        suggestions=_public_suggestions(result.get("suggestions"), req, _uid,
                                        _domains),
        matched_entry_id=result.get("matched_entry_id"),
        match_score=result.get("match_score"),
        sources=_sources_list(result.get("sources_json")),
        key_words_used=result.get("keywords_used", []),
        effective_scope=_effective_scope(req, result, _role, bool(_uid), _domains),
        language=result.get("user_language") or ui_default_language(),
        duration_ms=duration_ms,
        trace=result.get("trace", []),
    )
    # Log the tokens ACTUALLY used: if the client didn't send them, `prepare`
    # derived them server-side — capture those so question_answer_log.norm/lemma_query
    # reflects what search really matched on (not NULL).
    if not req.query_norm_tokens:
        req.query_norm_tokens = result.get("query_norm_tokens")
    if not req.query_lemma_tokens:
        req.query_lemma_tokens = result.get("query_lemma_tokens")
    if not req.query_stem_tokens:
        req.query_stem_tokens = result.get("query_stem_tokens")
    if not req.language:
        req.language = result.get("query_language")
        # grade as the cascade USED it: normalised field or text-derived; an
        # invalid input logs NULL, not the raw junk (a text column full of
        # "ZS6"/"12" would poison the per-grade reporting)
        req.user_school_grade = result.get("user_school_grade")   # detected → question_language in the log
    # Stored SYNCHRONOUSLY, unlike the logging below: a client that retries the
    # instant it gets the response must find it already there, and a background task
    # runs only after the response has gone out. `req` has been mutated by the token
    # backfill above, but the hash was taken from the original body at claim time and
    # a retry sends that same original — so they still match.
    # The wire copy drops the trace unless it was asked for; `resp` keeps it for
    # the logger below, whose flow_trace column is the reason it is gathered.
    # Idempotency stores the OUTGOING one, so a replay returns what the first
    # call returned rather than more.
    resp_out = resp if req.debug else resp.model_copy(update={"trace": []})
    if _key:
        idempotency.store(_acct, _key, resp_out.model_dump())
        background_tasks.add_task(idempotency.purge_expired)
    # append-only, fire-and-forget logging — runs after the response is sent,
    # never blocks or breaks the search (see log.py).
    background_tasks.add_task(log_search, req, resp, duration_ms, setting_id)
    # Answers are personalised by school, role and ročník — no shared cache along
    # the way has any business keeping one. GET číselník endpoints deliberately do
    # NOT get this: those are public reference data and SHOULD be cached.
    http_response.headers["Cache-Control"] = "no-store"
    return resp_out


@v1.get("/document-types")
def document_types():
    """Active document-type taxonomy (číselník edu_ai.taxonomy_rules, taxonomy_type_code='DOCUMENT_TYPE').

    Single source of truth for the classification LLM (via {{DOCUMENT_TYPES}}),
    for search filters, and — through the gateway proxy /v1/document-types —
    for the upload UI select menu. Read per request → edits apply immediately.
    """
    from .db import get_cursor
    with get_cursor() as cur:
        cur.execute(
            f"SELECT type_code, is_ui_default, labels "
            f"FROM {config.PG_SCHEMA}.taxonomy_rules "
            "WHERE is_active AND taxonomy_type_code = 'DOCUMENT_TYPE' ORDER BY sort_order"
        )
        rows = cur.fetchall()
    # `labels` ({"cs": …, "en": …, …}) carries the display names as DATA, so a new
    # language is an UPDATE — no DDL, no code change. (The legacy per-language
    # columns label_cs/label_en were dropped in migration 41.)
    return {"document_types": [
        {"type_code": r[0], "is_ui_default": r[1], "labels": r[2]} for r in rows
    ]}


@v1.get("/domains")
def domains(legal_entity: str | None = None):
    """Active DOMAIN taxonomy (edu_ai.taxonomy_rules, taxonomy_type_code='DOMAIN')
    — school-system situations (maturita, admissions, bullying, GDPR, …). Feeds
    the classification LLM ({{DOMAINS}}), the ingest declared-domain field, and
    the upload UI multiselect. Read per request → edits apply immediately.

    PER SCHOOL: with legal_entity the set is resolved nearest-owner-up
    (resolved_taxonomy_rules: the school's own domains, else the nearest parent's,
    else GLOBAL) — the same rule as subjects/grades. Without it → global.

    ALSO THE AREA PICKER'S CATALOG (spec.search-domain-scope §3), which is why it
    is now in the nginx allowlist as /search/v1/domains. `codes` a client may
    send as `domains` are exactly the `code` values here, for exactly the
    `legal_entity` it asked about — so the response echoes that entity back:
    without it a client cannot tell a catalog it asked for from the GLOBAL
    fallback it got, and a picker bound to the wrong school would offer areas
    the search will then reject with a 422.

    Kept ADDITIVE: `domains` is unchanged in shape and position, and the
    entity-less call still returns the GLOBAL set for the existing callers (the
    classification LLM's {{DOMAINS}}, the upload UI).
    """
    from .db import get_cursor
    with get_cursor() as cur:
        # Always resolve through the function: with an entity it returns that
        # school's set (its own, else global); with NULL it returns the GLOBAL set.
        # A plain "all DOMAIN rows" query would leak every school's rows once
        # domains are per-entity (17 total), so it must NOT be used here.
        cur.execute(
            f"SELECT type_code, labels "
            f"FROM {config.PG_SCHEMA}.resolved_taxonomy_rules(%s, 'DOMAIN') "
            "ORDER BY sort_order", (legal_entity,))
        rows = cur.fetchall()
    return {"legal_entity": legal_entity,
            "domains": [{"code": r[0], "labels": r[1]} for r in rows]}


VISIBILITY_VALUES = ("private", "entity", "organization")


def _child_may_publish_organization(legal_entity: str | None = None) -> bool:
    """The visibility-policy switch, read per request (hot-editable, like every
    other setting) — from the ORGANISATION's settings row (global when it has
    none), so two organisations can hold different policies. True = a unit may
    choose `organization`, which at its widest reaches the unit's own parent and
    siblings — organization_entities() never crosses to another legal person, so
    the switch cannot widen past the school.
    See postgres/init/92 + 98."""
    from .settings import get_active_setting
    return bool((get_active_setting(legal_entity)["config"]
                 .get("visibility") or {})
                .get("child_may_publish_organization", True))


@v1.get("/entities")
def entities():
    """Active legal entities, for the ingest form's first choice.

    Ingestion asks WHO before WHAT: the entity decides which users may be picked,
    so the form cannot offer a user list until this is answered.
    """
    from .db import get_cursor
    with get_cursor() as cur:
        cur.execute(
            f"SELECT legal_entity_code, display_name, parent_code, kind, level "
            f"FROM {config.PG_SCHEMA}.legal_entity "
            "WHERE is_active ORDER BY display_name")
        rows = cur.fetchall()
    # The form must not derive rights from rules of its own. It asks what an entity
    # MAY do and gets an answer; the rule stays here, where it is also enforced.
    # Two implementations of one rule drift, and `private_public_flag` is the
    # standing proof — it existed for a year and nothing ever checked it.
    # may_publish_organization is per organisation (init 98);
    # search_requires_user is GLOBAL by decision — one value for every school,
    # present and future, so it is read once from the global settings row
    _requires_user = bool((get_active_setting(None)["config"]
                           .get("visibility") or {})
                          .get("search_requires_user", True))
    return {"entities": [
        {"legal_entity_code": r[0], "display_name": r[1], "parent_code": r[2],
         "kind": r[3], "level": r[4],
         # the top-level school always; a unit per its org's settings switch
         "may_publish_organization": (r[3] == "legal_person"
                                      or _child_may_publish_organization(r[0])),
         # platform-wide: does /search insist on a verified user?
         "search_requires_user": _requires_user}
        for r in rows]}


@v1.get("/staff-users")
def staff_users(legal_entity: str):
    """Active staff users OF EXACTLY THIS ENTITY — for both forms.

    An include_ancestors variant existed for one afternoon (the head-office
    ředitelka searching AS her unit) and was rolled back by decision: the forms
    must not offer parent-entity users under a unit at all. /search itself still
    ACCEPTS a verified user of an entity above (a mobile client may know such a
    caller); this endpoint is about what the forms offer, not what the API
    tolerates.
    """
    from .db import get_cursor
    with get_cursor() as cur:
        cur.execute(
            f"SELECT user_id, display_name, legal_entity_code "
            f"FROM {config.PG_SCHEMA}.staff_user "
            "WHERE legal_entity_code = %s AND is_active ORDER BY display_name",
            (legal_entity,))
        rows = cur.fetchall()
    return {"staff_users": [
        {"user_id": r[0], "display_name": r[1], "legal_entity_code": r[2]}
        for r in rows]}


@v1.get("/ingest-principal")
def ingest_principal(legal_entity: str, user_id: str, visibility: str = "entity"):
    """Validate who is ingesting, for which entity, and how far it will reach.

    The gateway needs this and cannot answer it itself: edu_ai_api is deliberately
    off the `data` network ("no direct DB access" — docker-compose.yml), so the
    internet-facing service holds no database credentials. It already validates
    document_type through /document-types for the same reason; this is the same
    route, with one difference — it is a HARD dependency. A document-type lookup
    that fails may pass the value through, because the worst case is an
    unclassified document. An entity that cannot be verified must NOT be accepted:
    that is the whole isolation boundary.

    Returns the entity and the user's display name, so the gateway stops taking
    the uploader's name from the client and takes it from the register instead.

    404 — no such active entity, or the user does not belong to it
    400 — unknown visibility
    """
    if visibility not in VISIBILITY_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"visibility must be one of {', '.join(VISIBILITY_VALUES)}")

    from .db import get_cursor
    with get_cursor() as cur:
        cur.execute(
            f"SELECT display_name, kind FROM {config.PG_SCHEMA}.legal_entity "
            "WHERE legal_entity_code = %s AND is_active", (legal_entity,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"unknown or inactive legal entity '{legal_entity}'")
        entity_name, entity_kind = row[0], row[1]

        # Whether a unit may choose `organization` is policy, so it is a settings
        # switch (child_may_publish_organization, init script 92). At its widest a
        # unit's `organization` document reaches its own parent and siblings —
        # never another school; organization_entities() cannot cross a legal
        # person whatever the switch says. When the switch is off: refused, not
        # silently downgraded to `entity` — a downgrade would hide the refusal in
        # data nobody rereads.
        if (visibility == "organization" and entity_kind == "unit"
                and not _child_may_publish_organization(legal_entity)):
            raise HTTPException(
                status_code=400,
                detail=(f"'{legal_entity}' is a unit; publishing to the whole "
                        f"organisation is disabled for units "
                        f"(child_may_publish_organization)"))

        # The user must belong to EXACTLY this entity. This used to allow the
        # entities above it — but whether a particular parent-entity user may
        # ingest into a unit is a roles question (the head office's ředitelka
        # perhaps, the hospodářka perhaps not), and roles are deliberately not
        # built yet. Until they are, nobody from above ingests into a unit:
        # allowing all of them by default was the wrong default. Searching AS
        # the unit is unaffected (/search verifies against ancestors).
        # It also keeps the owner foreign key honest — the pair
        # (legal_entity_code, owner_user_id) exists only for the entity's own
        # users, so an ancestor's private upload would have been refused by the
        # database anyway, after the flow had already run.
        cur.execute(
            f"SELECT display_name FROM {config.PG_SCHEMA}.staff_user "
            "WHERE user_id = %s AND legal_entity_code = %s AND is_active",
            (user_id, legal_entity))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=(f"user '{user_id}' is not an active staff user of "
                        f"'{legal_entity}'"))
        user_name = row[0]

    return {"legal_entity_code": legal_entity, "legal_entity_name": entity_name,
            "user_id": user_id, "user_display_name": user_name,
            "visibility": visibility}


@v1.get("/qa-packs")
def qa_packs(legal_entity: str):
    """Active QA packs of one entity — what there is to download and edit.

    entry_count is COUNTED from the live entries rather than read from
    qa_pack_manifest.entry_count: the manifest column records what the ingest
    wrote, and the honest answer to "how much is in this pack" is what is
    active now.
    """
    from .db import get_cursor
    with get_cursor() as cur:
        cur.execute(
            f"""SELECT m.pack_id, m.version, m.lang, m.built_at,
                       count(e.entry_id) AS entry_count
                  FROM {config.PG_SCHEMA}.qa_pack_manifest m
                  LEFT JOIN {config.PG_SCHEMA}.qa_entries e
                         ON e.pack_id = m.pack_id
                        AND e.legal_entity_code = m.legal_entity_code
                        AND e.is_active
                 WHERE m.legal_entity_code = %s AND m.is_active
                 GROUP BY m.pack_id, m.version, m.lang, m.built_at
                 ORDER BY m.pack_id""",
            (legal_entity,))
        rows = cur.fetchall()
    return {"qa_packs": [
        {"pack_id": r[0], "version": r[1], "lang": r[2],
         "built_at": r[3].isoformat() if r[3] else None,
         "entry_count": r[4]}
        for r in rows]}


@v1.get("/qa-export")
def qa_export(legal_entity: str, pack_id: str):
    """One active QA pack as the CSV that the ingest reader takes straight back.

    Composed HERE and not in the gateway on purpose: the reader's contract —
    which columns, which delimiter, BOM or not — then has exactly one owner, so
    an export cannot drift away from what the import accepts. The gateway adds
    only the download headers.

    entry_key leads the file because it is the identity the upload matches on:
    keep it and an edited row UPDATES that entry, clear it and the row becomes a
    new one. is_active is exported as TRUE throughout — everything here is active
    by definition — and exists so a curator can set FALSE to remove an entry
    instead of deleting the line, which in merge mode does nothing.

    `sources` (asset_refs) is written back in the reader's own line format so the
    round-trip is lossless — see _sources_cell.
    """
    from .db import get_cursor
    with get_cursor() as cur:
        # 404 rather than an empty file: "this pack is not yours / does not
        # exist" and "this pack has no entries" are different answers, and a
        # curator who gets a 0-row CSV for a typo'd name would believe the pack
        # was empty and re-upload over it.
        cur.execute(
            f"SELECT 1 FROM {config.PG_SCHEMA}.qa_pack_manifest "
            "WHERE pack_id = %s AND legal_entity_code = %s AND is_active",
            (pack_id, legal_entity))
        if cur.fetchone() is None:
            raise HTTPException(
                status_code=404,
                detail=(f"no active QA pack '{pack_id}' for '{legal_entity}'"))

        # The scope predicate is not optional: the unique index is
        # (legal_entity_code, pack_id), so two schools legitimately hold a pack
        # of the same name, and without the entity one would download the
        # other's curated answers.
        #
        # qa_variants has NO `role` column — the exploder emits role='canonical'
        # but the table does not store it, so the canonical question sits there
        # as an ordinary phrasing. Excluding it by comparison is what keeps a
        # round-trip stable; leave it in and every cycle appends it to
        # `variants` again, cumulatively.
        cur.execute(
            f"""SELECT coalesce(e.entry_key, '')                     AS entry_key,
                       e.canonical_q,
                       e.answer_md,
                       coalesce(e.tags->>'category', '')             AS cat,
                       coalesce(string_agg(v.phrasing, E'\n'
                                           ORDER BY v.variant_id), '') AS variants,
                       e.asset_refs,
                       ARRAY(SELECT DISTINCT t.type_code
                             FROM {config.PG_SCHEMA}.taxonomy_rules t
                             WHERE t.id = ANY(e.domain_ids)
                             ORDER BY t.type_code) AS domains
                  FROM {config.PG_SCHEMA}.qa_entries e
                  LEFT JOIN {config.PG_SCHEMA}.qa_variants v
                         ON v.entry_id = e.entry_id
                        AND v.is_active
                        AND lower(v.phrasing) <> lower(e.canonical_q)
                 WHERE e.pack_id = %s
                   AND e.legal_entity_code = %s
                   AND e.is_active
                 GROUP BY e.entry_id, e.entry_key, e.canonical_q, e.answer_md,
                          e.tags, e.asset_refs
                 ORDER BY e.entry_key NULLS LAST, e.canonical_q""",
            (pack_id, legal_entity))
        rows = cur.fetchall()

    from .qa_csv import rows_to_csv_bytes
    csv_rows = [(r[0], r[1], r[2], r[3], r[4], _sources_cell(r[5]),
                 json.dumps(r[6])) for r in rows]
    return Response(content=rows_to_csv_bytes(csv_rows),
                    media_type="text/csv; charset=utf-8")


@v1.get("/validity-periods")
def validity_periods(legal_entity: str | None = None):
    """Validity periods defined for the entity's ORGANISATION
    (edu_ai.validity_period; a unit resolves to its top-level organisation —
    periods are defined once per organisation and apply to all its schools).
    Gateway proxies as /v1/validity-periods; the upload test page shows them
    for the selected entity. Read-only — defining periods is an admin task."""
    from .db import get_cursor
    rows = []
    with get_cursor() as cur:
        if legal_entity:
            # walk up to the top-level organisation (root of the entity's tree)
            cur.execute(
                f"""WITH RECURSIVE up AS (
                        SELECT legal_entity_code, parent_code
                          FROM {config.PG_SCHEMA}.legal_entity
                         WHERE legal_entity_code = %s
                        UNION ALL
                        SELECT e.legal_entity_code, e.parent_code
                          FROM {config.PG_SCHEMA}.legal_entity e
                          JOIN up ON e.legal_entity_code = up.parent_code)
                    SELECT legal_entity_code FROM up
                     WHERE parent_code IS NULL LIMIT 1""",
                (legal_entity,))
            row = cur.fetchone()
            org = row[0] if row else legal_entity
            cur.execute(
                f"SELECT id, legal_entity_code, name, valid_from, valid_to, is_default "
                f"FROM {config.PG_SCHEMA}.validity_period "
                "WHERE is_active AND legal_entity_code = %s "
                "ORDER BY is_default DESC, valid_from NULLS FIRST, id",
                (org,))
            rows = cur.fetchall()
    return {"validity_periods": [
        {"id": r[0], "legal_entity_code": r[1], "name": r[2],
         "valid_from": (r[3].isoformat() if r[3] else None),
         "valid_to": (r[4].isoformat() if r[4] else None),
         "is_default": r[5]} for r in rows]}


@v1.get("/code-lists")
def code_lists(legal_entity: str | None = None):
    """All generic code lists (edu_ai.code_list): languages (ISO 639-1),
    school subjects, school grades — code + labels {lang: name}, grouped by
    list_code. Same serving pattern as /document-types (per-request read,
    gateway proxies as /v1/code-lists for UI select menus).

    With `legal_entity` the lists are the ENTITY'S resolved sets
    (edu_ai.resolved_code_list): school lists are the organisation's OWN rows
    or nothing, 'language' is its selection or the platform set — and the
    entity may be ANY node of the tree, a unit resolves to its organisation
    (init 107-109). A UI select fed from this can therefore offer the grades
    that actually exist for whatever the user picked, unit or school."""
    from .db import get_cursor
    with get_cursor() as cur:
        if legal_entity:
            cur.execute(
                f"SELECT DISTINCT list_code FROM {config.PG_SCHEMA}.code_list "
                "WHERE is_active ORDER BY list_code")
            list_codes = [r[0] for r in cur.fetchall()]
            rows = []
            for lc in list_codes:
                cur.execute(
                    f"SELECT list_code, code, is_ui_default, "
                    f"       is_processing_fallback, labels "
                    f"FROM {config.PG_SCHEMA}.resolved_code_list(%s, %s) "
                    "ORDER BY sort_order, code",
                    (legal_entity, lc))
                rows.extend(cur.fetchall())
        else:
            cur.execute(
                f"SELECT list_code, code, is_ui_default, is_processing_fallback, labels "
                f"FROM {config.PG_SCHEMA}.code_list "
                "WHERE is_active ORDER BY list_code, sort_order"
            )
            rows = cur.fetchall()
    out: dict[str, list] = {}
    # is_ui_default is for PRESENTATION (which option a form preselects);
    # is_processing_fallback is what the pipeline uses when it cannot determine
    # the value. Two different consumers — see migration 42.
    for list_code, code, ui_default, proc_fallback, labels in rows:
        out.setdefault(list_code, []).append(
            {"code": code, "is_ui_default": ui_default,
             "is_processing_fallback": proc_fallback, "labels": labels})
    return {"code_lists": out}


@v1.get("/ui-labels")
def ui_labels(scope: str):
    """Interface copy for one scope (edu_ai.ui_label), plus which languages may
    actually be offered — gateway proxies this as /v1/ui-labels.

    `available` is the intersection of the ACTIVE languages in
    code_list('language') and the languages that hold EVERY key of the scope: a
    page must not offer a language it cannot fully render. `default` is
    code_list.is_ui_default when that language is available, else the first
    available one (en preferred) — so a page always has something to render.

    `incomplete` lists, per language, the keys it is missing. Without it the
    availability rule is a trap: one INSERT of a new key in Czech only would drop
    three languages out of every menu with no way to see why. With it, the same
    event produces a to-translate list.
    """
    from .db import get_cursor
    with get_cursor() as cur:
        cur.execute(
            f"SELECT label_key, lang, label_text FROM {config.PG_SCHEMA}.ui_label "
            "WHERE is_active AND scope = %s", (scope,))
        rows = cur.fetchall()
        cur.execute(
            f"SELECT code, is_ui_default FROM {config.PG_SCHEMA}.code_list "
            "WHERE is_active AND list_code = 'language' ORDER BY sort_order")
        langs = cur.fetchall()

    labels: dict[str, dict[str, str]] = {}
    for key, lang, text in rows:
        labels.setdefault(lang, {})[key] = text
    all_keys = {key for key, _lang, _t in rows}

    active = [code for code, _d in langs]
    ui_default = next((code for code, d in langs if d), None)
    # An empty scope must yield NO language rather than "everyone has all zero
    # keys" — otherwise a page would offer four languages and render none.
    available, incomplete = [], {}
    if all_keys:
        for code in active:
            missing = sorted(all_keys - set(labels.get(code, {})))
            if missing:
                incomplete[code] = missing
            else:
                available.append(code)

    default = ui_default if ui_default in available else (
        "en" if "en" in available else (available[0] if available else None))
    return {"scope": scope, "default": default, "available": available,
            "labels": {c: labels.get(c, {}) for c in available},
            "incomplete": incomplete}


@v1.get("/query-log")
def query_log(limit: int = 10, session_id: str | None = None):
    """Recent rows of edu_ai.question_answer_log — what the search actually recorded.

    The log is written in a BackgroundTask after the response is sent, so the row
    for a query appears a moment after the answer does; the test page polls
    briefly rather than expecting it to be there instantly.

    Learner questions are sensitive: this returns the query text, so it is a
    diagnostic endpoint for the test pages, not something to expose at the edge
    (see the nginx note for /ingest-status — same reasoning).
    """
    from .db import get_cursor
    limit = max(1, min(int(limit), 100))
    where, params = "", []
    if session_id:
        where, params = "WHERE session_id = %s", [session_id]
    with get_cursor() as cur:
        cur.execute(
            f"""SELECT l.id, l.created_at, l.raw_query, l.norm_query, l.lemma_query,
                       l.stem_query, l.stage, l.answer_source, l.strategies_text,
                       l.match_score, l.matched_entry_id, l.latency_ms,
                       l.question_language, l.answer_language, s.label,
                       l.flow_trace
                FROM {config.PG_SCHEMA}.question_answer_log l
                LEFT JOIN {config.PG_SCHEMA}.settings s USING (setting_id)
                {where}
                ORDER BY l.id DESC LIMIT {limit}""", params)
        rows = cur.fetchall()
    cols = ("id", "created_at", "raw_query", "norm_query", "lemma_query",
            "stem_query", "stage", "answer_source", "strategies_text",
            "match_score", "matched_entry_id", "latency_ms",
            "question_language", "answer_language", "setting_label",
            "flow_trace")
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None
        d["matched_entry_id"] = str(d["matched_entry_id"]) if d["matched_entry_id"] else None
        out.append(d)
    return {"rows": out, "count": len(out)}


@v1.get("/ingest-status")
def ingest_status(processing_uuid: str | None = None,
                  since_seconds: int | None = None):
    """Per-file progress of one ingest run (edu_ai.ingestion_log) — gateway
    proxies this as /v1/ingest-status for the upload test page to poll.

    Either identify the run by `processing_uuid`, or pass `since_seconds` to get
    the most recent run that STARTED within that window. The second mode exists
    because POST /v1/ingest is synchronous: it returns the processing_uuid only
    once the whole flow has finished, so a page that waits for it can never show
    progress. Polling by window lets the page start watching the moment it
    submits. Seconds rather than a timestamp keeps it free of client clock skew —
    the cutoff is computed here.

    Two things about ingestion_log shape the response:

    * `ingestion_log.file_name` holds the file_uuid, not a name, so a readable
      label has to come from the metadata row — source_url first (for a URL
      ingest that is what the user typed), then original_file_name, then the
      uuid's head.
    * every row in that table is level INFO; there is no error row, ever. So this
      endpoint CANNOT report failure, and deliberately does not pretend to: it
      returns facts (last event, age, chunk count) and no invented state. A
      stalled run shows as a growing `seconds_since_last`.

    `chunks` is the only trustworthy "stored" signal — the gateway reporting
    "completed" does not mean the rows landed (the markdown flow is async).
    """
    from .db import get_cursor
    with get_cursor() as cur:
        if not processing_uuid:
            if not since_seconds:
                raise HTTPException(
                    status_code=400,
                    detail="pass processing_uuid or since_seconds")
            # Newest run that STARTED inside the window. A run that began before
            # it is not "the upload the user just submitted", however chatty it is.
            cur.execute(
                f"SELECT processing_uuid FROM {config.PG_SCHEMA}.ingestion_log "
                "GROUP BY processing_uuid "
                "HAVING min(log_timestamp) >= localtimestamp - make_interval(secs => %s) "
                "ORDER BY min(log_timestamp) DESC LIMIT 1",
                (int(since_seconds),))
            row = cur.fetchone()
            if not row:
                return {"processing_uuid": None, "files": [], "file_count": 0}
            processing_uuid = str(row[0])

        cur.execute(
            f"""
            WITH f AS (
                SELECT file_uuid,
                       min(log_timestamp) AS started,
                       max(log_timestamp) AS last_at,
                       count(*)           AS events,
                       max(id)            AS last_id
                FROM {config.PG_SCHEMA}.ingestion_log
                WHERE processing_uuid = %s
                GROUP BY file_uuid
            )
            SELECT f.file_uuid, f.started, f.last_at, f.events,
                   l.log_message, l.component_name, l.log_level,
                   m.source_url, m.original_file_name, m.file_name,
                   EXTRACT(EPOCH FROM (localtimestamp - f.last_at))::int AS since_last,
                   EXTRACT(EPOCH FROM (f.last_at - f.started))::int      AS duration,
                   (SELECT count(*) FROM {config.PG_SCHEMA}.document_chunks c
                     WHERE c.file_uuid = f.file_uuid)                    AS chunks
            FROM f
            JOIN {config.PG_SCHEMA}.ingestion_log l ON l.id = f.last_id
            LEFT JOIN {config.PG_SCHEMA}.document_metadata m
                   ON m.file_uuid = f.file_uuid
            ORDER BY f.started
            """, (processing_uuid,))
        rows = cur.fetchall()

        # Full event sequence per file, oldest first — the panel is meant to show
        # the pipeline WALKING (flow started -> judge -> md saved -> split ->
        # metadata -> chunks), not just where it currently stands. Bounded so one
        # pathological run cannot return an unbounded document.
        cur.execute(
            f"SELECT file_uuid, log_timestamp, component_name, log_level, log_message "
            f"FROM {config.PG_SCHEMA}.ingestion_log "
            "WHERE processing_uuid = %s ORDER BY id LIMIT 500", (processing_uuid,))
        ev_rows = cur.fetchall()

    events_by_file: dict[str, list] = {}
    for fu_e, at, comp_e, lvl_e, msg_e in ev_rows:
        events_by_file.setdefault(str(fu_e), []).append(
            {"at": at.isoformat() if at else None, "component": comp_e,
             "level": lvl_e, "message": msg_e})

    # Same helper the answer step uses for its `sources`, so the name shown while
    # a document is being ingested is the name it will carry in a search result —
    # they cannot drift apart.
    from .nodes.answer import source_display

    files = []
    for (fu, started, last_at, events, msg, comp, level, url, orig, fname,
         since_last, duration, chunks) in rows:
        title, link = source_display({"source_url": url,
                                      "original_file_name": orig,
                                      "file_name": fname})
        files.append({
            "file_uuid": str(fu),
            "title": title or (str(fu)[:8] + "…"),   # as a search result shows it
            "url": link,
            "file_name": fname,
            "label": title or url or orig or (str(fu)[:8] + "…"),  # back-compat
            "started_at": started.isoformat() if started else None,
            "last_at": last_at.isoformat() if last_at else None,
            "events": events,
            "last_message": msg,
            "last_component": comp,
            "last_level": level,
            "seconds_since_last": since_last,
            "duration_s": duration,
            "chunks": chunks,
            "events": [
                {**e, "offset_s": max(0, int(
                    (datetime.fromisoformat(e["at"]) - started).total_seconds()))
                 if (e["at"] and started) else None}
                for e in events_by_file.get(str(fu), [])
            ],
        })
    return {"processing_uuid": processing_uuid, "files": files,
            "file_count": len(files)}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@v1.post("/ask/stream")
def search_stream(req: SearchRequest):
    """SSE variant of /search — same cascade, same answer, token streaming.

    Events (text/event-stream):
      meta  — document path only, before generation: outcome/source/strategies/
              sources/match_score are known up front
      delta — {"t": "<token>"} pieces of the generated answer, in order
      done  — the complete SearchResponse payload; ALWAYS the last event and the
              ONLY event for non-LLM outcomes (QA hit / guard / suggest /
              no_answer — those answer in ~ms, there is nothing to stream)
      error — cascade failure (terminal)

    Curated QA answers stay instant; the document path shows first tokens in
    ~0.5–1 s instead of waiting the full generation time.
    """
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required")

    t0 = time.time()
    # same rule as /ask: identifiers only, the text lives in question_answer_log
    logger.info("Stream search request (entity=%.64s, role=%.32s, chars=%d)",
                req.legal_entity, req.user_role, len(req.question))

    # SAME preparation as /ask, and OUTSIDE gen(): an HTTP error must be an HTTP
    # error. Once StreamingResponse has been returned the status line is already
    # 200 and a validation failure could only be reported as an SSE `error`
    # event, which is not what a 422 means. So the domain filter is validated
    # here — before the stream opens — exactly as the spec requires, and a bad
    # code gets the identical envelope /ask returns.
    #
    # It also picks up what this copy had drifted on: params_override was missing
    # for a while, and there is now one place for a new field to be added.
    _prep = _prepare_search(req)
    initial = _prep["initial"]
    _uid, _role, _role_err = _prep["_uid"], _prep["_role"], _prep["_role_err"]
    _domains = _prep["_domain_codes"]

    def gen():
        # role gate first — a missing/invalid role answers in one `done` event
        # (no cascade), same as the blocking endpoint's early return
        if _role_err is not None:
            yield _sse("done", _role_err.model_dump())
            return
        try:
            result, setting_id = run_search(initial, stop_before="answer")
        except Exception:
            logger.exception("Stream search cascade failed")
            # The SAME envelope as every other error, and without str(e): the
            # blocking path stopped putting the exception on the wire and this
            # one kept doing it, so one endpoint's failure looked nothing like
            # the other's. The traceback is already in the log above.
            yield _sse("error", _error_body("INTERNAL_ERROR", "search failed",
                                            True))
            return

        if result.pop("_stopped_before", None):
            step_params = result.pop("_stopped_params", {}) or {}
            prep = prepare_generation(result, step_params)
            if prep is None:
                result.update(resolve_unanswered(result))
            else:
                yield _sse("meta", {
                    "outcome": "answer",
                    "answer_source": "rag",
                    "strategies": result.get("strategies", []),
                    "sources": _sources_list(prep["sources_json"]),
                    "match_score": prep["match_score"],
                    "key_words_used": result.get("keywords_used", []),
                })
                pieces: list[str] = []
                # v5 NO-ANSWER protocol: the whole output may be "NO_ANSWER" +
                # VARIANT lines. Tokens are held back until the head can no
                # longer be that marker — an apologetic non-answer must never
                # reach the user token by token (see nodes/answer.py).
                _held: list[str] = []
                _verdict: str | None = None   # None undecided | "answer" | "no_answer"
                _fwd_len = 0                  # kolik znaků odpovědi už odteklo
                _gen_failed = False
                try:
                    llm = get_chat(prep["model"], prep["max_tokens"],
                                   prep.get("reasoning_effort"),
                                   prep.get("temperature"))
                    for part in llm.stream(prep["messages"]):
                        tok = part.content or ""
                        if not tok:
                            continue
                        pieces.append(tok)
                        if _verdict == "answer":
                            # trailing "VARIANT: …" lines (v6 grade-mismatch
                            # rephrasing) belong to suggestions, not the text —
                            # forward only up to a possible marker start and
                            # hold a short tail in case the marker is split
                            # across tokens
                            _acc = "".join(pieces)
                            _cut = _acc.upper().find("\nVARIANT")
                            _safe = _cut if _cut >= 0 else max(
                                _fwd_len, len(_acc) - 9)
                            if _safe > _fwd_len:
                                yield _sse("delta", {"t": _acc[_fwd_len:_safe]})
                                _fwd_len = _safe
                            continue
                        if _verdict == "no_answer":
                            continue          # collect silently, resolved below
                        _held.append(tok)
                        head = "".join(_held).lstrip()
                        if head.upper().startswith("NO_ANSWER"):
                            _verdict = "no_answer"
                        elif "NO_ANSWER".startswith(head[:9].upper()) and len(head) < 9:
                            continue          # still could become the marker
                        else:
                            _verdict = "answer"
                            _flush = "".join(_held)
                            _fwd_len = len("".join(pieces)) - len(_flush)
                            yield _sse("delta", {"t": _flush})
                            _fwd_len += len(_flush)
                except Exception as e:  # degrade mid-stream, never crash the response
                    _gen_failed = True
                    logger.warning("stream: generation failed (%s)", e)
                _raw_full = "".join(pieces)
                if _verdict == "answer":
                    _cut = _raw_full.upper().find("\nVARIANT")
                    _end = _cut if _cut >= 0 else len(_raw_full)
                    if _end > _fwd_len:
                        yield _sse("delta", {"t": _raw_full[_fwd_len:_end]})
                _full = _raw_full.strip()
                _noans, _variants = parse_no_answer(_full)
                if _gen_failed:
                    # BEFORE the branches below, and before _noans: a half-written
                    # answer must not be graded as one. `elif pieces:` used to
                    # claim outcome=answer for it — the deltas had already been
                    # sent, so the user got a sentence that stops mid-word and
                    # the log recorded a success.
                    #
                    # What was streamed cannot be unsent, so it stays and the
                    # notice is appended to it. The outcome is what changes: the
                    # model failed, and that is now sayable.
                    _msg = system_message("llm_unavailable", prep.get("language"))
                    result.update({
                        "resolved": True, "outcome": "llm_unavailable",
                        "answer_source": "none",
                        "answer": (f"{_full}\n\n{_msg}" if _full else _msg),
                        "sources_json": prep["sources_json"] if _full else "[]",
                        "suggestions": without_clarify(prep.get("suggestions")),
                        "match_score": prep["match_score"],
                        "user_language": prep.get("language"),
                    })
                elif _noans:
                    result.update(resolve_unanswered(result))
                    result["suggestions"] = merge_suggestions(
                        variant_chips(result, _variants),
                        without_clarify(prep.get("suggestions")),
                        result.get("suggestions"))
                elif pieces:
                    _clean, _trail = extract_trailing_variants(_full)
                    _sugg = prep.get("suggestions") or []
                    _mm = prep.get("grade_mismatch")
                    if _trail and _mm:
                        qg, pg = _mm
                        _sugg = merge_suggestions(
                            [{"kind": "clarify",
                              "question": system_message(
                                  "clarify_domains", prep.get("language")),
                              "options": [
                                  {"label": f"{qg}. ročník",
                                   "question": req.question},
                                  {"label": f"{pg}. ročník",
                                   "question": _trail[0]},
                              ]}], _sugg)
                    elif _trail:
                        _sugg = merge_suggestions(
                            variant_chips(result, _trail), _sugg)
                    result.update({
                        "resolved": True, "outcome": "answer", "answer_source": "rag",
                        "answer": _clean,
                        "sources_json": prep["sources_json"],
                        "suggestions": _sugg,
                        "match_score": prep["match_score"],
                        "user_language": prep.get("language"),
                    })
                else:
                    result.update(resolve_unanswered(result))

        _apply_result_document_cap(result)
        duration_ms = int((time.time() - t0) * 1000)
        resp = SearchResponse(
            answer=result.get("answer", ""),
            outcome=result.get("outcome", "no_answer"),
            answer_source=result.get("answer_source", "none"),
            strategies=result.get("strategies", []),
            answer_parts=result.get("answer_parts", []),
            suggestions=_public_suggestions(result.get("suggestions"), req, _uid,
                                        _domains),
            matched_entry_id=result.get("matched_entry_id"),
            match_score=result.get("match_score"),
            sources=_sources_list(result.get("sources_json")),
            key_words_used=result.get("keywords_used", []),
            effective_scope=_effective_scope(req, result, _role, bool(_uid), _domains),
            language=result.get("user_language") or ui_default_language(),
            duration_ms=duration_ms,
        )
        # same token backfill + append-only logging as /search (log_search never raises)
        if not req.query_norm_tokens:
            req.query_norm_tokens = result.get("query_norm_tokens")
        if not req.query_lemma_tokens:
            req.query_lemma_tokens = result.get("query_lemma_tokens")
        if not req.query_stem_tokens:
            req.query_stem_tokens = result.get("query_stem_tokens")
        if not req.language:
            req.language = result.get("query_language")
        # grade as the cascade USED it: normalised field or text-derived; an
        # invalid input logs NULL, not the raw junk (a text column full of
        # "ZS6"/"12" would poison the per-grade reporting)
        req.user_school_grade = result.get("user_school_grade")
        # logged with the trace, sent without it — same rule as /v1/ask
        log_search(req, resp, duration_ms, setting_id)
        logger.info("Stream search completed in %d ms (outcome=%s via %s)",
                    duration_ms, resp.outcome, resp.answer_source)
        _out = resp if req.debug else resp.model_copy(update={"trace": []})
        yield _sse("done", _out.model_dump())

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        # X-Accel-Buffering: nginx must NOT buffer the stream (web_chat sits behind it).
        # no-store, not just no-cache: the answer is personalised by school and role,
        # so no cache along the way should keep a copy at all.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@v1.post("/explain")
def explain(req: SearchRequest):
    """Debug: the deterministic prefix (prepare → route → qa_exact → qa_lexical).

    CONTRACT CHANGE, 2026-09-06 — deliberate and stated. Until now this built its
    own two-key state and ignored the rest of the request: no entity, no caller,
    no role, no language. scope.where() therefore saw nothing resolved, which is
    "see nothing", and the endpoint's usefulness rested on that accident rather
    than on a rule. It is also in the nginx allowlist, so it is a PUBLISHED
    diagnostic — and a published endpoint that resolved identity loosely would be
    a way to read curated material without the entity/role/visibility gate.

    So it now runs the same _prepare_search as /ask: the same 400 for an unknown
    entity, the same 403 for an unbacked user_id, the same 422/503 for a bad
    domain filter, the same role gate, and the same thematic scope. Same
    diagnostic value; the identity behind it is now a rule with a test.

    What does NOT change: it stays deterministic, spends no LLM call, and stops
    after qa_lexical. `role_required` is returned as the same 200 body /ask
    returns rather than as a state dump, because there is no state to dump —
    nothing ran.
    """
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required")
    from .nodes.prepare import prepare
    from .nodes.route import route
    from .nodes.qa_exact import qa_exact
    from .nodes.qa_lexical import qa_lexical

    _prep = _prepare_search(req)
    if _prep["_role_err"] is not None:
        return _prep["_role_err"]
    state = dict(_prep["initial"])

    # THE ACTIVE SETTING'S CONFIG, which this endpoint never passed and now must.
    # prepare() builds the token forms from the normalization config, and since
    # eduai_normalize 3.0.0 the stoplist is DATA — no built-in list any more — so
    # `prepare(state)` with no cfg raises and the endpoint answered 500. Verified
    # 2026-09-06 on production at e0316a6: /v1/explain 500s there too, i.e. this
    # is a PRE-EXISTING defect of the 3.0.0 rollout and not of the domain scope.
    # Fixed here because a diagnostic that cannot run cannot show the
    # deterministic prefix it exists for, and the same setting row is what the
    # cascade itself uses (executor.run_search), so the prefix shown is the
    # prefix that would really run.
    _cfg = get_active_setting(req.legal_entity)["config"]
    if req.params_override:
        state["_params_override"] = req.params_override
    state.update(prepare(state, _cfg))
    state.update(route(state))
    if not state.get("resolved"):
        state.update(qa_exact(state))
    if not state.get("resolved"):
        state.update(qa_lexical(state))
    state.pop("rag_chunks", None)
    # the query embedding is a 1536-float list nobody reads out of a diagnostic
    state.pop("query_vec", None)
    return state


# LAST, deliberately: the decorators above register on `v1`, and include_router
# copies what exists at the moment it runs. Called any earlier it would mount an
# empty — or half-built — router, and the routes would 404 with nothing to show
# for it in the code.
api.include_router(v1)
from .qa_admin import internal as _qa_admin_internal
api.include_router(_qa_admin_internal)
