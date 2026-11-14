"""Client for the search service — the portal's ONLY path to QA data.

Two families of upstream endpoint:
  * existing public `/v1/*` (entities, domains, qa-packs) — already served by
    search/app/server.py today, unrelated to this contract.
  * the NEW `/internal/qa-admin/*` router (CONTRACT.md §9,
    contracts/qa-portal/internal-qa-admin.yaml) — a DATA worker builds it in
    parallel; it does not exist yet. This adapter is written against that
    frozen contract. Tests substitute FakeSearchClient (same method names);
    no network in this repo's test suite.

No PG_* here, no SQL — this process never touches the database (CONTRACT.md §1).
"""
from __future__ import annotations

import uuid
from typing import Any, Protocol

import requests

from ..config import settings


class SearchNotFound(Exception):
    """Upstream returned 404 — entity/pack/preview target does not exist."""


class SearchUnavailable(Exception):
    """Upstream unreachable, timed out, or returned 5xx. Retryable by nature."""


class SearchServerError(SearchUnavailable):
    """Upstream answered HTTP 500 (e.g. a database permission error). A
    subclass, so legacy `except SearchUnavailable` keeps its 503 mapping; the
    document routes map it to 502 UPSTREAM_ERROR, not retryable (§14.8)."""


class SearchConflict(Exception):
    """Upstream 409 (e.g. category code already exists). `.body` is its JSON."""

    def __init__(self, body: dict):
        super().__init__(str(body))
        self.body = body


class SearchValidationError(Exception):
    """Upstream 422 (e.g. bad category code/labels). `.body` is its JSON."""

    def __init__(self, body: dict):
        super().__init__(str(body))
        self.body = body


class SearchBadRequest(Exception):
    """Upstream 400 (e.g. a file_uuid that is not a UUID). `.body` is its JSON/text."""

    def __init__(self, body):
        super().__init__(str(body))
        self.body = body


def _raise_for_5xx(resp) -> None:
    """500 -> SearchServerError; other 5xx (502/503/504...) -> SearchUnavailable."""
    if resp.status_code == 500:
        raise SearchServerError(f"500: {resp.text}")
    if resp.status_code >= 500:
        raise SearchUnavailable(f"{resp.status_code}: {resp.text}")


class SearchClient(Protocol):
    def list_active_entities(self) -> list[dict]: ...
    def get_domains(self, legal_entity: str) -> list[dict]: ...
    def get_code_lists(self, legal_entity: str) -> dict[str, list[dict]]: ...
    def list_qa_packs(self, legal_entity: str) -> list[dict]: ...
    def get_pack_state(self, legal_entity: str, pack_id: str) -> dict: ...
    def list_entries(self, legal_entity: str, pack_id: str, limit: int,
                      cursor: str | None, lang: str | None) -> dict: ...
    def get_uploader(self, legal_entity: str) -> dict: ...
    def list_categories(self, lang: str | None, include_inactive: bool) -> list[dict]: ...
    def create_category(self, payload: dict) -> dict: ...
    def delete_entry(self, legal_entity: str, pack_id: str, entry_key: str) -> dict: ...
    def delete_pack(self, legal_entity: str, pack_id: str) -> dict: ...
    # Documents (CONTRACT.md §14.2) and the document option catalogs (§14.4).
    def list_documents(self, legal_entity: str, status: str, q: str | None,
                       limit: int, cursor: str | None, *, type_code: str | None = None,
                       role: str | None = None, validity: str | None = None,
                       source: str | None = None, file_format: str | None = None) -> dict: ...
    def get_document(self, legal_entity: str, file_uuid: str) -> dict: ...
    def update_document_validity(self, legal_entity: str, file_uuid: str,
                                 valid_from: str | None, valid_to: str | None) -> dict: ...
    def deactivate_document(self, legal_entity: str, file_uuid: str) -> dict: ...
    def activate_document(self, legal_entity: str, file_uuid: str) -> dict: ...
    def get_document_types(self) -> list[dict]: ...
    def get_validity_periods(self, legal_entity: str) -> list[dict]: ...
    # Try a question (CONTRACT.md §14.9): the chat cascade, `POST {v1}/ask`.
    def ask(self, payload: dict) -> dict: ...
    # §14.11-§14.14 (v1.8.0).
    def explain(self, payload: dict) -> dict: ...
    def get_entry_by_id(self, legal_entity: str, entry_id: str) -> dict: ...
    def get_document_stats(self, legal_entity: str) -> dict: ...
    def list_document_chunks(self, legal_entity: str, file_uuid: str, limit: int,
                             cursor: str | None) -> dict: ...
    def update_document_content_hash(self, legal_entity: str, file_uuid: str,
                                     content_sha256: str) -> dict: ...


class HttpSearchClient:
    """requests-based implementation. Never used directly in unit tests."""

    def __init__(self, v1_base_url: str | None = None, internal_base_url: str | None = None,
                 timeout: float | None = None, ask_timeout: float | None = None):
        self._v1 = (v1_base_url or settings.search_v1_base_url).rstrip("/")
        self._internal = (internal_base_url or settings.search_base_url).rstrip("/") + "/internal/qa-admin"
        self._timeout = timeout or settings.http_timeout_seconds
        self._ask_timeout = ask_timeout or settings.ask_timeout_seconds

    def _get(self, base: str, path: str, params: dict | None = None) -> Any:
        try:
            resp = requests.get(f"{base}{path}", params=params, timeout=self._timeout)
        except requests.RequestException as e:
            raise SearchUnavailable(str(e)) from e
        if resp.status_code == 404:
            raise SearchNotFound(resp.text)
        _raise_for_5xx(resp)
        resp.raise_for_status()
        return resp.json()

    def _post(self, base: str, path: str, json_body: dict) -> Any:
        try:
            resp = requests.post(f"{base}{path}", json=json_body, timeout=self._timeout)
        except requests.RequestException as e:
            raise SearchUnavailable(str(e)) from e
        _raise_for_5xx(resp)
        return resp  # caller inspects status_code for 409/422 (contract-specific)

    def list_active_entities(self) -> list[dict]:
        data = self._get(self._v1, "/entities")
        return data.get("entities", [])

    def get_domains(self, legal_entity: str) -> list[dict]:
        data = self._get(self._v1, "/domains", {"legal_entity": legal_entity})
        return data.get("domains", [])

    def get_code_lists(self, legal_entity: str) -> dict[str, list[dict]]:
        """Existing, gateway-free `GET {v1}/code-lists?legal_entity=` — used to
        validate pack_metadata against 'role'/'school_grade' (CONTRACT.md §2.4a).
        """
        data = self._get(self._v1, "/code-lists", {"legal_entity": legal_entity})
        return data.get("code_lists", {})

    def list_qa_packs(self, legal_entity: str) -> list[dict]:
        data = self._get(self._v1, "/qa-packs", {"legal_entity": legal_entity})
        return data.get("qa_packs", [])

    def get_pack_state(self, legal_entity: str, pack_id: str) -> dict:
        return self._get(self._internal, "/pack-state",
                          {"legal_entity": legal_entity, "pack_id": pack_id})

    def list_entries(self, legal_entity: str, pack_id: str, limit: int,
                      cursor: str | None, lang: str | None) -> dict:
        params = {"legal_entity": legal_entity, "pack_id": pack_id, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        if lang:
            params["lang"] = lang
        return self._get(self._internal, "/entries", params)

    def get_uploader(self, legal_entity: str) -> dict:
        return self._get(self._internal, "/uploader", {"legal_entity": legal_entity})

    def list_categories(self, lang: str | None, include_inactive: bool) -> list[dict]:
        params: dict[str, Any] = {"include_inactive": include_inactive}
        if lang:
            params["lang"] = lang
        data = self._get(self._internal, "/categories", params)
        return data.get("categories", [])

    def create_category(self, payload: dict) -> dict:
        resp = self._post(self._internal, "/categories", payload)
        if resp.status_code == 201:
            return resp.json()
        if resp.status_code == 409:
            raise SearchConflict(resp.json())
        if resp.status_code == 422:
            raise SearchValidationError(resp.json())
        resp.raise_for_status()
        return resp.json()

    def _delete(self, base: str, path: str, params: dict) -> dict:
        try:
            resp = requests.delete(f"{base}{path}", params=params, timeout=self._timeout)
        except requests.RequestException as e:
            raise SearchUnavailable(str(e)) from e
        if resp.status_code == 404:
            raise SearchNotFound(resp.text)
        _raise_for_5xx(resp)
        resp.raise_for_status()
        return resp.json()

    def delete_entry(self, legal_entity: str, pack_id: str, entry_key: str) -> dict:
        return self._delete(self._internal, "/entries",
                             {"legal_entity": legal_entity, "pack_id": pack_id,
                              "entry_key": entry_key})

    def delete_pack(self, legal_entity: str, pack_id: str) -> dict:
        return self._delete(self._internal, "/packs",
                             {"legal_entity": legal_entity, "pack_id": pack_id})

    # ── documents (CONTRACT.md §14.2) ───────────────────────────────────
    def _request(self, method: str, base: str, path: str, params: dict,
                 json_body: dict | None = None) -> Any:
        """404 -> SearchNotFound, 400 -> SearchBadRequest, 422 ->
        SearchValidationError, 500 -> SearchServerError, other 5xx/network ->
        SearchUnavailable; anything else unexpected raises HTTPError."""
        kwargs: dict[str, Any] = {"params": params, "timeout": self._timeout}
        if json_body is not None:
            kwargs["json"] = json_body
        try:
            resp = requests.request(method, f"{base}{path}", **kwargs)
        except requests.RequestException as e:
            raise SearchUnavailable(str(e)) from e
        if resp.status_code == 404:
            raise SearchNotFound(resp.text)
        _raise_for_5xx(resp)
        if resp.status_code in (400, 422):
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise (SearchBadRequest(body) if resp.status_code == 400
                   else SearchValidationError(body if isinstance(body, dict) else {}))
        resp.raise_for_status()
        return resp.json()

    def list_documents(self, legal_entity: str, status: str, q: str | None,
                       limit: int, cursor: str | None, *, type_code: str | None = None,
                       role: str | None = None, validity: str | None = None,
                       source: str | None = None, file_format: str | None = None) -> dict:
        params: dict[str, Any] = {"legal_entity": legal_entity, "status": status,
                                  "limit": limit}
        if q:
            params["q"] = q
        if cursor:
            params["cursor"] = cursor
        # §14.8 filters, sent only when set (validity "all" is search's default).
        for key, value in (("type", type_code), ("role", role), ("validity", validity),
                           ("source", source), ("format", file_format)):
            if value:
                params[key] = value
        return self._request("GET", self._internal, "/documents", params)

    @staticmethod
    def _uuid(file_uuid: str) -> str:
        # Canonical form only: nothing from the request reaches a URL path raw.
        try:
            return str(uuid.UUID(str(file_uuid)))
        except ValueError:
            raise SearchBadRequest({"error": {"code": "BAD_REQUEST",
                                              "message": "file_uuid is not a UUID"}})

    def get_document(self, legal_entity: str, file_uuid: str) -> dict:
        return self._request("GET", self._internal, f"/documents/{self._uuid(file_uuid)}",
                             {"legal_entity": legal_entity})

    def update_document_validity(self, legal_entity: str, file_uuid: str,
                                 valid_from: str | None, valid_to: str | None) -> dict:
        return self._request("PATCH", self._internal,
                             f"/documents/{self._uuid(file_uuid)}/validity",
                             {"legal_entity": legal_entity},
                             {"valid_from": valid_from, "valid_to": valid_to})

    def deactivate_document(self, legal_entity: str, file_uuid: str) -> dict:
        return self._request("POST", self._internal, f"/documents/{self._uuid(file_uuid)}/deactivate",
                             {"legal_entity": legal_entity})

    def activate_document(self, legal_entity: str, file_uuid: str) -> dict:
        return self._request("POST", self._internal, f"/documents/{self._uuid(file_uuid)}/activate",
                             {"legal_entity": legal_entity})

    def get_document_types(self) -> list[dict]:
        """Existing `GET {v1}/document-types` -> [{type_code, is_ui_default, labels}]."""
        data = self._get(self._v1, "/document-types")
        return data.get("document_types", [])

    def get_validity_periods(self, legal_entity: str) -> list[dict]:
        """Existing `GET {v1}/validity-periods?legal_entity=` (organisation periods)."""
        data = self._get(self._v1, "/validity-periods", {"legal_entity": legal_entity})
        return data.get("validity_periods", [])

    def _post_v1(self, path: str, payload: dict, timeout: float) -> dict:
        """4xx -> SearchBadRequest (its body), 500 -> SearchServerError, other
        5xx/network -> SearchUnavailable."""
        try:
            resp = requests.post(f"{self._v1}{path}", json=payload, timeout=timeout)
        except requests.RequestException as e:
            raise SearchUnavailable(str(e)) from e
        _raise_for_5xx(resp)
        if 400 <= resp.status_code < 500:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise SearchBadRequest(body)
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError as e:
            raise SearchServerError("search answered with a non-JSON body") from e

    def ask(self, payload: dict) -> dict:
        """§14.9: a full answer is an LLM call, hence its own long timeout."""
        return self._post_v1("/ask", payload, self._ask_timeout)

    def explain(self, payload: dict) -> dict:
        """§14.11: `POST {v1}/explain`, deterministic (no LLM): the usual timeout."""
        return self._post_v1("/explain", payload, self._timeout)

    def get_entry_by_id(self, legal_entity: str, entry_id: str) -> dict:
        return self._request("GET", self._internal, "/entries/by-id",
                             {"legal_entity": legal_entity, "entry_id": entry_id})

    def get_document_stats(self, legal_entity: str) -> dict:
        return self._request("GET", self._internal, "/documents/stats",
                             {"legal_entity": legal_entity})

    def list_document_chunks(self, legal_entity: str, file_uuid: str, limit: int,
                             cursor: str | None) -> dict:
        params: dict[str, Any] = {"legal_entity": legal_entity, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", self._internal,
                             f"/documents/{self._uuid(file_uuid)}/chunks", params)

    def update_document_content_hash(self, legal_entity: str, file_uuid: str,
                                     content_sha256: str) -> dict:
        return self._request("PATCH", self._internal,
                             f"/documents/{self._uuid(file_uuid)}/content-hash",
                             {"legal_entity": legal_entity},
                             {"content_sha256": content_sha256})
