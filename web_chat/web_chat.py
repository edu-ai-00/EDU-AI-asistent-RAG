# last change in code by Jan Drozd and Martin Safranek (2026-08-18).
"""Small FastAPI BFF and static host for the VPN-only edu_ai web chat."""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal, get_args

import httpx
from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator


logger = logging.getLogger("edu_ai.web_chat")

ROOT_DIR = Path(__file__).resolve().parent
STATIC_DIR = ROOT_DIR / "static"
SEARCH_BASE_URL = os.getenv("SEARCH_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
SEARCH_TIMEOUT_SECONDS = float(os.getenv("CHAT_REQUEST_TIMEOUT_SECONDS", "125"))
SESSION_COOKIE = "edu_ai_chat_session"
COOKIE_SECURE = os.getenv("CHAT_COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"}
# WHOSE documents this chat searches. /search requires an entity and always has;
# the chat never sent one, which is the whole reason every message came back as
# "Vyhledávání není dostupné" (a 422 the BFF reported as an outage).
#
# A single default because the entity is NOT the client's to choose: the intended
# resolution is origin -> entity via legal_entity.domains, which this BFF cannot
# do (it has no database, only HTTP to search). Until that exists the deployment
# names one entity here, and CHAT_ENTITY_PICKER lets a TEST build override it per
# request from the dropdown. Off by default — with it on, any caller reads any
# organisation whose code they can name.
#
# A .env.example placeholder (CHANGE_ME_*) is "not set", not a school name: it
# reaches the container verbatim through Compose and must never go upstream.
PLACEHOLDER_PREFIX = "CHANGE_ME"


def _is_placeholder(value: str | None) -> bool:
    return not value or value.strip().startswith(PLACEHOLDER_PREFIX)


CHAT_LEGAL_ENTITY = os.getenv("CHAT_LEGAL_ENTITY", "").strip()
if _is_placeholder(CHAT_LEGAL_ENTITY):
    CHAT_LEGAL_ENTITY = ""
CHAT_ENTITY_PICKER = os.getenv("CHAT_ENTITY_PICKER", "false").lower() in {"1", "true", "yes", "on"}


class ChatError(Exception):
    """An error the client renders. `fields` is what makes one FIXABLE.

    Added 2026-09-06 with the area filter, because that is the first request
    field a user can get wrong by clicking: a stale code left in a picker after
    the school's catalog changed. Search answers such a body with a 422 naming
    the offending element, and this service used to flatten every upstream 4xx
    into one 500 ("chyba na naší straně, opakování nepomůže") — true for a
    malformed body the BFF built, and exactly wrong for a choice the user can
    correct. `retryable` travels for the same reason: a 503 from the catalog
    lookup is worth retrying and a 422 never is.
    """

    def __init__(self, status_code: int, code: str, message: str,
                 fields: list[dict] | None = None,
                 retryable: bool = False) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.fields = fields or []
        self.retryable = retryable
        super().__init__(message)


UserRole = Literal["zak", "ucitel", "rodic"]

# This channel speaks Czech role slugs; /search validates against code_list('role')
# (init 127) and knows none of them, so sending one through unmapped short-circuits
# every question with outcome='role_required' — which is what shipped on 2026-08-11
# and made the chat answer "Neznáme tvoji roli" with a role selected in the UI.
# Translated here rather than in the frontend: the browser persists its choice in
# localStorage, and changing the stored vocabulary would silently discard it.
SEARCH_ROLE = {"zak": "STUDENT", "ucitel": "TEACHER", "rodic": "PARENT"}


class ChatRequest(BaseModel):
    # 2000 is now also /v1/ask's own cap on `question`, and this value is passed
    # through verbatim — keep the two equal, or this service starts rejecting
    # what search would have answered (or forwarding what it will refuse).
    message: str = Field(min_length=1, max_length=2_000)
    school_grade: int | None = Field(default=None, ge=1, le=13)
    user_role: UserRole | None = None
    conversation_id: uuid.UUID | None = None
    # honoured only when CHAT_ENTITY_PICKER is on; ignored otherwise so a build
    # without the picker cannot have its entity chosen by the caller
    legal_entity: str | None = Field(default=None, max_length=100)
    # THEMATIC AREAS — DOMAIN codes from /chat/api/domains. null / absent = every
    # available area, which is what every existing client sends.
    #
    # STRUCTURAL validation only: a list of short non-empty strings. WHICH codes
    # exist is per-school data, and search is the authority for it (it owns the
    # catalog and answers 422 with `fields`). A second copy of the membership
    # rule here would be a list to keep in step with a database — the failure
    # this file has already had once, with the Czech role slugs.
    #
    # The cap is a shape guard, not a policy: 32 codes is far past any real
    # catalog (11 in production), and it stops an unbounded array from becoming
    # an unbounded upstream query string.
    domains: list[str] | None = Field(default=None, max_length=32)

    @field_validator("domains")
    @classmethod
    def validate_domains(cls, value):
        """Trim, drop nothing silently, and refuse the shapes search refuses.

        The same three rejections search makes (empty array, empty element,
        over-long element) so the user gets one legible error from whichever
        service notices first — and so the BFF cannot forward a body it knows
        search will reject.
        """
        if value is None:
            return None
        if not value:
            raise ValueError("Vyberte alespoň jednu oblast, nebo zvolte všechny oblasti.")
        out = []
        for item in value:
            code = (item or "").strip()
            if not code or len(code) > 100:
                raise ValueError("Neplatný kód oblasti.")
            out.append(code)
        return out

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Zadejte prosím otázku.")
        return value


class SuggestionOption(BaseModel):
    label: str
    question: str


class Suggestion(BaseModel):
    # kind rozhoduje o renderu: každý druh je tlačítko posílající question,
    # jediná výjimka je clarify — jeho question je výzva, posílatelný text
    # nesou options. Neznámý kind padá na qa: kontrakt ručí, že question
    # jde poslat rovnou zpět na /v1/ask.
    kind: str = "qa"
    entry_id: str = ""
    question: str
    options: list[SuggestionOption] = []


class Source(BaseModel):
    title: str
    page: int | None = None
    url: str | None = None


# role_required belongs here: /search answers it with a 200 carrying the localized
# "pick your role" prompt (see server _role_gate), so coercing it away printed
# "Odpověď nebyla nalezena" above that prompt. The allowlist in send_message is
# derived from this alias, so the two cannot drift apart again.
Outcome = Literal["answer", "suggest", "blocked", "self_harm", "unusable",
                  "no_answer", "role_required", "llm_unavailable"]


class EffectiveScope(BaseModel):
    """What search ACTUALLY ran with, passed through so the UI can show it.

    A subset of search's own effective_scope: the browser has no use for
    `identified`, and the internal taxonomy ids never leave the search service.
    `domains` is the canonical code set search validated — the UI compares it
    with the picker's selection, which is how a client detects that its stored
    choice no longer matches what it asked for.
    """
    legal_entity: str | None = None
    role: str | None = None
    school_grade: str | None = None
    language: str | None = None
    domains: list[str] | None = None


class ChatResponse(BaseModel):
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    answer: str
    outcome: Outcome
    answer_source: str
    suggestions: list[Suggestion]
    sources: list[Source]
    language: str
    duration_ms: int
    # additive: an older frontend build ignores it
    effective_scope: EffectiveScope | None = None


def _map_scope(raw: Any) -> EffectiveScope | None:
    """search's effective_scope -> the browser's. None for anything unusable.

    Only the five fields the UI needs; `identified` and the internal ids stay on
    the server side. `domains` comes through EXACTLY as search validated it,
    because the UI's whole use for it is to compare it with what it selected.
    """
    if not isinstance(raw, dict):
        return None
    doms = raw.get("domains")
    return EffectiveScope(
        legal_entity=raw.get("legal_entity"),
        role=raw.get("role"),
        school_grade=raw.get("school_grade"),
        language=raw.get("language"),
        domains=[str(d) for d in doms] if isinstance(doms, list) else None,
    )


def map_sources(raw_sources: Any) -> list[Source]:
    if isinstance(raw_sources, str):
        try:
            raw_sources = json.loads(raw_sources)
        except (json.JSONDecodeError, TypeError):
            return []

    if not isinstance(raw_sources, list):
        return []

    mapped: list[Source] = []
    for item in raw_sources:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("file_name") or item.get("document_title")
        if not isinstance(title, str) or not title.strip():
            continue
        page = item.get("page")
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            page = None
        url = item.get("url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            url = None
        mapped.append(Source(title=title.strip(), page=page, url=url))
    return mapped


def map_options(raw_options: Any) -> list[SuggestionOption]:
    if not isinstance(raw_options, list):
        return []
    mapped: list[SuggestionOption] = []
    for item in raw_options:
        if not isinstance(item, dict):
            continue
        label, question = item.get("label"), item.get("question")
        if (isinstance(label, str) and label.strip()
                and isinstance(question, str) and question.strip()):
            mapped.append(SuggestionOption(label=label.strip(), question=question.strip()))
    return mapped


def map_suggestions(raw_suggestions: Any) -> list[Suggestion]:
    if not isinstance(raw_suggestions, list):
        return []
    mapped: list[Suggestion] = []
    for item in raw_suggestions:
        if not isinstance(item, dict):
            continue
        question = item.get("question")
        if isinstance(question, str) and question.strip():
            kind = item.get("kind")
            options = map_options(item.get("options"))
            # clarify bez použitelných options je slepá ulička: jeho question
            # poslat nelze a tlačítko by nebylo z čeho udělat.
            if kind == "clarify" and not options:
                continue
            mapped.append(
                Suggestion(
                    kind=kind if isinstance(kind, str) and kind.strip() else "qa",
                    entry_id=str(item.get("entry_id") or ""),
                    question=question.strip(),
                    options=options,
                )
            )
    return mapped


@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = httpx.Timeout(SEARCH_TIMEOUT_SECONDS, connect=5.0)
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        app.state.search_client = client
        yield


app = FastAPI(
    title="edu_ai Web Chat",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

app.mount(
    "/chat/assets",
    StaticFiles(directory=STATIC_DIR / "assets", check_dir=False),
    name="chat-assets",
)


async def get_search_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.search_client


@app.exception_handler(ChatError)
async def chat_error_handler(_request: Request, exc: ChatError) -> JSONResponse:
    # `fields` and `retryable` are ADDITIVE: absent-as-empty for every error that
    # has no field to point at, so a client written against the old shape is
    # unaffected.
    body: dict[str, Any] = {"code": exc.code, "message": exc.message,
                            "retryable": exc.retryable}
    if exc.fields:
        body["fields"] = exc.fields
    return JSONResponse(status_code=exc.status_code, content={"error": body})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request,
                                   exc: RequestValidationError) -> JSONResponse:
    """The BFF's OWN structural errors, in the same fixable shape as search's.

    Was one 400 and one sentence about "otázku a ročník" for every rejected
    field. That sentence is now wrong as often as it is right: an area code can
    fail this validator too, and telling the user to check their question and
    year sends them to fix something that is fine. So: 422 like search (the same
    status for the same class of problem, which is what a client switches on),
    plus `fields` naming what was rejected.

    The field NAME is passed through and the REASON is not: pydantic's own text
    is English and internal ("String should have at most 32 items"), and the
    place that knows what a field means in Czech is the validator on the model.
    Whichever message it raised reaches the client through `msg`.
    """
    fields: list[dict] = []
    for err in exc.errors():
        # loc is ("body", "domains", 0) — drop the "body" frame, keep the path
        path = [str(p) for p in (err.get("loc") or []) if str(p) != "body"]
        fields.append({"field": ".".join(path) or "body",
                       "message": str(err.get("msg") or "")})
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "invalid_request",
                           "message": "Zkontrolujte prosím zadání.",
                           "retryable": False,
                           "fields": fields}},
    )


@app.get("/chat/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "edu_ai_web_chat"}


async def _active_entities(client: httpx.AsyncClient) -> list[dict[str, str]]:
    """The active schools, straight from search (`/v1/entities`), never cached.

    Raises ChatError(503) when the list cannot be read. Not cached on purpose: an
    organisation deactivated a minute ago must stop being searchable now.
    """
    try:
        upstream = await client.get(f"{SEARCH_BASE_URL}/v1/entities")
        upstream.raise_for_status()
        body = upstream.json()
        if not isinstance(body, dict) or not isinstance(body.get("entities"), list):
            raise ValueError("invalid entity registry shape")
        entities = []
        seen = set()
        for row in body["entities"]:
            if not isinstance(row, dict):
                raise ValueError("invalid entity registry row")
            code = row.get("legal_entity_code")
            name = row.get("display_name")
            if (not isinstance(code, str) or not code.strip()
                    or code != code.strip() or code in seen):
                raise ValueError("invalid entity registry code")
            if name is not None and not isinstance(name, str):
                raise ValueError("invalid entity registry name")
            seen.add(code)
            entities.append({"code": code, "name": name or code})
        return entities
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        logger.warning("chat: entity list unavailable (%s)", type(exc).__name__)
        raise ChatError(503, "entities_unavailable",
                        "Seznam škol se teď nepodařilo načíst. Zkuste to prosím znovu.",
                        retryable=True) from exc


@app.get("/chat/api/config")
async def chat_config(
    client: Annotated[httpx.AsyncClient, Depends(get_search_client)],
) -> dict[str, Any]:
    """What the UI may offer, and whether the chat may be used yet.

    `setup` is the readiness the browser acts on (additive, no stored flag — it is
    derived from this process's environment and search's list on every call):
      ready                 a school is determined (fixed, or a valid default)
      awaiting_selection    picker on, no usable default — the user must choose
      entities_unavailable  picker on, the list could not be read (retryable)
      no_entities           picker on, search knows no active school
      unconfigured          picker off and no fixed school: nothing can be searched

    With the picker off the response lists no schools, so the dropdown cannot
    appear in a build that would ignore it, and nothing here is checked upstream.
    The `default` of a picker build is returned only when it is an ACTIVE school.
    """
    if not CHAT_ENTITY_PICKER:
        return {"entity_picker": False, "entities": [],
                "setup": "ready" if CHAT_LEGAL_ENTITY else "unconfigured"}
    try:
        entities = await _active_entities(client)
    except ChatError:
        return {"entity_picker": True, "entities": [], "default": "",
                "setup": "entities_unavailable"}
    if not entities:
        return {"entity_picker": True, "entities": [], "default": "",
                "setup": "no_entities"}
    default = CHAT_LEGAL_ENTITY if CHAT_LEGAL_ENTITY in {e["code"] for e in entities} else ""
    return {"entity_picker": True, "entities": entities, "default": default,
            "setup": "ready" if default else "awaiting_selection"}


def _upstream_fields(response: httpx.Response) -> list[dict]:
    """search's `error.fields`, defensively — [] for anything unexpected.

    The envelope is a contract (contracts/errors.yaml), but a proxy or a future
    version could hand back something else, and a KeyError here would turn a
    fixable 422 into a 500. Only the two keys the UI renders are kept, so an
    upstream message cannot smuggle extra structure into this response.
    """
    try:
        body = response.json()
        fields = ((body or {}).get("error") or {}).get("fields") or []
        return [{"field": str(f.get("field") or ""),
                 "message": str(f.get("message") or "")}
                for f in fields if isinstance(f, dict)]
    except (ValueError, AttributeError, TypeError):
        return []


async def _effective_entity(client: httpx.AsyncClient, claimed: str | None) -> str:
    """WHOSE school this request searches — the one rule, in one place.

    With CHAT_ENTITY_PICKER off the caller's `legal_entity` is IGNORED, not
    merely defaulted: a build without the picker must not have its entity chosen
    by whoever can reach the chat. /chat/api/domains needs the identical answer,
    because a catalog fetched for one school and a search run in another is the
    bug spec §7 names outright ("nemůže načíst katalog jedné školy a potom hledat
    ve druhé") — and it would show the user areas that then 422.

    With the picker ON the claim is a client scope, not a permission: it must be
    an ACTIVE school in search's own list, checked here on every call. No claim
    falls back only to an explicit, valid deployment default; otherwise it is
    refused BEFORE any search upstream — never the first school, never a
    placeholder, never a different school than the one named.
    """
    if not CHAT_ENTITY_PICKER:
        if not CHAT_LEGAL_ENTITY:
            # Refused here rather than sent on: search would answer 422, which reads
            # as "the search is broken" when it is this service that is unconfigured.
            logger.error("chat: no school is configured and the picker is off")
            raise ChatError(500, "chat_misconfigured",
                            "Chat není nastavený — chybí škola, ve které se má hledat.")
        return CHAT_LEGAL_ENTITY

    entity = (claimed or "").strip() or CHAT_LEGAL_ENTITY
    if not entity:
        raise ChatError(422, "school_required",
                        "Nejdřív vyberte školu.",
                        fields=[{"field": "legal_entity",
                                 "message": "Škola není vybraná."}])
    active = {e["code"] for e in await _active_entities(client)}
    if _is_placeholder(entity) or entity not in active:
        raise ChatError(422, "invalid_school",
                        "Vybraná škola není dostupná. Vyberte prosím školu znovu.",
                        fields=[{"field": "legal_entity",
                                 "message": "Neznámá nebo neaktivní škola."}])
    return entity


@app.get("/chat/api/domains")
async def chat_domains(
    client: Annotated[httpx.AsyncClient, Depends(get_search_client)],
    legal_entity: str | None = None,
) -> dict[str, Any]:
    """The area picker's catalog for the EFFECTIVE school.

    Returns `legal_entity` as well as `domains`, and that is not decoration: with
    the picker off, a spoofed `?legal_entity=` is ignored and the response says
    which school the codes actually belong to, so the UI can key its stored
    selection by the entity it will really search in.

    Codes AND translated labels come from here. The frontend holds no copy of
    either — the catalog is per-school data (11 codes in production, a different
    set per school), and a hard-coded list in the browser could only drift from
    the database that validates it.

    A catalog that cannot be read is a 503 with retryable=true, NOT an empty
    list: an empty list would render as "this school has no areas" and quietly
    remove the filter, which is the silent reset spec §7 forbids.
    """
    entity = await _effective_entity(client, legal_entity)
    try:
        upstream = await client.get(f"{SEARCH_BASE_URL}/v1/domains",
                                    params={"legal_entity": entity})
        upstream.raise_for_status()
        data = upstream.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("chat: domain catalog unavailable (%s)", type(exc).__name__)
        raise ChatError(503, "domains_unavailable",
                        "Seznam oblastí se teď nepodařilo načíst. "
                        "Zkuste to prosím znovu.", retryable=True) from exc
    rows = data.get("domains") or []
    return {
        # the entity SEARCH resolved the catalog for, echoed back
        "legal_entity": data.get("legal_entity") or entity,
        "entity_picker": CHAT_ENTITY_PICKER,
        "domains": [{"code": r.get("code"), "labels": r.get("labels") or {}}
                    for r in rows if r.get("code")],
    }


@app.post("/chat/api/message", response_model=ChatResponse)
async def send_message(
    payload: ChatRequest,
    request: Request,
    response: Response,
    client: Annotated[httpx.AsyncClient, Depends(get_search_client)],
) -> ChatResponse:
    session_id = request.cookies.get(SESSION_COOKIE)
    try:
        session_uuid = uuid.UUID(session_id) if session_id else uuid.uuid4()
    except ValueError:
        session_uuid = uuid.uuid4()

    if session_id != str(session_uuid):
        response.set_cookie(
            SESSION_COOKIE,
            str(session_uuid),
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="lax",
            path="/chat/",
        )

    conversation_id = payload.conversation_id or uuid.uuid4()
    entity = await _effective_entity(client, payload.legal_entity)

    upstream_payload = {
        "question": payload.message,
        "session_id": str(session_uuid),
        "conversation_id": str(conversation_id),
        "legal_entity": entity,
        # No user identity in this channel. /search accepts that when the platform
        # policy allows it (settings visibility.search_requires_user, init 99);
        # if it does not, the answer is a 403 and the fix is a real identity, not
        # a made-up id here.
        "user_id": None,
        "user_school_grade": payload.school_grade,
        # KeyError is impossible — UserRole is a Literal over exactly these keys —
        # but a role added to the Literal and not to the map must break loudly here
        # rather than reach search as an unknown code and read as "no answer".
        "user_role": SEARCH_ROLE[payload.user_role] if payload.user_role else None,
    }
    # OMITTED when there is no filter, not sent as null. Search treats the two
    # identically, but omitting it keeps the body byte-identical to what this
    # service sent before the feature existed — which is what keeps an
    # Idempotency-Key digest taken by the old code replaying (see
    # server._hashable_body). This channel sends no key today; the rule is here
    # so it stays true if it starts to.
    if payload.domains is not None:
        upstream_payload["domains"] = payload.domains

    try:
        upstream = await client.post(f"{SEARCH_BASE_URL}/v1/ask", json=upstream_payload)
    except httpx.TimeoutException as exc:
        raise ChatError(
            504,
            "search_timeout",
            "Odpověď nyní trvá příliš dlouho. Zkuste to prosím znovu.",
        ) from exc
    except httpx.HTTPError as exc:
        logger.warning("Search service connection failed: %s", type(exc).__name__)
        raise ChatError(
            502,
            "search_unavailable",
            "Vyhledávání nyní není dostupné. Zkuste to prosím později.",
        ) from exc

    # 4xx and 5xx mean opposite things and used to share one message. A 4xx is a
    # request THIS service built wrong — deterministic, so "zkuste to později"
    # invites a retry that cannot ever succeed, and it sent everyone hunting for
    # an outage that was not happening. 5xx and timeouts are the real outage.
    if 400 <= upstream.status_code < 500:
        # NOT the whole request body in the log — a question is user content and
        # belongs only in the query log. The status and the error envelope are
        # what a report needs.
        logger.error("chat: search rejected our request — HTTP %s",
                     upstream.status_code)
        if upstream.status_code == 403:
            # the one 4xx a user can be told something useful about: the platform
            # wants a verified caller and this channel has none
            raise ChatError(403, "search_forbidden",
                            "Tento chat nemá oprávnění hledat v podkladech této školy.")
        if upstream.status_code == 422:
            # A FIXABLE ERROR, passed through as one. The user's own choice can
            # produce it — a stale area code left in the picker after the
            # school's catalog changed — and flattening it into the old blanket
            # 500 told them "chyba na naší straně, opakování nepomůže" about
            # something they can correct in one click. `fields` names which
            # element, so the UI can mark the picker rather than the message.
            _fields = _upstream_fields(upstream)
            raise ChatError(422, "invalid_scope",
                            "Vybraná oblast už pro tuto školu neexistuje. "
                            "Zvolte prosím oblast znovu.", fields=_fields)
        raise ChatError(500, "search_rejected",
                        "V dotazu je chyba na naší straně. Opakování nepomůže — "
                        "nahlaste to prosím.")
    if upstream.status_code == 503:
        # the catalog could not be read, so the NARROWER search the user asked
        # for could not be applied. Retryable, and deliberately NOT retried here
        # without the filter: a wider answer is a different answer.
        logger.warning("Search service reported 503 — scope could not be applied")
        raise ChatError(503, "scope_unavailable",
                        "Vyhledávání ve vybrané oblasti teď není dostupné. "
                        "Zkuste to prosím znovu.", retryable=True)
    if upstream.status_code >= 500:
        logger.warning("Search service returned HTTP %s", upstream.status_code)
        raise ChatError(
            502,
            "search_unavailable",
            "Vyhledávání nyní není dostupné. Zkuste to prosím později.",
            retryable=True,
        )

    try:
        data = upstream.json()
    except ValueError as exc:
        raise ChatError(502, "invalid_search_response", "Search služba vrátila neplatnou odpověď.") from exc
    if not isinstance(data, dict):
        raise ChatError(502, "invalid_search_response", "Search služba vrátila neplatnou odpověď.")

    outcome = data.get("outcome", "no_answer")
    if outcome not in get_args(Outcome):
        outcome = "no_answer"

    try:
        duration_ms = max(0, int(data.get("duration_ms") or 0))
    except (TypeError, ValueError):
        duration_ms = 0

    return ChatResponse(
        message_id=uuid.uuid4(),
        conversation_id=conversation_id,
        answer=str(data.get("answer") or ""),
        outcome=outcome,
        answer_source=str(data.get("answer_source") or "none"),
        suggestions=map_suggestions(data.get("suggestions")),
        sources=map_sources(data.get("sources")),
        language=str(data.get("language") or "cs"),
        duration_ms=duration_ms,
        effective_scope=_map_scope(data.get("effective_scope")),
    )


@app.get("/chat/")
@app.get("/chat/{path:path}")
async def chat_spa(path: str = "") -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.is_file():
        raise ChatError(503, "frontend_not_built", "Frontend zatím není sestavený.")
    return FileResponse(index_path)
