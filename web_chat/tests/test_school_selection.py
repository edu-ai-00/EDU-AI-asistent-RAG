"""BFF rules for the school chosen in the browser (CHAT_ENTITY_PICKER on) and for fixed builds.

Run from web_chat/:  python -m pytest tests/test_school_selection.py -q
The tests set the module's settings themselves; no environment variable is needed.
"""
import builtins
import importlib.util
import pathlib
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

import web_chat
from web_chat import app, get_search_client

ENTITIES = {"entities": [
    {"legal_entity_code": "SKOLA_A", "display_name": "Základní škola A", "kind": "legal_person"},
    {"legal_entity_code": "SKOLA_B", "display_name": "Základní škola B", "kind": "legal_person"},
]}
ANSWER = {"answer": "Ahoj.", "outcome": "answer", "answer_source": "rag", "suggestions": [],
          "sources": [], "language": "cs", "duration_ms": 5,
          "effective_scope": {"legal_entity": "SKOLA_A"}}


def _resp(method: str, url: str, payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request(method, url))


class RoutedSearch:
    """Answers by URL and records every upstream call, so a test can prove what did NOT happen."""

    def __init__(self, entities=ENTITIES, entities_error: Exception | None = None):
        self.entities = entities
        self.entities_error = entities_error
        self.calls: list[tuple[str, str, dict | None]] = []

    def asked(self) -> list[dict]:
        return [c[2] for c in self.calls if c[0] == "POST" and c[1].endswith("/v1/ask")]

    def catalogs(self) -> list[dict]:
        return [c[2] for c in self.calls if c[1].endswith("/v1/domains")]

    def lists(self) -> list:
        return [c for c in self.calls if c[1].endswith("/v1/entities")]

    async def get(self, url: str, params: dict | None = None):
        self.calls.append(("GET", url, params))
        if url.endswith("/v1/entities"):
            if self.entities_error:
                raise self.entities_error
            return _resp("GET", url, self.entities)
        if url.endswith("/v1/domains"):
            return _resp("GET", url, {"legal_entity": (params or {}).get("legal_entity"),
                                      "domains": [{"code": "ATTENDANCE", "labels": {"cs": "Docházka"}}]})
        return _resp("GET", url, {}, 404)

    async def post(self, url: str, json: dict):
        self.calls.append(("POST", url, json))
        return _resp("POST", url, ANSWER)


@pytest.fixture
def chat(monkeypatch):
    """(TestClient, fake) factory: picker/default are arguments, nothing leaks between tests."""
    made = []

    def make(picker=True, default="", fake=None):
        monkeypatch.setattr(web_chat, "CHAT_ENTITY_PICKER", picker)
        monkeypatch.setattr(web_chat, "CHAT_LEGAL_ENTITY", default)
        fake = fake or RoutedSearch()
        app.dependency_overrides[get_search_client] = lambda: fake
        client = TestClient(app)
        made.append(client)
        return client, fake

    yield make
    app.dependency_overrides.clear()


def ask(client, entity=..., domains=None):
    body = {"message": "Kdy začíná školní rok?", "user_role": "zak",
            "conversation_id": str(uuid.uuid4())}
    if entity is not ...:
        body["legal_entity"] = entity
    if domains is not None:
        body["domains"] = domains
    return client.post("/chat/api/message", json=body)


# ── config: what the browser is told about readiness ─────────────────────────

def test_picker_without_default_awaits_a_selection_and_lists_names(chat):
    client, _ = chat()
    cfg = client.get("/chat/api/config").json()
    assert cfg["entity_picker"] is True
    assert cfg["setup"] == "awaiting_selection"
    assert cfg["default"] == ""
    assert cfg["entities"] == [{"code": "SKOLA_A", "name": "Základní škola A"},
                               {"code": "SKOLA_B", "name": "Základní škola B"}]


def test_a_valid_explicit_default_keeps_its_old_meaning(chat):
    client, _ = chat(default="SKOLA_B")
    cfg = client.get("/chat/api/config").json()
    assert (cfg["setup"], cfg["default"]) == ("ready", "SKOLA_B")
    # and a question without a claim is searched in that school
    client2, fake = chat(default="SKOLA_B")
    assert ask(client2).status_code == 200
    assert fake.asked()[0]["legal_entity"] == "SKOLA_B"


def test_an_inactive_default_is_not_offered_as_ready(chat):
    client, _ = chat(default="SKOLA_GONE")
    cfg = client.get("/chat/api/config").json()
    assert (cfg["setup"], cfg["default"]) == ("awaiting_selection", "")


def test_an_unreadable_list_is_its_own_state_not_an_empty_ready_one(chat):
    err = httpx.ConnectError("down")
    client, _ = chat(fake=RoutedSearch(entities_error=err))
    cfg = client.get("/chat/api/config").json()
    assert cfg["setup"] == "entities_unavailable"
    assert cfg["entities"] == []


def test_an_empty_list_is_not_a_finished_configuration(chat):
    client, _ = chat(fake=RoutedSearch(entities={"entities": []}))
    assert client.get("/chat/api/config").json()["setup"] == "no_entities"


@pytest.mark.parametrize("registry", [
    None, [], {}, {"entities": None}, {"entities": 0}, {"entities": 7},
    {"entities": "not a list"}, {"entities": {}}, {"entities": [None]},
    {"entities": [{}]}, {"entities": [{"legal_entity_code": []}]},
    {"entities": [{"legal_entity_code": {}}]},
    {"entities": [{"legal_entity_code": 123}]},
    {"entities": [{"legal_entity_code": "  "}]},
    {"entities": [{"legal_entity_code": " SKOLA_A"}]},
    {"entities": [{"legal_entity_code": "SKOLA_A", "display_name": []}]},
    {"entities": [ENTITIES["entities"][0], {}]},
    {"entities": [ENTITIES["entities"][0], ENTITIES["entities"][0]]},
], ids=["null", "array", "missing", "null-list", "zero", "integer", "string", "object",
        "null-row", "missing-code", "list-code", "object-code", "numeric-code", "blank-code",
        "padded-code", "list-name", "mixed-invalid", "duplicate-code"])
def test_malformed_registry_is_unavailable_and_never_searches(chat, registry):
    client, fake = chat(fake=RoutedSearch(entities=registry))
    config = client.get("/chat/api/config")
    assert config.status_code == 200
    assert config.json() == {"entity_picker": True, "entities": [], "default": "",
                             "setup": "entities_unavailable"}
    for response in (ask(client, "SKOLA_A"),
                     client.get("/chat/api/domains", params={"legal_entity": "SKOLA_A"})):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "entities_unavailable"
        assert response.json()["error"]["retryable"] is True
    assert len(fake.lists()) == 3
    assert fake.asked() == []
    assert fake.catalogs() == []


def test_registry_names_can_be_missing_without_changing_scope(chat):
    client, fake = chat(fake=RoutedSearch(entities={"entities": [
        {"legal_entity_code": "SKOLA_A"},
        {"legal_entity_code": "SKOLA_B", "display_name": None},
    ]}))
    assert client.get("/chat/api/config").json()["entities"] == [
        {"code": "SKOLA_A", "name": "SKOLA_A"},
        {"code": "SKOLA_B", "name": "SKOLA_B"},
    ]
    assert ask(client, "SKOLA_B").status_code == 200
    assert fake.asked()[0]["legal_entity"] == "SKOLA_B"


def test_fixed_build_reports_ready_or_unconfigured_and_never_lists_schools(chat):
    client, fake = chat(picker=False, default="SKOLA_A")
    cfg = client.get("/chat/api/config").json()
    assert cfg == {"entity_picker": False, "entities": [], "setup": "ready"}
    assert fake.lists() == []
    client, _ = chat(picker=False, default="")
    assert client.get("/chat/api/config").json()["setup"] == "unconfigured"


# ── message: the entity is validated by the server, not by the dropdown ──────

def test_no_school_is_refused_before_search(chat):
    client, fake = chat()
    for claim in (..., None, "", "   "):
        r = ask(client, claim)
        assert r.status_code == 422, claim
        assert r.json()["error"]["code"] == "school_required"
    assert fake.asked() == []


@pytest.mark.parametrize("claim", ["CHANGE_ME_legal_entity_code", "SKOLA_NEEXISTUJE",
                                   "skola_a", "SKOLA_A; DROP", "../.env"])
def test_placeholder_unknown_or_inactive_school_never_searches(chat, claim):
    client, fake = chat(default="SKOLA_B")   # a valid default must not become a fallback
    r = ask(client, claim)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_school"
    assert fake.asked() == []


def test_a_valid_school_is_searched_exactly_as_chosen(chat):
    client, fake = chat(default="SKOLA_B")
    assert ask(client, "SKOLA_A").status_code == 200
    assert [q["legal_entity"] for q in fake.asked()] == ["SKOLA_A"]


def test_a_school_deactivated_after_the_page_loaded_is_refused_on_the_next_question(chat):
    client, fake = chat()
    assert ask(client, "SKOLA_A").status_code == 200
    fake.entities = {"entities": [ENTITIES["entities"][1]]}    # SKOLA_A now inactive
    assert ask(client, "SKOLA_A").status_code == 422
    assert len(fake.asked()) == 1


def test_the_school_list_failing_blocks_the_question_retryably(chat):
    client, fake = chat(fake=RoutedSearch(entities_error=httpx.ConnectError("down")))
    r = ask(client, "SKOLA_A")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "entities_unavailable"
    assert r.json()["error"]["retryable"] is True
    assert fake.asked() == []


def test_a_placeholder_deployment_default_counts_as_unset():
    spec = importlib.util.spec_from_file_location(
        "web_chat_placeholder", pathlib.Path(web_chat.__file__))
    mod = importlib.util.module_from_spec(spec)
    import os
    old = os.environ.get("CHAT_LEGAL_ENTITY")
    os.environ["CHAT_LEGAL_ENTITY"] = "CHANGE_ME_legal_entity_code"
    try:
        spec.loader.exec_module(mod)
    finally:
        if old is None:
            del os.environ["CHAT_LEGAL_ENTITY"]
        else:
            os.environ["CHAT_LEGAL_ENTITY"] = old
    assert mod.CHAT_LEGAL_ENTITY == ""


def test_picker_off_ignores_a_spoofed_school_and_asks_no_list(chat):
    client, fake = chat(picker=False, default="SKOLA_A")
    assert ask(client, "SKOLA_B").status_code == 200
    assert [q["legal_entity"] for q in fake.asked()] == ["SKOLA_A"]
    assert fake.lists() == []
    assert client.get("/chat/api/domains", params={"legal_entity": "SKOLA_B"}).json()["legal_entity"] == "SKOLA_A"


def test_picker_off_without_a_school_is_a_controlled_error(chat):
    client, fake = chat(picker=False, default="")
    r = ask(client, "SKOLA_A")
    assert (r.status_code, r.json()["error"]["code"]) == (500, "chat_misconfigured")
    assert fake.asked() == []


# ── domains: the catalog is only ever fetched for a validated school ─────────

def test_catalog_is_not_fetched_without_or_for_an_invalid_school(chat):
    client, fake = chat()
    for params in ({}, {"legal_entity": "CHANGE_ME_x"}, {"legal_entity": "NOPE"}):
        r = client.get("/chat/api/domains", params=params)
        assert r.status_code == 422, params
    assert fake.catalogs() == []
    ok = client.get("/chat/api/domains", params={"legal_entity": "SKOLA_B"})
    assert ok.status_code == 200 and ok.json()["legal_entity"] == "SKOLA_B"
    assert [c["legal_entity"] for c in fake.catalogs()] == ["SKOLA_B"]


# ── the choice is a client value: nothing is ever written server-side ────────

def test_no_http_route_can_set_the_school_or_write_a_file(chat, monkeypatch):
    allowed = {("/chat/api/message", "POST")}
    for route in app.routes:
        for method in getattr(route, "methods", None) or ():
            if method in ("GET", "HEAD"):
                continue
            assert (route.path, method) in allowed, (route.path, method)

    real_open = builtins.open

    def guarded(file, mode="r", *a, **k):
        assert not any(c in mode for c in "wax+"), f"write attempt: {file!r}"
        return real_open(file, mode, *a, **k)

    client, _ = chat()
    monkeypatch.setattr(builtins, "open", guarded)
    for method in ("PUT", "POST", "PATCH", "DELETE"):
        r = client.request(method, "/chat/api/config", json={"legal_entity": "SKOLA_A"})
        assert r.status_code == 405, method
    ask(client, "SKOLA_A")
    ask(client, "../.env")
    # another visitor is unaffected by what the first one chose
    assert client.get("/chat/api/config").json()["default"] == ""
