"""Live checks for permanent delete (CONTRACT.md §13) against the isolated dev
stack — API over real HTTP, and the row counts read straight from the dev
database, because "deleted" is only credible if the rows are actually gone.

Synthetic data only: every pack this file creates is prefixed "tdel-" and lives
in the allocated dev school. It never touches an existing pack, and it must
never be pointed at production.

Run:
    cd qa_portal/tests
    QA_PORTAL_BASE_URL=http://127.0.0.1:18282 \
        "../backend/.venv/Scripts/python.exe" -m pytest -q test_live_delete.py
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid

import httpx
import pytest

BASE_URL = os.environ.get("QA_PORTAL_BASE_URL", "http://127.0.0.1:18282")
SCHOOL = "ZAKLADNI_SKOLA_BOROVA"
OTHER_SCHOOL = "ZAKLADNI_SKOLA_LIPOVA"  # exists in the dev seed, but owns nothing here
PG_CONTAINER = os.environ.get("QA_DEV_PG", "qadev_edu_ai_postgres")
HEADERS = {"X-QA-Portal": "1", "Origin": BASE_URL}


def _sql(query: str) -> str:
    """One scalar out of the DEV database. Deliberately not a portal call:
    the point is to see the rows, not to trust the API's own report."""
    out = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", "edu_ai", "-d", "edu_ai", "-Atc", query],
        capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return out.stdout.strip()


def _counts(pack: str) -> dict:
    entries = int(_sql(f"select count(*) from edu_ai.qa_entries where pack_id='{pack}'"))
    variants = int(_sql(
        "select count(*) from edu_ai.qa_variants v join edu_ai.qa_entries e "
        f"on e.entry_id=v.entry_id where e.pack_id='{pack}'"))
    embeddings = int(_sql(
        "select count(*) from edu_ai.qa_embedding em join edu_ai.qa_entries e "
        f"on e.entry_id=em.entry_id where e.pack_id='{pack}'"))
    manifests = int(_sql(f"select count(*) from edu_ai.qa_pack_manifest where pack_id='{pack}'"))
    return {"entries": entries, "variants": variants, "embeddings": embeddings,
            "manifests": manifests}


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, headers=HEADERS, timeout=180.0) as c:
        try:
            resp = c.get("/qa-api/v1/context")
        except httpx.HTTPError as exc:
            pytest.skip(f"dev portal at {BASE_URL} is unreachable: {exc}")
        if resp.status_code != 200:
            pytest.skip(f"dev portal returned {resp.status_code}")
        yield c


def _entry(key: str) -> dict:
    return {"entry_key": key, "canonical_q": f"Otázka {key}?",
            "answer_md": f"Odpověď {key}.", "variants": [f"Jinak řečeno {key}?"],
            "sources": [], "domains": [], "is_active": True}


def _wait(client: httpx.Client, operation_id: str) -> dict:
    for _ in range(90):
        op = client.get(f"/qa-api/v1/operations/{operation_id}").json()
        if op["status"] in ("completed", "failed"):
            return op
        time.sleep(2)
    raise AssertionError("operation did not finish")


def _make_pack(client: httpx.Client, keys: list[str]) -> str:
    pack = f"tdel-{uuid.uuid4().hex[:8]}"
    body = {"base_etag": "", "entries": [_entry(k) for k in keys],
            "pack_metadata": {"visibility": "entity", "audience_roles": ["TEACHER"]}}
    prev = client.post(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}/preview", json=body)
    assert prev.status_code == 200, prev.text
    pub = client.post(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}/publish",
                      json={"preview_id": prev.json()["preview_id"], "expected_etag": ""})
    assert pub.status_code == 202, pub.text
    assert _wait(client, pub.json()["operation_id"])["status"] == "completed"
    return pack


def test_entry_delete_removes_exactly_that_entry(client):
    pack = _make_pack(client, ["prvni", "druha"])
    before = _counts(pack)
    assert before["entries"] == 2 and before["variants"] >= 2, before

    resp = client.delete(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}/entries/prvni")
    assert resp.status_code == 200, resp.text
    deleted = resp.json()["deleted"]
    assert deleted["entries"] == 1 and deleted["manifests"] == 0, deleted

    after = _counts(pack)
    assert after["entries"] == 1, after
    assert after["variants"] < before["variants"], after
    assert after["embeddings"] < before["embeddings"], (before, after)
    # the pack itself survives a single-entry delete
    assert after["manifests"] == before["manifests"], after
    assert _sql("select count(*) from edu_ai.qa_entries "
                f"where pack_id='{pack}' and entry_key='druha'") == "1"

    client.request("DELETE", f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}",
                   json={"confirm_pack_id": pack})


def test_pack_delete_removes_entries_variants_embeddings_and_manifests(client):
    pack = _make_pack(client, ["a", "b"])
    before = _counts(pack)
    assert all(before[k] > 0 for k in before), before

    resp = client.request("DELETE", f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}",
                          json={"confirm_pack_id": pack})
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"]["manifests"] >= 1, resp.text

    assert _counts(pack) == {"entries": 0, "variants": 0, "embeddings": 0, "manifests": 0}


def test_pack_delete_requires_the_typed_identifier(client):
    pack = _make_pack(client, ["a"])
    resp = client.request("DELETE", f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}",
                          json={"confirm_pack_id": pack + "x"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "DELETE_CONFIRMATION_MISMATCH", resp.text
    assert _counts(pack)["entries"] == 1, "a mismatch must not delete anything"

    client.request("DELETE", f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}",
                   json={"confirm_pack_id": pack})


def test_another_school_cannot_delete_this_packs_entry(client):
    pack = _make_pack(client, ["a"])
    resp = client.delete(f"/qa-api/v1/schools/{OTHER_SCHOOL}/packs/{pack}/entries/a")
    assert resp.status_code == 404, resp.text
    assert _counts(pack)["entries"] == 1, "a foreign school must not delete anything"

    resp = client.request("DELETE", f"/qa-api/v1/schools/{OTHER_SCHOOL}/packs/{pack}",
                          json={"confirm_pack_id": pack})
    assert resp.status_code == 404, resp.text
    assert _counts(pack)["manifests"] >= 1, "a foreign school must not delete the pack"

    client.request("DELETE", f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}",
                   json={"confirm_pack_id": pack})


def test_a_stale_preview_cannot_resurrect_a_deleted_pack(client):
    pack = _make_pack(client, ["a"])
    state = client.get(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}/entries").json()
    prev = client.post(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}/preview",
                       json={"base_etag": state["pack_etag"],
                             "entries": [_entry("a"), _entry("pozdeji")]})
    assert prev.status_code == 200, prev.text
    stale_preview = prev.json()["preview_id"]

    resp = client.request("DELETE", f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}",
                          json={"confirm_pack_id": pack})
    assert resp.status_code == 200, resp.text

    pub = client.post(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}/publish",
                      json={"preview_id": stale_preview, "expected_etag": state["pack_etag"]})
    assert pub.status_code != 202, "a stale preview must not publish a deleted pack"
    assert _counts(pack) == {"entries": 0, "variants": 0, "embeddings": 0, "manifests": 0}, \
        "the deleted pack came back"


def test_delete_is_refused_while_a_publish_holds_the_lock(client):
    pack = _make_pack(client, ["a"])
    state = client.get(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}/entries").json()
    prev = client.post(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}/preview",
                       json={"base_etag": state["pack_etag"],
                             "entries": [_entry("a"), _entry("b")]})
    assert prev.status_code == 200, prev.text

    outcome: dict = {}

    def publish():
        r = client.post(f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}/publish",
                        json={"preview_id": prev.json()["preview_id"],
                              "expected_etag": state["pack_etag"]})
        outcome["publish"] = r.status_code
        if r.status_code == 202:
            outcome["op"] = _wait(client, r.json()["operation_id"])["status"]

    t = threading.Thread(target=publish)
    t.start()
    time.sleep(0.4)  # let the publish take the lock and start its ingest
    resp = client.request("DELETE", f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}",
                          json={"confirm_pack_id": pack})
    t.join(timeout=240)

    assert outcome.get("publish") == 202, outcome
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "PUBLISH_IN_PROGRESS", resp.text
    assert _counts(pack)["entries"] >= 1, "the refused delete must not have removed anything"

    client.request("DELETE", f"/qa-api/v1/schools/{SCHOOL}/packs/{pack}",
                   json={"confirm_pack_id": pack})
