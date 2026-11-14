"""CONTRACT.md §14.11 (v1.8.0): the debug fields of POST ask-preview, the explain
call beside it, and GET entries/by-id."""
from __future__ import annotations

import copy
import logging

import pytest
from fastapi.testclient import TestClient

from app.adapters.search import (HttpSearchClient, SearchBadRequest, SearchNotFound,
                                 SearchServerError, SearchUnavailable)
from app.api.deps import get_policy, get_principal, get_search_client
from app.main import app
from tests.fake_search import ENTRY_BY_ID, ENTRY_ID, EXPLAIN_RESPONSE, FakeSearchClient
from tests.test_document_adapters import FakeResponse, Recorder
from tests.test_document_policy import OTHER, STAFF, RestrictedPolicy, TwoSchoolSearch

HEADERS = {"X-QA-Portal": "1"}
BASE = "/qa-api/v1/schools/ZS_DEMO"
URL = f"{BASE}/ask-preview"
QUESTION = "Tajna otazka o debug obsahu 5519"
BODY = {"question": QUESTION, "user_role": "PARENT", "retrieval_only": True}
SECRET = "ZVLASTNI-OBSAH-TRASY-8842"

DEBUG_ASK = {
    "answer": None, "outcome": "suggest", "answer_source": "none",
    "effective_scope": {"legal_entity": "ZS_DEMO", "role": "PARENT", "school_grade": None,
                        "language": "cs", "domains": ["PROVOZ"], "identified": True},
    "strategies": ["qa_exact", "qa_lexical"], "key_words_used": ["rok", "zacina"],
    "language": "cs", "answer_parts": [{"kind": "qa", "text": SECRET}],
    "trace": [{"step": "qa_exact", "status": "ran", "ms": 3, "pool": 0, "pool_added": 0,
               "added_chunk_ids": [], "matched_entry_id": None, "resolved": None,
               "outcome": None, "decision": "miss", "match_score": None,
               "stopped_here": False, "note": SECRET, "detail": {"ngram_chip": SECRET}}],
}


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


# ── debug on (the default) ──────────────────────────────────────────────
def test_debug_is_the_default_and_sent_to_ask_and_explain(client, search):
    assert _post(client).status_code == 200
    (ask,) = search.ask_calls
    (explain,) = search.explain_calls
    assert ask["debug"] is True and ask["retrieval_only"] is True
    # explain: the same identity, without retrieval_only/debug
    assert set(explain) == {"question", "legal_entity", "user_role", "user_id", "query_id"}
    for key in explain:
        assert explain[key] == ask[key]


def test_grade_goes_to_explain_too(client, search):
    _post(client, user_school_grade="3")
    assert search.explain_calls[0]["user_school_grade"] == "3"


def test_debug_fields_pass_through_unchanged(client, search):
    search.ask_response = copy.deepcopy(DEBUG_ASK)
    out = _post(client).json()
    for key in ("effective_scope", "strategies", "key_words_used", "language",
                "answer_parts", "trace"):
        assert out[key] == DEBUG_ASK[key]
    assert out["explain"] == EXPLAIN_RESPONSE


def test_missing_debug_keys_become_null_and_empty_lists(client, search):
    search.ask_response = {"answer": "x", "outcome": "answer", "answer_source": "qa_exact"}
    out = _post(client).json()
    assert out["effective_scope"] is None and out["language"] is None
    assert out["strategies"] == [] and out["key_words_used"] == []
    assert out["answer_parts"] == [] and out["trace"] == []
    assert out["explain"] == EXPLAIN_RESPONSE


# ── debug off ───────────────────────────────────────────────────────────
def test_debug_false_sends_no_debug_and_no_explain(client, search):
    out = _post(client, debug=False).json()
    (ask,) = search.ask_calls
    assert "debug" not in ask
    assert search.explain_calls == []
    assert out["explain"] is None


def test_debug_must_be_a_boolean(client, search):
    assert _post(client, debug="yes").status_code == 422
    assert search.ask_calls == []


# ── explain failures never fail the answer ──────────────────────────────
@pytest.mark.parametrize("error", [SearchUnavailable("down"), SearchServerError("500"),
                                   SearchBadRequest({"detail": "bad"}),
                                   RuntimeError("boom")])
def test_explain_failure_is_null_and_the_answer_is_returned(client, search, error):
    search.explain_error = error
    resp = _post(client)
    assert resp.status_code == 200
    out = resp.json()
    assert out["explain"] is None and out["outcome"] == "answer"
    assert len(search.explain_calls) == 1


def test_explain_with_a_non_object_body_is_null(client, search):
    search.explain_response = ["not", "an", "object"]
    assert _post(client).json()["explain"] is None


def test_ask_failure_still_maps_when_explain_ran(client, search):
    search.ask_error = SearchBadRequest({"error": {"code": "BAD_REQUEST", "message": "nope"}})
    resp = _post(client)
    assert resp.status_code == 400 and resp.json()["error"]["message"] == "nope"


def test_ask_500_is_502_with_debug(client, search):
    search.raise_server_error_on = "ask"
    assert _post(client).status_code == 502


# ── nothing is logged ───────────────────────────────────────────────────
def test_no_debug_content_reaches_a_log(client, search, caplog):
    search.ask_response = copy.deepcopy(DEBUG_ASK)
    search.explain_response = {**EXPLAIN_RESPONSE, "trace_detail": {"x": SECRET},
                               "query_norm_tokens": [SECRET]}
    with caplog.at_level(logging.DEBUG):
        assert _post(client).status_code == 200
    assert QUESTION not in caplog.text and SECRET not in caplog.text


def test_explain_failure_logs_only_the_error_type(client, search, caplog):
    search.explain_error = RuntimeError(f"echo {QUESTION} {SECRET}")
    with caplog.at_level(logging.DEBUG):
        assert _post(client).status_code == 200
    assert "RuntimeError" in caplog.text
    assert QUESTION not in caplog.text and SECRET not in caplog.text


# ── CSRF and policy of ask-preview are unchanged ────────────────────────
def test_missing_csrf_is_403_and_nothing_is_called(client, search):
    assert client.post(URL, json=BODY).status_code == 403
    assert search.ask_calls == [] and search.explain_calls == []


# ── GET entries/by-id ───────────────────────────────────────────────────
def _entry(client, entry_id=ENTRY_ID, school="ZS_DEMO"):
    return client.get(f"/qa-api/v1/schools/{school}/entries/by-id",
                      params={"entry_id": entry_id})


def test_entry_by_id_success(client, search):
    resp = _entry(client)
    assert resp.status_code == 200
    assert resp.json() == ENTRY_BY_ID
    assert search.entry_calls == [("ZS_DEMO", ENTRY_ID)]


def test_entry_by_id_extra_search_fields_are_dropped(client, search, monkeypatch):
    monkeypatch.setattr(search, "get_entry_by_id",
                        lambda e, i: {**ENTRY_BY_ID, "answer_md": "internal"})
    assert _entry(client).json() == ENTRY_BY_ID


def test_entry_id_is_canonicalised_before_search(client, search):
    assert _entry(client, ENTRY_ID.upper()).status_code == 200
    assert search.entry_calls == [("ZS_DEMO", ENTRY_ID)]


def test_entry_by_id_not_a_uuid_is_400_without_calling_search(client, search):
    resp = _entry(client, "not-a-uuid")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "BAD_REQUEST"
    assert search.entry_calls == []


def test_entry_by_id_missing_param_is_422(client):
    resp = client.get(f"{BASE}/entries/by-id")
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_entry_by_id_unknown_is_404(client):
    resp = _entry(client, "22222222-2222-2222-2222-222222222222")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "NOT_FOUND"


def test_entry_by_id_search_400_is_400(client, search, monkeypatch):
    def boom(e, i):
        raise SearchBadRequest({"error": {"code": "BAD_REQUEST", "message": "bad id"}})
    monkeypatch.setattr(search, "get_entry_by_id", boom)
    resp = _entry(client)
    assert resp.status_code == 400 and resp.json()["error"]["message"] == "bad id"


def test_entry_by_id_search_500_is_502_and_outage_503(client, search):
    search.raise_server_error_on = "get_entry_by_id"
    assert _entry(client).status_code == 502
    search.raise_server_error_on = None
    search.raise_unavailable_on = "get_entry_by_id"
    resp = _entry(client)
    assert resp.status_code == 503 and resp.json()["error"]["retryable"] is True


def test_entry_by_id_unknown_school_is_404(client):
    assert _entry(client, school="NOPE").status_code == 404


@pytest.fixture
def restricted():
    two = TwoSchoolSearch()

    def make(policy):
        app.dependency_overrides[get_search_client] = lambda: two
        app.dependency_overrides[get_policy] = lambda: policy
        app.dependency_overrides[get_principal] = lambda: STAFF
        return TestClient(app)

    yield two, make
    app.dependency_overrides.clear()


def test_entry_by_id_foreign_school_is_403(restricted):
    two, make = restricted
    resp = _entry(make(RestrictedPolicy()), school=OTHER)
    assert resp.status_code == 403 and two.entry_calls == []


def test_entry_by_id_needs_the_read_permission(restricted):
    two, make = restricted
    resp = _entry(make(RestrictedPolicy(frozenset({"read"}))))
    assert resp.status_code == 403 and two.entry_calls == []


# ── the HTTP adapter ────────────────────────────────────────────────────
def test_adapter_posts_explain_to_v1_with_the_short_timeout(monkeypatch):
    rec = Recorder(FakeResponse(200, {"query_language": "cs"}))
    monkeypatch.setattr("app.adapters.search.requests.post", rec)
    client = HttpSearchClient("http://s/v1", "http://s", timeout=7, ask_timeout=99)
    assert client.explain({"question": "q"}) == {"query_language": "cs"}
    assert rec.calls[0]["args"] == ("http://s/v1/explain",)
    assert rec.calls[0]["timeout"] == 7 and rec.calls[0]["json"] == {"question": "q"}
    client.ask({"question": "q"})
    assert rec.calls[1]["args"] == ("http://s/v1/ask",) and rec.calls[1]["timeout"] == 99


def test_adapter_entry_by_id_params(monkeypatch):
    rec = Recorder(FakeResponse(200, dict(ENTRY_BY_ID)))
    monkeypatch.setattr("app.adapters.search.requests.request", rec)
    HttpSearchClient("http://s/v1", "http://s").get_entry_by_id("ZS_DEMO", ENTRY_ID)
    call = rec.calls[0]
    assert call["args"] == ("GET", "http://s/internal/qa-admin/entries/by-id")
    assert call["params"] == {"legal_entity": "ZS_DEMO", "entry_id": ENTRY_ID}


@pytest.mark.parametrize("status,exc", [(404, SearchNotFound), (400, SearchBadRequest),
                                        (500, SearchServerError), (503, SearchUnavailable)])
def test_adapter_entry_by_id_error_mapping(monkeypatch, status, exc):
    rec = Recorder(FakeResponse(status, {"error": {"message": "x"}}))
    monkeypatch.setattr("app.adapters.search.requests.request", rec)
    with pytest.raises(exc):
        HttpSearchClient("http://s/v1", "http://s").get_entry_by_id("ZS_DEMO", ENTRY_ID)
