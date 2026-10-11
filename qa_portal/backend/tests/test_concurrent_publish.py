"""Two overlapping publish requests for the SAME (school, pack) through the
real API wiring (CONTRACT.md §3 "Souběžné převzetí"). FakeIngestClient is a
mocked test double — no real ingest is exercised; only its artificial delay
lets the two HTTP calls genuinely overlap.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_ingest_client, get_search_client, get_store
from app.main import app
from app.state.store import StateStore
from tests.fake_ingest import FakeIngestClient
from tests.fake_search import FakeSearchClient

HEADERS = {"X-QA-Portal": "1"}


class SlowFakeIngestClient(FakeIngestClient):
    """Same behaviour as FakeIngestClient, but holds the pack lock open long
    enough (via time.sleep) for a second request to arrive mid-flight."""

    def __init__(self, search, delay_seconds: float):
        super().__init__(search)
        self._delay = delay_seconds

    def run_ingest(self, **kwargs):
        time.sleep(self._delay)
        return super().run_ingest(**kwargs)


@pytest.fixture
def search():
    return FakeSearchClient()


@pytest.fixture
def ingest(search):
    return SlowFakeIngestClient(search, delay_seconds=0.3)


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "qa_admin_state")


@pytest.fixture
def client(search, ingest, store):
    app.dependency_overrides[get_search_client] = lambda: search
    app.dependency_overrides[get_ingest_client] = lambda: ingest
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def _preview_body(base_etag: str = "etag-1") -> dict:
    return {
        "base_etag": base_etag,
        "entries": [{
            "entry_key": "k1", "canonical_q": "Kdy zacina rok?", "answer_md": "V zari.",
            "variants": [], "sources": [], "domains": ["PROVOZ"], "category_code": "GENERAL",
            "is_active": True,
        }],
    }


def test_two_concurrent_publishes_one_wins_one_gets_409(client):
    # Two independent previews for the same school+pack; neither has published
    # yet, so both are valid against the pack's current (empty) etag.
    prev_a = client.post("/qa-api/v1/schools/ZS_DEMO/packs/faq/preview",
                          json=_preview_body(), headers=HEADERS).json()
    prev_b = client.post("/qa-api/v1/schools/ZS_DEMO/packs/faq/preview",
                          json=_preview_body(), headers=HEADERS).json()

    responses: list[object] = [None, None]

    def call(idx, preview_id):
        responses[idx] = client.post(
            "/qa-api/v1/schools/ZS_DEMO/packs/faq/publish",
            json={"preview_id": preview_id, "expected_etag": "etag-1"},
            headers=HEADERS)

    t_a = threading.Thread(target=call, args=(0, prev_a["preview_id"]))
    t_b = threading.Thread(target=call, args=(1, prev_b["preview_id"]))
    t_a.start()
    time.sleep(0.05)  # let A get inside the lock before B is dispatched
    t_b.start()
    t_a.join()
    t_b.join()

    statuses = sorted(r.status_code for r in responses)
    if statuses == [202, 202]:
        pytest.fail(
            "both publishes were accepted concurrently for the SAME pack — "
            "the two requests never actually overlapped inside the TestClient "
            "wiring, so this run could not exercise the API-level race; see "
            "test_pack_lock.py for the service-level proof of mutual exclusion")

    assert 202 in statuses
    assert 409 in statuses
    assert 500 not in statuses

    loser = responses[0] if responses[0].status_code == 409 else responses[1]
    body = loser.json()
    assert body["error"]["code"] == "PUBLISH_IN_PROGRESS"
