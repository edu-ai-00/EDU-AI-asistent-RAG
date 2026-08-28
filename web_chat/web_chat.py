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
CHAT_LEGAL_ENTITY = os.getenv("CHAT_LEGAL_ENTITY", "").strip()
CHAT_ENTITY_PICKER = os.getenv("CHAT_ENTITY_PICKER", "false").lower() in {"1", "true", "yes", "on"}


class ChatError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
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
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": {"code": "invalid_message", "message": "Zkontrolujte zadanou otázku a ročník."}},
    )


@app.get("/chat/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "edu_ai_web_chat"}


@app.get("/chat/api/config")
async def chat_config(
    client: Annotated[httpx.AsyncClient, Depends(get_search_client)],
) -> dict[str, Any]:
    """What the UI may offer: the fixed entity, or the test picker's options.

    Only reachable list, never a rule of its own — with the picker off this
    returns no entities at all, so the dropdown cannot appear in a build that
    would ignore it anyway.
    """
    if not CHAT_ENTITY_PICKER:
        return {"entity_picker": False, "entities": []}
    try:
        upstream = await client.get(f"{SEARCH_BASE_URL}/v1/entities")
        upstream.raise_for_status()
        rows = upstream.json().get("entities") or []
    except (httpx.HTTPError, ValueError) as exc:
        # the chat still works on CHAT_LEGAL_ENTITY — no list is not a failure
        logger.warning("chat: entity list unavailable (%s)", type(exc).__name__)
        return {"entity_picker": True, "entities": [], "default": CHAT_LEGAL_ENTITY}
    return {
        "entity_picker": True,
        "default": CHAT_LEGAL_ENTITY,
        "entities": [{"code": r.get("legal_entity_code"),
                      "name": r.get("display_name")} for r in rows
                     if r.get("legal_entity_code")],
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
    entity = (payload.legal_entity if CHAT_ENTITY_PICKER else None) or CHAT_LEGAL_ENTITY
    if not entity:
        # Refused here rather than sent on: search would answer 422, which reads
        # as "the search is broken" when it is this service that is unconfigured.
        logger.error("chat: CHAT_LEGAL_ENTITY is not set — nothing can be searched")
        raise ChatError(500, "chat_misconfigured",
                        "Chat není nastavený — chybí škola, ve které se má hledat.")

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
        logger.error("chat: search rejected our request — HTTP %s: %s",
                     upstream.status_code, upstream.text[:300])
        if upstream.status_code == 403:
            # the one 4xx a user can be told something useful about: the platform
            # wants a verified caller and this channel has none
            raise ChatError(403, "search_forbidden",
                            "Tento chat nemá oprávnění hledat v podkladech této školy.")
        raise ChatError(500, "search_rejected",
                        "V dotazu je chyba na naší straně. Opakování nepomůže — "
                        "nahlaste to prosím.")
    if upstream.status_code >= 500:
        logger.warning("Search service returned HTTP %s", upstream.status_code)
        raise ChatError(
            502,
            "search_unavailable",
            "Vyhledávání nyní není dostupné. Zkuste to prosím později.",
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
    )


@app.get("/chat/")
@app.get("/chat/{path:path}")
async def chat_spa(path: str = "") -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.is_file():
        raise ChatError(503, "frontend_not_built", "Frontend zatím není sestavený.")
    return FileResponse(index_path)
