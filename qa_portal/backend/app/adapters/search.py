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

from typing import Any, Protocol

import requests

from ..config import settings


class SearchNotFound(Exception):
    """Upstream returned 404 — entity/pack/preview target does not exist."""


class SearchUnavailable(Exception):
    """Upstream unreachable, timed out, or returned 5xx. Retryable by nature."""


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


class HttpSearchClient:
    """requests-based implementation. Never used directly in unit tests."""

    def __init__(self, v1_base_url: str | None = None, internal_base_url: str | None = None,
                 timeout: float | None = None):
        self._v1 = (v1_base_url or settings.search_v1_base_url).rstrip("/")
        self._internal = (internal_base_url or settings.search_base_url).rstrip("/") + "/internal/qa-admin"
        self._timeout = timeout or settings.http_timeout_seconds

    def _get(self, base: str, path: str, params: dict | None = None) -> Any:
        try:
            resp = requests.get(f"{base}{path}", params=params, timeout=self._timeout)
        except requests.RequestException as e:
            raise SearchUnavailable(str(e)) from e
        if resp.status_code == 404:
            raise SearchNotFound(resp.text)
        if resp.status_code >= 500:
            raise SearchUnavailable(f"{resp.status_code}: {resp.text}")
        resp.raise_for_status()
        return resp.json()

    def _post(self, base: str, path: str, json_body: dict) -> Any:
        try:
            resp = requests.post(f"{base}{path}", json=json_body, timeout=self._timeout)
        except requests.RequestException as e:
            raise SearchUnavailable(str(e)) from e
        if resp.status_code >= 500:
            raise SearchUnavailable(f"{resp.status_code}: {resp.text}")
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
        if resp.status_code >= 500:
            raise SearchUnavailable(f"{resp.status_code}: {resp.text}")
        resp.raise_for_status()
        return resp.json()

    def delete_entry(self, legal_entity: str, pack_id: str, entry_key: str) -> dict:
        return self._delete(self._internal, "/entries",
                             {"legal_entity": legal_entity, "pack_id": pack_id,
                              "entry_key": entry_key})

    def delete_pack(self, legal_entity: str, pack_id: str) -> dict:
        return self._delete(self._internal, "/packs",
                             {"legal_entity": legal_entity, "pack_id": pack_id})
