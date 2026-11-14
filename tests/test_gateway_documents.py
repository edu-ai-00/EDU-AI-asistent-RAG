# written for task qa-portal-documents.
"""Gateway DELETE /v1/documents/{file_uuid} (api/app/main.py, CONTRACT.md
qa-portal sec.14.3) — search is mocked, UPLOAD_DIR is a tmp dir. No Docker.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]

# Loaded under its own name: api/app and search/app are both called `app`,
# so a plain import would collide with the search tests in one session.
_spec = importlib.util.spec_from_file_location(
    "edu_ai_gateway_main", ROOT / "api" / "app" / "main.py")
gateway = importlib.util.module_from_spec(_spec)
sys.modules["edu_ai_gateway_main"] = gateway
_spec.loader.exec_module(gateway)

ENTITY = "ZS_JEDOVNICE"
FU = "aaaaaaaa-0000-0000-0000-000000000002"
FU_OLD = "aaaaaaaa-0000-0000-0000-000000000001"
FU_OTHER = "cccccccc-0000-0000-0000-000000000009"
DELETED = {"versions": 2, "chunks": 12, "elements": 40, "tables": 3}


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "UPLOAD_DIR", tmp_path)
    for name in (f"{FU}_rad.pdf", f"{FU}_rad.md", f"{FU_OLD}_rad.pdf",
                 f"{FU_OTHER}_jine.pdf", "rad.pdf", f"x{FU}_rad.pdf"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    return tmp_path


@pytest.fixture
def calls(monkeypatch):
    """Scripted requests.delete; records (url, params)."""
    state = {"result": None, "calls": []}

    def fake_delete(url, params=None, timeout=None):
        state["calls"].append((url, params))
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]

    monkeypatch.setattr(gateway.requests, "delete", fake_delete)
    return state


@pytest.fixture
def client():
    return TestClient(gateway.app)


def _url(fu=FU):
    return f"/v1/documents/{fu}"


def test_invalid_uuid_is_400_and_search_not_called(client, calls, uploads):
    resp = client.delete(_url("not-a-uuid"), params={"legal_entity": ENTITY})
    assert resp.status_code == 400
    assert "detail" in resp.json()
    assert calls["calls"] == []


def test_search_404_is_404_and_no_file_removed(client, calls, uploads):
    before = sorted(p.name for p in uploads.iterdir())
    calls["result"] = FakeResponse(404, {"error": {"code": "NOT_FOUND"}})
    resp = client.delete(_url(), params={"legal_entity": ENTITY})
    assert resp.status_code == 404
    assert "detail" in resp.json()
    assert sorted(p.name for p in uploads.iterdir()) == before


def test_search_400_is_400(client, calls, uploads):
    calls["result"] = FakeResponse(400, {"error": {"code": "BAD_REQUEST"}})
    resp = client.delete(_url(), params={"legal_entity": ENTITY})
    assert resp.status_code == 400


@pytest.mark.parametrize("result", [
    requests.ConnectionError("down"),
    requests.Timeout("slow"),
    FakeResponse(500, {"error": {}}),
    FakeResponse(503, None),
])
def test_search_unreachable_or_5xx_is_503_and_no_file_removed(
        client, calls, uploads, result):
    before = sorted(p.name for p in uploads.iterdir())
    calls["result"] = result
    resp = client.delete(_url(), params={"legal_entity": ENTITY})
    assert resp.status_code == 503
    assert sorted(p.name for p in uploads.iterdir()) == before


def test_success_removes_only_prefixed_files(client, calls, uploads):
    calls["result"] = FakeResponse(200, {"deleted": DELETED,
                                         "file_uuids": [FU_OLD, FU]})
    # addressed in a non-canonical form: search still gets the canonical uuid
    resp = client.delete(_url(FU.upper()), params={"legal_entity": ENTITY})
    assert resp.status_code == 200
    assert resp.json() == {"deleted": DELETED, "file_uuids": [FU_OLD, FU],
                           "files_removed": 3, "warnings": []}

    url, params = calls["calls"][0]
    assert url.endswith("/internal/qa-admin/documents")
    assert "/v1/" not in url
    assert params == {"legal_entity": ENTITY, "file_uuid": FU}

    left = sorted(p.name for p in uploads.iterdir())
    assert left == sorted([f"{FU_OTHER}_jine.pdf", "rad.pdf", f"x{FU}_rad.pdf"])


def test_success_skips_directories_and_bad_uuids(client, calls, uploads):
    (uploads / f"{FU}_dir").mkdir()
    calls["result"] = FakeResponse(200, {"deleted": DELETED,
                                         "file_uuids": [FU, "../*"]})
    resp = client.delete(_url(), params={"legal_entity": ENTITY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["files_removed"] == 2
    assert len(body["warnings"]) == 1
    assert (uploads / f"{FU}_dir").is_dir()
    assert (uploads / "rad.pdf").exists()


def test_unlink_failure_is_200_with_warning(client, calls, uploads, monkeypatch):
    real_unlink = Path.unlink

    def flaky_unlink(self, *a, **kw):
        if self.name.endswith(".md"):
            raise PermissionError("locked")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    calls["result"] = FakeResponse(200, {"deleted": DELETED, "file_uuids": [FU]})
    resp = client.delete(_url(), params={"legal_entity": ENTITY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["files_removed"] == 1
    assert len(body["warnings"]) == 1 and FU in body["warnings"][0]
    assert (uploads / f"{FU}_rad.md").exists()
    assert not (uploads / f"{FU}_rad.pdf").exists()


def test_missing_upload_dir_is_fine(client, calls, monkeypatch, tmp_path):
    monkeypatch.setattr(gateway, "UPLOAD_DIR", tmp_path / "missing")
    calls["result"] = FakeResponse(200, {"deleted": DELETED, "file_uuids": [FU]})
    resp = client.delete(_url(), params={"legal_entity": ENTITY})
    assert resp.status_code == 200
    assert resp.json()["files_removed"] == 0


# ---------------------------------------------------------------------------
# POST /v1/ingest dry_run cleanup (CONTRACT.md sec.14.8)
# ---------------------------------------------------------------------------

class FakePage:
    status_code = 200
    headers = {"content-type": "text/html; charset=utf-8"}
    content = b"<html><body><p>rad</p></body></html>"

    def raise_for_status(self):
        pass


@pytest.fixture
def ingest_env(uploads, monkeypatch):
    """Principal, page fetch and child crawl mocked; uploads holds a bystander."""
    monkeypatch.setattr(gateway, "_validate_principal",
                        lambda *a, **kw: {"user_display_name": "qa_portal__X"})
    monkeypatch.setattr(gateway.requests, "get", lambda *a, **kw: FakePage())
    monkeypatch.setattr(gateway, "extract_links",
                        lambda *a, **kw: [{"url": "https://zs.example/a", "type": "html"}])

    def fake_children(links, processing_uuid):
        saved = []
        for i, link in enumerate(links):
            name = f"{FU_OTHER[:-1]}{i}_child_CH.html"
            (uploads / name).write_text("<p>c</p>", encoding="utf-8")
            saved.append({"file_uuid": name.split("_")[0], "file_name": name,
                          "file_path": str(uploads / name), "source_url": link["url"],
                          "source_type": "web", "page_role": "child"})
        return saved

    monkeypatch.setattr(gateway, "fetch_and_save_children", fake_children)
    monkeypatch.setattr(gateway, "process_file",
                        lambda **kw: {"file_uuid": kw["file_uuid"], "status": "ok"})
    return uploads


FORM = {"legal_entity": ENTITY, "user_id": "u1", "visibility": "entity",
        "audience_roles": "STUDENT"}


def _names(path):
    return sorted(p.name for p in path.iterdir())


def test_dry_run_url_removes_parent_and_children(client, ingest_env):
    before = _names(ingest_env)
    resp = client.post("/v1/ingest", data={
        **FORM, "url": "https://zs.example/rad", "dry_run": "true",
        "save_level_1_child": "true"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "dry_run"
    assert [f["page_role"] for f in body["files"]] == ["parent", "child"]
    assert _names(ingest_env) == before


def test_dry_run_file_upload_is_removed(client, ingest_env):
    before = _names(ingest_env)
    resp = client.post("/v1/ingest", data={**FORM, "dry_run": "true"},
                       files={"file": ("pravidla.pdf", b"%PDF-1.4 x", "application/pdf")})
    assert resp.status_code == 200
    assert resp.json()["status"] == "dry_run"
    assert _names(ingest_env) == before


def test_dry_run_tolerates_unlink_errors(client, ingest_env, monkeypatch):
    real_unlink = Path.unlink

    def flaky_unlink(self, *a, **kw):
        if self.name.endswith("_P.html"):
            raise PermissionError("locked")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    resp = client.post("/v1/ingest", data={
        **FORM, "url": "https://zs.example/rad", "dry_run": "true",
        "save_level_1_child": "true"})
    assert resp.status_code == 200
    left = [n for n in _names(ingest_env) if n.endswith("_P.html")]
    assert len(left) == 1                       # only the locked parent remains
    assert not any(n.endswith("_CH.html") for n in _names(ingest_env))


def test_dry_run_leaves_unrelated_files_alone(client, ingest_env):
    keep = ingest_env / "unrelated.txt"
    keep.write_text("x", encoding="utf-8")
    client.post("/v1/ingest", data={**FORM, "url": "https://zs.example/rad",
                                    "dry_run": "true"})
    assert keep.exists()
    assert (ingest_env / "rad.pdf").exists()


def test_real_ingest_keeps_saved_files(client, ingest_env):
    resp = client.post("/v1/ingest", data={
        **FORM, "url": "https://zs.example/rad", "save_level_1_child": "true"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    names = _names(ingest_env)
    assert any(n.endswith("_P.html") for n in names)
    assert any(n.endswith("_CH.html") for n in names)


# ---------------------------------------------------------------------------
# GET /v1/documents/{file_uuid}/file (sec.14.10)
# ---------------------------------------------------------------------------

@pytest.fixture
def detail(monkeypatch):
    state = {"result": None, "calls": []}

    def fake_get(url, params=None, timeout=None):
        state["calls"].append((url, params))
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]

    monkeypatch.setattr(gateway.requests, "get", fake_get)
    return state


def _versions(name):
    return {"document": {}, "versions": [{"file_uuid": FU, "file_name": name}]}


def test_file_download_streams_the_version_file(uploads, detail, client):
    detail["result"] = FakeResponse(200, _versions(f"{FU}_rad.pdf"))
    resp = client.get(f"/v1/documents/{FU}/file", params={"legal_entity": "ZS_A"})
    assert resp.status_code == 200 and resp.content == b"x"
    assert 'filename="rad.pdf"' in resp.headers["content-disposition"]
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["x-content-type-options"] == "nosniff"
    url, params = detail["calls"][0]
    assert url.endswith(f"/internal/qa-admin/documents/{FU}") and params == {"legal_entity": "ZS_A"}


@pytest.mark.parametrize("name", ["", None, f"{FU_OTHER}_jine.pdf", f"{FU}_../../etc/passwd",
                                  f"{FU}_a/b.pdf", f"{FU}_missing.pdf"])
def test_file_download_refuses_foreign_or_missing_names(uploads, detail, client, name):
    detail["result"] = FakeResponse(200, _versions(name))
    resp = client.get(f"/v1/documents/{FU}/file", params={"legal_entity": "ZS_A"})
    assert resp.status_code == 404


def test_file_download_foreign_school_404_bad_uuid_400_down_503(uploads, detail, client):
    detail["result"] = FakeResponse(404, {})
    assert client.get(f"/v1/documents/{FU}/file", params={"legal_entity": "ZS_B"}).status_code == 404
    assert client.get("/v1/documents/nope/file", params={"legal_entity": "ZS_A"}).status_code == 400
    detail["result"] = requests.ConnectionError("down")
    assert client.get(f"/v1/documents/{FU}/file", params={"legal_entity": "ZS_A"}).status_code == 503


# ---------------------------------------------------------------------------
# content_sha256 in /v1/ingest results (CONTRACT.md sec.14.14)
# ---------------------------------------------------------------------------

import hashlib

_REAL_FETCH_CHILDREN = gateway.fetch_and_save_children   # ingest_env replaces it


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Page:
    status_code = 200

    def __init__(self, content: bytes, ctype="text/html; charset=utf-8"):
        self.content = content
        self.headers = {"content-type": ctype}

    def raise_for_status(self):
        pass


def _ingest_url(client, monkeypatch, content, ctype="text/html; charset=utf-8", **extra):
    monkeypatch.setattr(gateway.requests, "get", lambda *a, **kw: Page(content, ctype))
    resp = client.post("/v1/ingest", data={
        **FORM, "url": "https://zs.example/rad", "dry_run": "true", **extra})
    assert resp.status_code == 200
    return resp.json()["files"]


def test_file_upload_hash_is_sha256_of_the_bytes(client, ingest_env):
    data = b"%PDF-1.4 obsah"
    for dry in ("true", "false"):
        resp = client.post("/v1/ingest", data={**FORM, "dry_run": dry},
                           files={"file": ("pravidla.pdf", data, "application/pdf")})
        assert resp.status_code == 200
        assert [f["content_sha256"] for f in resp.json()["files"]] == [_sha(data)]


def test_web_page_hash_ignores_markup_and_whitespace(client, ingest_env, monkeypatch):
    a = (b"<html><head><title>T</title><script>var d='2026-11-01';</script></head>"
         b"<body><h1>Rad</h1><p>Skola   zacina\n v zari.</p></body></html>")
    b = (b"<html><head><title>T</title></head><body><div class='x'><h1> Rad </h1></div>"
         b"<script>var d='2026-11-14';</script>"
         b"<section><p>Skola zacina v zari.</p></section></body></html>")
    c = b"<html><body><h1>Rad</h1><p>Skola zacina v rijnu.</p></body></html>"
    ha = _ingest_url(client, monkeypatch, a)[0]["content_sha256"]
    hb = _ingest_url(client, monkeypatch, b)[0]["content_sha256"]
    hc = _ingest_url(client, monkeypatch, c)[0]["content_sha256"]
    assert ha == hb and len(ha) == 64 and ha != hc
    assert ha == gateway.html_text_sha256(a.decode())


def test_web_page_hash_present_for_parent_in_dry_run(client, ingest_env, monkeypatch):
    files = _ingest_url(client, monkeypatch, b"<p>x</p>")
    assert files[0]["status"] == "dry_run"
    assert files[0]["content_sha256"] == gateway.html_text_sha256("<p>x</p>")


def test_url_file_hash_is_sha256_of_the_bytes(client, ingest_env, monkeypatch):
    data = b"%PDF-1.4 z webu"
    files = _ingest_url(client, monkeypatch, data, ctype="application/pdf")
    assert files[0]["source_type"] != "web"
    assert files[0]["content_sha256"] == _sha(data)


def test_children_carry_their_own_hash(uploads, monkeypatch):
    html = "<html><body><main>" + "<p>obsah stranky</p>" * 80 + "</main></body></html>"
    pdf = b"%PDF-1.4 child"
    monkeypatch.setattr(gateway, "fetch_html", lambda url: html)
    monkeypatch.setattr(gateway, "fetch_file", lambda url: pdf)
    saved = _REAL_FETCH_CHILDREN(
        [{"url": "https://zs.example/a", "type": "html"},
         {"url": "https://zs.example/b.pdf", "type": "file"}], "p-uuid")
    assert [s["content_sha256"] for s in saved] == [
        gateway.html_text_sha256(html), _sha(pdf)]


@pytest.mark.parametrize("body", [None, {"deleted": DELETED}])
def test_unreadable_200_is_200_and_removes_the_requested_files(client, calls, uploads, body):
    calls["result"] = FakeResponse(200, body)
    resp = client.delete(_url(), params={"legal_entity": ENTITY})
    assert resp.status_code == 200
    out = resp.json()
    assert out["file_uuids"] == [FU] and out["files_removed"] == 2
    assert out["warnings"] and "without details" in out["warnings"][0]
    assert (uploads / f"{FU_OLD}_rad.pdf").exists()


def test_file_download_allows_dots_inside_a_name(uploads, detail, client):
    (uploads / f"{FU}_a..b.pdf").write_text("x", encoding="utf-8")
    detail["result"] = FakeResponse(200, _versions(f"{FU}_a..b.pdf"))
    resp = client.get(f"/v1/documents/{FU}/file", params={"legal_entity": "ZS_A"})
    assert resp.status_code == 200 and 'filename="a..b.pdf"' in resp.headers["content-disposition"]
