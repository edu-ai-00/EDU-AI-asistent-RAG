"""Origin/Referer must be compared as exact origins, not string prefixes
(CONTRACT.md §8). A tiny standalone app exercises csrf_guard directly; no
fake search/ingest needed since the guard never reaches a service.
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app import errors
from app.api.security import csrf_guard
from app.config import settings

app = FastAPI()
app.add_exception_handler(errors.AppError, errors.app_error_handler)
app.add_exception_handler(RequestValidationError, errors.request_validation_exception_handler)
app.add_exception_handler(Exception, errors.unhandled_exception_handler)


@app.post("/write", dependencies=[Depends(csrf_guard)])
def write() -> dict:
    return {"ok": True}


client = TestClient(app, raise_server_exceptions=False)
HEADERS = {"X-QA-Portal": "1"}


@pytest.fixture(autouse=True)
def restore_allowed_origins():
    original = settings.allowed_origins
    yield
    settings.allowed_origins = original


def test_missing_header_is_403_before_origin_logic():
    settings.allowed_origins = ()
    resp = client.post("/write")
    assert resp.status_code == 403


def test_wrong_header_is_403_before_origin_logic():
    settings.allowed_origins = ("http://10.66.0.1",)
    resp = client.post("/write", headers={"X-QA-Portal": "0"})
    assert resp.status_code == 403


def test_empty_allowlist_skips_origin_check():
    settings.allowed_origins = ()
    resp = client.post("/write", headers=HEADERS)
    assert resp.status_code == 200


def test_exact_origin_header_accepted():
    settings.allowed_origins = ("http://10.66.0.1",)
    resp = client.post("/write", headers={**HEADERS, "Origin": "http://10.66.0.1"})
    assert resp.status_code == 200


def test_referer_origin_accepted():
    settings.allowed_origins = ("http://10.66.0.1",)
    resp = client.post("/write", headers={**HEADERS, "Referer": "http://10.66.0.1/qa/"})
    assert resp.status_code == 200


def test_trailing_slash_in_config_still_matches_bare_origin():
    settings.allowed_origins = ("http://10.66.0.1/",)
    resp = client.post("/write", headers={**HEADERS, "Origin": "http://10.66.0.1"})
    assert resp.status_code == 200


@pytest.mark.parametrize("bad_origin", [
    "http://10.66.0.1.attacker.example",
    "http://10.66.0.1:9999",
    "https://10.66.0.1",
    "http://10.66.0.11",
    "not a url",
])
def test_lookalike_and_garbage_origins_rejected(bad_origin):
    settings.allowed_origins = ("http://10.66.0.1",)
    resp = client.post("/write", headers={**HEADERS, "Origin": bad_origin})
    assert resp.status_code == 403
