"""CONTRACT.md §14.9 (v1.7.0): POST /schools/{school}/ask-preview — payload to
search, response shape, validation, policy/CSRF, error mapping, and that the
question and answer never reach a log."""
from __future__ import annotations

import logging
import uuid

import pytest
import requests
from fastapi.testclient import TestClient

from app.adapters import search as search_mod
from app.adapters.search import (HttpSearchClient, SearchBadRequest, SearchServerError,
                                 SearchUnavailable)
from app.api.deps import get_policy, get_principal, get_search_client
from app.config import settings
from app.main import app
from tests.fake_search import ASK_RESPONSE, FakeSearchClient
from tests.test_document_adapters import FakeResponse, Recorder
from tests.test_document_policy import OTHER, STAFF, RestrictedPolicy, TwoSchoolSearch

HEADERS = {"X-QA-Portal": "1"}
BASE = "/qa-api/v1/schools/ZS_DEMO"
URL = f"{BASE}/ask-preview"
QUESTION = "Tajna otazka o zvlastnim obsahu 7731"
BODY = {"question": QUESTION, "user_role": "PARENT", "user_school_grade": None,
        "retrieval_only": False}
RESPONSE_KEYS = {"answer", "outcome", "answer_source", "delivered_by", "sources",
                 "suggestions", "matched_entry_id", "match_score", "duration_ms",
                 "retrieval_only", "effective_scope", "strategies", "key_words_used",
                 "language", "answer_parts", "trace", "explain"}


@pytest.fixture
def search():
    return FakeSearchClient()


@pytest.fixture
def client(search):
    app.dependency_overrides[get_search_client] = lambda: search
    yield TestClient(app)
    app.dependency_overrides.clear()


def _post(client, **changes):
    return client.post(URL, headers=HEADERS, json={**BODY, **changes})


def _fields(resp) -> set[str]:
    return {f["field"] for f in resp.json()["error"].get("fields") or []}


# ── success ─────────────────────────────────────────────────────────────
def test_success_passes_the_search_fields_through(client, search):
    resp = _post(client)
    assert resp.status_code == 200
    out = resp.json()
    assert set(out) == RESPONSE_KEYS
    for key in ("answer", "outcome", "answer_source", "delivered_by", "sources",
                "suggestions", "matched_entry_id", "match_score", "duration_ms"):
        assert out[key] == ASK_RESPONSE[key]
    assert out["retrieval_only"] is False


def test_payload_sent_to_search(client, search):
    _post(client, question=f"  {QUESTION}  ", user_school_grade="3")
    (payload,) = search.ask_calls
    assert payload["question"] == QUESTION                      # stripped
    assert payload["legal_entity"] == "ZS_DEMO"                 # from the path
    assert payload["user_role"] == "PARENT"
    assert payload["user_school_grade"] == "3"
    assert payload["retrieval_only"] is False
    assert payload["user_id"] == search.get_uploader("ZS_DEMO")["user_id"]
    assert str(uuid.UUID(payload["query_id"])) == payload["query_id"]
    assert set(payload) == {"question", "legal_entity", "user_role", "user_school_grade",
                            "retrieval_only", "user_id", "query_id", "debug"}


def test_no_grade_in_payload_when_null_or_absent(client, search):
    _post(client)
    body = {k: v for k, v in BODY.items() if k != "user_school_grade"}
    client.post(URL, headers=HEADERS, json=body)
    assert len(search.ask_calls) == 2
    assert all("user_school_grade" not in p for p in search.ask_calls)


def test_each_request_gets_a_fresh_query_id(client, search):
    _post(client)
    _post(client)
    assert search.ask_calls[0]["query_id"] != search.ask_calls[1]["query_id"]


def test_retrieval_only_is_forwarded_and_echoed(client, search):
    search.ask_response = {"answer": "", "outcome": "answer", "answer_source": "rag",
                           "sources": [{"rank": 1, "title": "Rad", "url": None}]}
    resp = _post(client, retrieval_only=True)
    assert search.ask_calls[0]["retrieval_only"] is True
    out = resp.json()
    assert out["retrieval_only"] is True
    assert out["sources"][0]["title"] == "Rad"


def test_missing_search_keys_become_null_and_empty_lists(client, search):
    search.ask_response = {"answer": "x", "outcome": "no_answer", "answer_source": "none"}
    out = _post(client).json()
    assert set(out) == RESPONSE_KEYS
    assert out["sources"] == [] and out["suggestions"] == []
    assert out["matched_entry_id"] is None and out["match_score"] is None
    assert out["delivered_by"] is None and out["duration_ms"] is None


def test_role_and_grade_codes_are_sent_canonical(client, search):
    _post(client, user_role="parent")
    assert search.ask_calls[0]["user_role"] == "PARENT"


def test_question_of_exactly_2000_chars_is_accepted(client, search):
    assert _post(client, question="a" * 2000).status_code == 200


# ── validation ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("changes,field", [
    ({"question": ""}, "question"),
    ({"question": "   \n\t "}, "question"),
    ({"question": "a" * 2001}, "question"),
    ({"user_role": "ADMIN"}, "user_role"),
    ({"user_role": ""}, "user_role"),
    ({"user_school_grade": "99"}, "user_school_grade"),
    ({"user_school_grade": ""}, "user_school_grade"),
    ({"retrieval_only": "yes"}, "retrieval_only"),
    ({"user_role": None}, "user_role"),
])
def test_validation_errors_are_422_with_fields(client, search, changes, field):
    resp = _post(client, **changes)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert field in _fields(resp)
    assert search.ask_calls == []


def test_missing_required_fields_are_422(client, search):
    resp = client.post(URL, headers=HEADERS, json={"question": "q"})
    assert resp.status_code == 422 and "user_role" in _fields(resp)
    resp = client.post(URL, headers=HEADERS, json={"user_role": "PARENT"})
    assert resp.status_code == 422 and "question" in _fields(resp)
    assert search.ask_calls == []


def test_unknown_body_field_is_422(client, search):
    assert _post(client, legal_entity="ZS_OTHER").status_code == 422
    assert search.ask_calls == []


def test_all_violations_are_reported_together(client):
    resp = _post(client, question=" ", user_role="X", user_school_grade="99")
    assert _fields(resp) == {"question", "user_role", "user_school_grade"}


# ── CSRF and policy ─────────────────────────────────────────────────────
def test_missing_csrf_header_is_403(client, search):
    resp = client.post(URL, json=BODY)
    assert resp.status_code == 403
    assert search.ask_calls == []


@pytest.fixture
def restricted(tmp_path):
    two = TwoSchoolSearch()

    def make(policy):
        app.dependency_overrides[get_search_client] = lambda: two
        app.dependency_overrides[get_policy] = lambda: policy
        app.dependency_overrides[get_principal] = lambda: STAFF
        return TestClient(app)

    yield two, make
    app.dependency_overrides.clear()


def test_foreign_school_is_403_and_never_reaches_search(restricted):
    two, make = restricted
    resp = make(RestrictedPolicy()).post(f"/qa-api/v1/schools/{OTHER}/ask-preview",
                                         headers=HEADERS, json=BODY)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    assert two.ask_calls == []


def test_own_school_passes_the_policy(restricted):
    two, make = restricted
    assert make(RestrictedPolicy()).post(URL, headers=HEADERS, json=BODY).status_code == 200
    assert two.ask_calls[0]["legal_entity"] == "ZS_DEMO"


def test_publish_permission_is_required(restricted):
    two, make = restricted
    resp = make(RestrictedPolicy(frozenset({"publish"}))).post(URL, headers=HEADERS, json=BODY)
    assert resp.status_code == 403
    assert two.ask_calls == []


def test_unknown_school_is_404(client):
    resp = client.post("/qa-api/v1/schools/NOPE/ask-preview", headers=HEADERS, json=BODY)
    assert resp.status_code == 404


# ── upstream error mapping (§14.9, §14.8) ───────────────────────────────
def test_search_4xx_is_400_with_its_message(client, search):
    search.ask_error = SearchBadRequest({"error": {"code": "BAD_REQUEST",
                                                   "message": "question is blank"}})
    resp = _post(client)
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "BAD_REQUEST" and err["message"] == "question is blank"
    assert err["retryable"] is False


def test_search_422_detail_message_does_not_echo_the_input(client, search):
    search.ask_error = SearchBadRequest({"detail": [
        {"type": "string_too_long", "loc": ["body", "question"], "msg": "too long",
         "input": QUESTION}]})
    resp = _post(client)
    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == "question: too long"
    assert QUESTION not in resp.text


def test_search_4xx_with_a_plain_body_has_a_generic_message(client, search):
    search.ask_error = SearchBadRequest(f"raw text mentioning {QUESTION}")
    resp = _post(client)
    assert resp.status_code == 400 and QUESTION not in resp.text


def test_search_500_is_502_not_retryable(client, search):
    search.raise_server_error_on = "ask"
    resp = _post(client)
    assert resp.status_code == 502
    err = resp.json()["error"]
    assert err["code"] == "UPSTREAM_ERROR" and err["retryable"] is False
    assert "permission denied" not in resp.text


def test_search_unavailable_is_503_retryable(client, search):
    search.raise_unavailable_on = "ask"
    resp = _post(client)
    assert resp.status_code == 503
    err = resp.json()["error"]
    assert err["code"] == "UNAVAILABLE" and err["retryable"] is True


def test_code_list_outage_is_503_before_asking(client, search):
    search.raise_unavailable_on = "get_code_lists"
    assert _post(client).status_code == 503
    assert search.ask_calls == []


def test_code_list_500_is_502(client, search):
    search.raise_server_error_on = "get_code_lists"
    assert _post(client).status_code == 502
    assert search.ask_calls == []


# ── nothing about the question or answer is logged ──────────────────────
@pytest.mark.parametrize("failure", [None, "bad_request", "server", "unavailable"])
def test_question_and_answer_are_never_logged(client, search, caplog, failure):
    search.ask_response = {**ASK_RESPONSE, "answer": "Tajna odpoved 5519"}
    if failure == "bad_request":
        search.ask_error = SearchBadRequest({"detail": f"bad {QUESTION}"})
    elif failure == "server":
        search.ask_error = SearchServerError(f"500: {QUESTION}")
    elif failure == "unavailable":
        search.ask_error = SearchUnavailable(f"timeout for {QUESTION}")
    with caplog.at_level(logging.DEBUG):
        _post(client)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Tajna otazka" not in text and "7731" not in text
    assert "Tajna odpoved" not in text and "5519" not in text
    if failure is None:
        assert "school=ZS_DEMO" in text and "outcome=answer" in text


def test_log_line_carries_the_query_id_sent_to_search(client, search, caplog):
    with caplog.at_level(logging.INFO, logger="qa_portal.ask"):
        _post(client)
    assert search.ask_calls[0]["query_id"] in caplog.text


# ── HTTP adapter ────────────────────────────────────────────────────────
def _http(monkeypatch, response=None, exc=None):
    rec = Recorder(response, exc)
    monkeypatch.setattr(search_mod.requests, "post", rec)
    return HttpSearchClient("http://s/v1", "http://s"), rec


def test_adapter_posts_to_v1_ask_with_the_long_timeout(monkeypatch):
    http, rec = _http(monkeypatch, FakeResponse(200, {"answer": "a", "outcome": "answer"}))
    assert http.ask({"question": "q"}) == {"answer": "a", "outcome": "answer"}
    call = rec.calls[0]
    assert call["args"] == ("http://s/v1/ask",)
    assert call["json"] == {"question": "q"}
    assert call["timeout"] == settings.ask_timeout_seconds == 90
    assert call["timeout"] > settings.http_timeout_seconds


def test_adapter_ask_timeout_can_be_overridden(monkeypatch):
    rec = Recorder(FakeResponse(200, {}))
    monkeypatch.setattr(search_mod.requests, "post", rec)
    HttpSearchClient("http://s/v1", "http://s", ask_timeout=5).ask({})
    assert rec.calls[0]["timeout"] == 5


@pytest.mark.parametrize("status,body,expected", [
    (400, {"error": {"message": "blank"}}, {"error": {"message": "blank"}}),
    (422, {"detail": "x"}, {"detail": "x"}),
    (404, None, "nope"),
])
def test_adapter_4xx_raises_bad_request_with_the_body(monkeypatch, status, body, expected):
    http, _ = _http(monkeypatch, FakeResponse(status, body, text="nope"))
    with pytest.raises(SearchBadRequest) as e:
        http.ask({})
    assert e.value.body == expected


def test_adapter_500_is_server_error(monkeypatch):
    http, _ = _http(monkeypatch, FakeResponse(500, None, text="boom"))
    with pytest.raises(SearchServerError):
        http.ask({})


@pytest.mark.parametrize("status", [502, 503, 504])
def test_adapter_gateway_errors_are_unavailable_not_server_error(monkeypatch, status):
    http, _ = _http(monkeypatch, FakeResponse(status, None, text="gw"))
    with pytest.raises(SearchUnavailable) as e:
        http.ask({})
    assert type(e.value) is SearchUnavailable


def test_adapter_network_error_is_unavailable(monkeypatch):
    http, _ = _http(monkeypatch, exc=requests.ConnectionError("down"))
    with pytest.raises(SearchUnavailable):
        http.ask({})


def test_adapter_non_json_success_is_server_error(monkeypatch):
    http, _ = _http(monkeypatch, FakeResponse(200, None, text="<html>"))
    with pytest.raises(SearchServerError):
        http.ask({})
