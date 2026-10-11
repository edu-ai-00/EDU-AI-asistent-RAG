"""Client for the existing ingest gateway (POST /v1/ingest), publish only.

CONTRACT.md §6: /v1/ingest is SYNCHRONOUS. This adapter's `run_ingest` blocks
for as long as the gateway takes; callers MUST run it off the event loop
(see app.services.publish_service, which uses a thread executor) so an import
never blocks UI/API traffic.

Also wraps GET /v1/ingest-status, but ONLY by a known processing_uuid — never
by since_seconds, which is a global "recent runs" search and cannot correlate
to one user's operation (CONTRACT.md §6, forbidden explicitly).
"""
from __future__ import annotations

from typing import Protocol

import requests

from ..config import settings


class IngestUnavailable(Exception):
    """Gateway unreachable, timed out, or returned 5xx."""


class IngestRejected(Exception):
    """Gateway returned 4xx — the request itself was invalid. `.body` is its JSON/text."""

    def __init__(self, status_code: int, body):
        super().__init__(f"{status_code}: {body}")
        self.status_code = status_code
        self.body = body


class IngestClient(Protocol):
    def run_ingest(self, *, csv_bytes: bytes, filename: str, legal_entity: str,
                    user_id: str, visibility: str, pack_id: str, qa_mode: str,
                    domains: list[str] | None = None,
                    audience_roles: list[str] | None = None,
                    school_subject_grades: list[str] | None = None,
                    valid_from: str | None = None,
                    valid_to: str | None = None) -> dict: ...

    def get_status(self, processing_uuid: str) -> dict | None: ...


class HttpIngestClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self._base = (base_url or settings.ingest_base_url).rstrip("/")
        # The gateway call is synchronous end-to-end (langflow QA writer +
        # embeddings); give it much more room than the usual API timeout.
        self._timeout = timeout or max(settings.http_timeout_seconds, 300.0)

    def run_ingest(self, *, csv_bytes: bytes, filename: str, legal_entity: str,
                    user_id: str, visibility: str, pack_id: str, qa_mode: str,
                    domains: list[str] | None = None,
                    audience_roles: list[str] | None = None,
                    school_subject_grades: list[str] | None = None,
                    valid_from: str | None = None,
                    valid_to: str | None = None) -> dict:
        data = [
            ("legal_entity", legal_entity),
            ("user_id", user_id),
            ("visibility", visibility),
            ("user_defined_pack_id", pack_id),
            ("qa_mode", qa_mode),
        ]
        for d in (domains or []):
            data.append(("domains", d))
        for r in (audience_roles or []):
            data.append(("audience_roles", r))
        for g in (school_subject_grades or []):
            data.append(("school_subject_grades", g))
        if valid_from:
            data.append(("valid_from", valid_from))
        if valid_to:
            data.append(("valid_to", valid_to))
        files = {"file": (filename, csv_bytes, "text/csv")}
        try:
            resp = requests.post(f"{self._base}/v1/ingest", data=data, files=files,
                                  timeout=self._timeout)
        except requests.RequestException as e:
            raise IngestUnavailable(str(e)) from e
        if resp.status_code >= 500:
            raise IngestUnavailable(f"{resp.status_code}: {resp.text}")
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise IngestRejected(resp.status_code, body)
        return resp.json()

    def get_status(self, processing_uuid: str) -> dict | None:
        try:
            resp = requests.get(f"{self._base}/v1/ingest-status",
                                 params={"processing_uuid": processing_uuid},
                                 timeout=settings.http_timeout_seconds)
        except requests.RequestException as e:
            raise IngestUnavailable(str(e)) from e
        if resp.status_code == 404:
            return None
        if resp.status_code >= 500:
            raise IngestUnavailable(f"{resp.status_code}: {resp.text}")
        resp.raise_for_status()
        return resp.json()
