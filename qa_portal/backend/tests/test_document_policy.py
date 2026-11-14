"""Per-school policy (CONTRACT.md §2.1 mandatory test, extended by §14.6):
a policy restricted to one school gets 403 on every document route of
another school, never reaches upstream, and stored operations/exports of a
foreign school are 404."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api.deps import (get_ingest_client, get_policy, get_principal, get_search_client,
                          get_store)
from app.main import app
from app.policy import Principal, SchoolSelector, VpnAdminPolicy
from app.state.store import StateStore
from tests.fake_ingest import FakeIngestClient
from tests.fake_search import DOC_UUID, ENTITIES, FakeSearchClient

HEADERS = {"X-QA-Portal": "1"}
OTHER = "ZS_OTHER"
TWO_SCHOOLS = ENTITIES + [{"legal_entity_code": OTHER, "display_name": "ZS Other",
                           "kind": "school", "level": "zs", "parent_code": None}]


class TwoSchoolSearch(FakeSearchClient):
    def list_active_entities(self) -> list[dict]:
        return TWO_SCHOOLS


class RestrictedPolicy(VpnAdminPolicy):
    """Stage-2 shape: only the principal's own schools; `deny` switches
    single permissions off to prove each route asks the right method."""

    def __init__(self, deny: frozenset[str] = frozenset()):
        self.deny = deny

    def allowed_schools(self, p: Principal) -> SchoolSelector:
        return p.schools

    def _ok(self, name: str, p: Principal, school: str) -> bool:
        return name not in self.deny and p.schools.allows(school)

    def may_read(self, p, school):
        return self._ok("read", p, school)

    def may_publish(self, p, school):
        return self._ok("publish", p, school)

    def may_replace(self, p, school):
        return self._ok("replace", p, school)

    def may_ingest(self, p, school):
        return self._ok("ingest", p, school)

    def may_deactivate(self, p, school):
        return self._ok("deactivate", p, school)

    def may_delete_permanently(self, p, school):
        return self._ok("delete", p, school)


STAFF = Principal(kind="school_staff", subject="staff-1", schools=SchoolSelector.only("ZS_DEMO"))


@pytest.fixture
def search():
    return TwoSchoolSearch()


@pytest.fixture
def ingest():
    return FakeIngestClient()


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state")


def _client(search, ingest, store, policy, principal=STAFF):
    app.dependency_overrides[get_search_client] = lambda: search
    app.dependency_overrides[get_ingest_client] = lambda: ingest
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_policy] = lambda: policy
    app.dependency_overrides[get_principal] = lambda: principal
    return TestClient(app)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _upload_files():
    meta = json.dumps({"visibility": "entity", "audience_roles": ["PARENT"]})
    return {"file": ("a.pdf", b"x", "application/pdf"),
            "batch_id": (None, "33333333-0000-0000-0000-000000000001"),
            "metadata": (None, meta)}


def _routes(school: str):
    base = f"/qa-api/v1/schools/{school}"
    return [
        ("GET", f"{base}/documents", {}),
        ("GET", f"{base}/documents/{DOC_UUID}", {}),
        ("GET", f"{base}/document-options", {}),
        ("GET", f"{base}/document-jobs", {}),
        ("POST", f"{base}/documents", {"files": _upload_files()}),
        ("POST", f"{base}/documents/{DOC_UUID}/deactivate", {}),
        ("POST", f"{base}/documents/{DOC_UUID}/activate", {}),
        ("DELETE", f"{base}/documents/{DOC_UUID}", {"json": {"confirm_file_uuid": DOC_UUID}}),
    ]


@pytest.mark.parametrize("method,path,kw", _routes(OTHER))
def test_foreign_school_is_403_on_every_document_route(search, ingest, store, method, path, kw):
    client = _client(search, ingest, store, RestrictedPolicy())
    resp = client.request(method, path, headers=HEADERS, **kw)
    assert resp.status_code == 403, path
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    assert search.document_calls == []
    assert ingest.delete_calls == [] and ingest.document_received == []
    assert store.list_ids("jobs") == []


@pytest.mark.parametrize("method,path,kw", _routes("ZS_DEMO"))
def test_own_school_passes_the_policy(search, ingest, store, method, path, kw):
    client = _client(search, ingest, store, RestrictedPolicy())
    resp = client.request(method, path, headers=HEADERS, **kw)
    assert resp.status_code in (200, 202), (path, resp.text)


@pytest.mark.parametrize("deny,method,suffix,kw", [
    ("read", "GET", "documents", {}),
    ("read", "GET", "document-options", {}),
    ("read", "GET", "document-jobs", {}),
    ("ingest", "POST", "documents", {"files": _upload_files()}),
    ("deactivate", "POST", f"documents/{DOC_UUID}/deactivate", {}),
    ("deactivate", "POST", f"documents/{DOC_UUID}/activate", {}),
    ("delete", "DELETE", f"documents/{DOC_UUID}", {"json": {"confirm_file_uuid": DOC_UUID}}),
])
def test_each_route_asks_its_own_permission(search, ingest, store, deny, method, suffix, kw):
    client = _client(search, ingest, store, RestrictedPolicy(frozenset({deny})))
    resp = client.request(method, f"/qa-api/v1/schools/ZS_DEMO/{suffix}", headers=HEADERS, **kw)
    assert resp.status_code == 403


def test_delete_denied_does_not_block_deactivate(search, ingest, store):
    client = _client(search, ingest, store, RestrictedPolicy(frozenset({"delete"})))
    resp = client.post(f"/qa-api/v1/schools/ZS_DEMO/documents/{DOC_UUID}/deactivate",
                       headers=HEADERS)
    assert resp.status_code == 200


def test_schools_carry_permissions_from_policy(search, ingest, store):
    client = _client(search, ingest, store, RestrictedPolicy(frozenset({"delete"})))
    schools = client.get("/qa-api/v1/schools").json()["schools"]
    assert [s["code"] for s in schools] == ["ZS_DEMO"]          # foreign school hidden
    assert schools[0]["permissions"] == {"read": True, "publish": True, "ingest": True,
                                         "deactivate": True, "delete_permanently": False}


def test_vpn_admin_has_every_permission_everywhere(search, ingest, store):
    admin = Principal(kind="vpn_admin", subject="vpn-admin", schools=SchoolSelector.all())
    client = _client(search, ingest, store, VpnAdminPolicy(), admin)
    schools = client.get("/qa-api/v1/schools").json()["schools"]
    assert len(schools) == 2
    assert all(all(s["permissions"].values()) for s in schools)


def test_context_capabilities_come_from_policy(search, ingest, store):
    client = _client(search, ingest, store, RestrictedPolicy(frozenset({"publish"})))
    caps = client.get("/qa-api/v1/context").json()["capabilities"]
    assert caps["may_publish"] is False and caps["may_replace"] is True
    client = _client(search, ingest, store, RestrictedPolicy())
    assert client.get("/qa-api/v1/context").json()["capabilities"]["may_publish"] is True


def test_context_fails_closed_when_register_is_down(ingest, store):
    down = FakeSearchClient(raise_unavailable_on="list_active_entities")
    admin = Principal(kind="vpn_admin", subject="vpn-admin", schools=SchoolSelector.all())
    client = _client(down, ingest, store, VpnAdminPolicy(), admin)
    resp = client.get("/qa-api/v1/context")
    assert resp.status_code == 200
    assert resp.json()["capabilities"]["may_publish"] is False


def test_context_reports_contract_version(search, ingest, store):
    client = _client(search, ingest, store, VpnAdminPolicy(),
                     Principal("vpn_admin", "vpn-admin", SchoolSelector.all()))
    assert client.get("/qa-api/v1/context").json()["contract_version"] == "qa-portal/1.8.0"


# ── stored records: operations and exports ──────────────────────────────
def _op(store, op_id, school):
    rec = {"operation_id": op_id, "status": "completed", "pack_id": "faq"}
    if school is not None:
        rec["school"] = school
    store.save_operation(op_id, rec)


@pytest.mark.parametrize("school,staff,admin", [("ZS_DEMO", 200, 200), (OTHER, 404, 200),
                                                (None, 404, 200)])
def test_operation_visible_only_for_readable_school(search, ingest, store, school, staff, admin):
    op_id = "44444444-0000-0000-0000-000000000001"
    _op(store, op_id, school)
    client = _client(search, ingest, store, RestrictedPolicy())
    assert client.get(f"/qa-api/v1/operations/{op_id}").status_code == staff
    client = _client(search, ingest, store, VpnAdminPolicy(),
                     Principal("vpn_admin", "vpn-admin", SchoolSelector.all()))
    assert client.get(f"/qa-api/v1/operations/{op_id}").status_code == admin


def _export(store, export_id, school):
    meta = {"export_id": export_id, "format": "csv", "filename": "faq.csv", "bytes": 3,
            "expires_at": "2099-01-01T00:00:00+00:00", "ext": "csv", "note": None}
    if school is not None:
        meta["school"] = school
    store.save_export_blob(export_id, b"a,b", "csv")
    store.save_export_meta(export_id, meta)


@pytest.mark.parametrize("school,staff,admin", [("ZS_DEMO", 200, 200), (OTHER, 404, 200),
                                                (None, 404, 200)])
def test_export_download_visible_only_for_readable_school(search, ingest, store, school,
                                                          staff, admin):
    export_id = "55555555-0000-0000-0000-000000000001"
    _export(store, export_id, school)
    client = _client(search, ingest, store, RestrictedPolicy())
    assert client.get(f"/qa-api/v1/exports/{export_id}/download").status_code == staff
    client = _client(search, ingest, store, VpnAdminPolicy(),
                     Principal("vpn_admin", "vpn-admin", SchoolSelector.all()))
    assert client.get(f"/qa-api/v1/exports/{export_id}/download").status_code == admin


def test_new_export_records_school_but_does_not_return_it(search, ingest, store):
    client = _client(search, ingest, store, RestrictedPolicy())
    resp = client.post("/qa-api/v1/schools/ZS_DEMO/exports",
                       json={"pack_ids": ["faq"], "format": "csv"}, headers=HEADERS)
    assert resp.status_code == 201
    body = resp.json()
    assert "school" not in body and "ext" not in body
    assert store.get_export_meta(body["export_id"])["school"] == "ZS_DEMO"
    assert client.get(body["download_url"]).status_code == 200


def test_foreign_school_export_creation_is_403(search, ingest, store):
    client = _client(search, ingest, store, RestrictedPolicy())
    resp = client.post(f"/qa-api/v1/schools/{OTHER}/exports",
                       json={"pack_ids": ["faq"]}, headers=HEADERS)
    assert resp.status_code == 403
