"""Client for the existing ingest gateway (POST /v1/ingest), publish only.

CONTRACT.md §6: /v1/ingest is SYNCHRONOUS. This adapter's `run_ingest` blocks
for as long as the gateway takes; callers MUST run it off the event loop
(see app.services.publish_service, which uses a thread executor) so an import
never blocks UI/API traffic.

Also wraps GET /v1/ingest-status, but ONLY by a known processing_uuid — never
by since_seconds, which is a global "recent runs" search and cannot correlate
to one user's operation (CONTRACT.md §6, forbidden explicitly).

Documents (CONTRACT.md §14.3-§14.5): `run_document_ingest` sends one file or
URL item through the same /v1/ingest, `delete_document` calls the gateway's
DELETE /v1/documents/{file_uuid}.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterator, Protocol

import requests

from ..config import settings


class IngestUnavailable(Exception):
    """Gateway unreachable, timed out, or returned 5xx."""


class IngestNotFound(Exception):
    """Gateway returned 404 (a foreign or unknown document, CONTRACT.md §14.3)."""


class IngestRejected(Exception):
    """Gateway returned 4xx — the request itself was invalid. `.body` is its JSON/text."""

    def __init__(self, status_code: int, body):
        super().__init__(f"{status_code}: {body}")
        self.status_code = status_code
        self.body = body


@dataclass
class StoredFile:
    """A gateway file stream (§14.10): `chunks` yields the bytes, `close()`
    releases the connection (idempotent)."""
    chunks: Iterator[bytes]
    content_disposition: str | None
    close: Callable[[], None]


class IngestClient(Protocol):
    def run_ingest(self, *, csv_bytes: bytes, filename: str, legal_entity: str,
                    user_id: str, visibility: str, pack_id: str, qa_mode: str,
                    domains: list[str] | None = None,
                    audience_roles: list[str] | None = None,
                    school_subject_grades: list[str] | None = None,
                    valid_from: str | None = None,
                    valid_to: str | None = None) -> dict: ...

    def get_status(self, processing_uuid: str) -> dict | None: ...

    def run_document_ingest(self, *, legal_entity: str, user_id: str, visibility: str,
                            audience_roles: list[str],
                            file: BinaryIO | bytes | None = None,
                            filename: str | None = None,
                            url: str | None = None,
                            save_level_1_child: bool = False,
                            max_child_links: int | None = None,
                            document_type: str | None = None,
                            document_language: str | None = None,
                            school_subject: str | None = None,
                            school_grade_codes: list[str] | None = None,
                            domain_codes: list[str] | None = None,
                            valid_from: str | None = None,
                            valid_to: str | None = None) -> dict: ...

    def preview_document_url(self, *, legal_entity: str, user_id: str, visibility: str,
                             audience_roles: list[str], url: str,
                             save_level_1_child: bool = False,
                             max_child_links: int | None = None,
                             document_type: str | None = None,
                             document_language: str | None = None,
                             school_subject: str | None = None,
                             school_grade_codes: list[str] | None = None,
                             domain_codes: list[str] | None = None,
                             valid_from: str | None = None,
                             valid_to: str | None = None) -> dict: ...

    def delete_document(self, legal_entity: str, file_uuid: str) -> dict: ...

    def open_document_file(self, legal_entity: str, file_uuid: str) -> StoredFile: ...


def _form_fields(required: list[tuple[str, str]],
                 lists: list[tuple[str, list[str] | None]] = (),
                 optional: list[tuple[str, str | None]] = ()) -> list[tuple[str, str]]:
    """Multipart form fields in a stable order: required scalars (always
    sent), each list as a repeated field, then optional scalars (sent only
    when non-empty)."""
    data = list(required)
    for key, values in lists:
        for v in (values or []):
            data.append((key, v))
    data += [(k, v) for k, v in optional if v]
    return data


def _error_body(resp: requests.Response):
    try:
        return resp.json()
    except ValueError:
        return resp.text


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
        data = _form_fields(
            [("legal_entity", legal_entity), ("user_id", user_id),
             ("visibility", visibility), ("user_defined_pack_id", pack_id),
             ("qa_mode", qa_mode)],
            [("domains", domains), ("audience_roles", audience_roles),
             ("school_subject_grades", school_subject_grades)],
            [("valid_from", valid_from), ("valid_to", valid_to)])
        files = {"file": (filename, csv_bytes, "text/csv")}
        return self._post_ingest(data, files, self._timeout)

    def run_document_ingest(self, *, legal_entity: str, user_id: str, visibility: str,
                            audience_roles: list[str],
                            file: BinaryIO | bytes | None = None,
                            filename: str | None = None,
                            url: str | None = None,
                            save_level_1_child: bool = False,
                            max_child_links: int | None = None,
                            document_type: str | None = None,
                            document_language: str | None = None,
                            school_subject: str | None = None,
                            school_grade_codes: list[str] | None = None,
                            domain_codes: list[str] | None = None,
                            valid_from: str | None = None,
                            valid_to: str | None = None) -> dict:
        """One document item, `file` xor `url`. Portal names map to the
        gateway's: school_grade_codes -> school_subject_grades,
        domain_codes -> domains."""
        if (file is None) == (url is None):
            raise ValueError("exactly one of file or url is required")
        data, files = self._document_form(
            legal_entity=legal_entity, user_id=user_id, visibility=visibility,
            audience_roles=audience_roles, file=file, filename=filename, url=url,
            save_level_1_child=save_level_1_child, max_child_links=max_child_links,
            document_type=document_type, document_language=document_language,
            school_subject=school_subject, school_grade_codes=school_grade_codes,
            domain_codes=domain_codes, valid_from=valid_from, valid_to=valid_to)
        return self._post_ingest(data, files, settings.document_ingest_timeout_seconds)

    def preview_document_url(self, *, legal_entity: str, user_id: str, visibility: str,
                             audience_roles: list[str], url: str,
                             save_level_1_child: bool = False,
                             max_child_links: int | None = None,
                             document_type: str | None = None,
                             document_language: str | None = None,
                             school_subject: str | None = None,
                             school_grade_codes: list[str] | None = None,
                             domain_codes: list[str] | None = None,
                             valid_from: str | None = None,
                             valid_to: str | None = None) -> dict:
        """Same form as a URL ingest plus dry_run=true (CONTRACT.md §14.8);
        synchronous, short timeout (the gateway skips Langflow)."""
        data, files = self._document_form(
            legal_entity=legal_entity, user_id=user_id, visibility=visibility,
            audience_roles=audience_roles, file=None, filename=None, url=url,
            save_level_1_child=save_level_1_child, max_child_links=max_child_links,
            document_type=document_type, document_language=document_language,
            school_subject=school_subject, school_grade_codes=school_grade_codes,
            domain_codes=domain_codes, valid_from=valid_from, valid_to=valid_to)
        data.append(("dry_run", "true"))
        return self._post_ingest(data, files, settings.document_preview_timeout_seconds)

    @staticmethod
    def _document_form(*, legal_entity, user_id, visibility, audience_roles, file, filename,
                       url, save_level_1_child, max_child_links, document_type,
                       document_language, school_subject, school_grade_codes,
                       domain_codes, valid_from, valid_to):
        required = [("legal_entity", legal_entity), ("user_id", user_id),
                    ("visibility", visibility)]
        files = None
        if url is not None:
            required.append(("url", url))
            required.append(("save_level_1_child", "true" if save_level_1_child else "false"))
            if max_child_links is not None:
                required.append(("max_child_links", str(int(max_child_links))))
        else:
            files = {"file": (filename or "upload", file, "application/octet-stream")}
        data = _form_fields(
            required,
            [("audience_roles", audience_roles),
             ("school_subject_grades", school_grade_codes),
             ("domains", domain_codes)],
            [("document_type", document_type), ("document_language", document_language),
             ("school_subject", school_subject),
             ("valid_from", valid_from), ("valid_to", valid_to)])
        return data, files

    def _post_ingest(self, data: list[tuple[str, str]], files: dict | None,
                     timeout: float) -> dict:
        try:
            resp = requests.post(f"{self._base}/v1/ingest", data=data, files=files,
                                  timeout=timeout)
        except requests.RequestException as e:
            raise IngestUnavailable(str(e)) from e
        if resp.status_code >= 500:
            raise IngestUnavailable(f"{resp.status_code}: {resp.text}")
        if resp.status_code >= 400:
            raise IngestRejected(resp.status_code, _error_body(resp))
        return resp.json()

    def delete_document(self, legal_entity: str, file_uuid: str) -> dict:
        """Gateway DELETE /v1/documents/{file_uuid} (CONTRACT.md §14.3). Its
        errors keep the FastAPI `{"detail": ...}` body."""
        canonical = str(uuid.UUID(str(file_uuid)))  # never a raw path segment
        try:
            resp = requests.delete(f"{self._base}/v1/documents/{canonical}",
                                   params={"legal_entity": legal_entity},
                                   timeout=settings.document_delete_timeout_seconds)
        except requests.RequestException as e:
            raise IngestUnavailable(str(e)) from e
        if resp.status_code == 404:
            raise IngestNotFound(_error_body(resp))
        if resp.status_code >= 500:
            raise IngestUnavailable(f"gateway answered {resp.status_code}")
        if resp.status_code >= 400:
            raise IngestRejected(resp.status_code, _error_body(resp))
        return resp.json()

    def open_document_file(self, legal_entity: str, file_uuid: str) -> StoredFile:
        """Gateway GET /v1/documents/{file_uuid}/file (§14.10), streamed. 404 ->
        IngestNotFound, other 4xx -> IngestRejected, 5xx/network -> IngestUnavailable."""
        canonical = str(uuid.UUID(str(file_uuid)))  # never a raw path segment
        try:
            resp = requests.get(f"{self._base}/v1/documents/{canonical}/file",
                                params={"legal_entity": legal_entity}, stream=True,
                                timeout=settings.document_download_timeout_seconds)
        except requests.RequestException as e:
            raise IngestUnavailable(str(e)) from e
        if resp.status_code >= 400:
            try:
                if resp.status_code == 404:
                    raise IngestNotFound(_error_body(resp))
                if resp.status_code >= 500:
                    raise IngestUnavailable(f"gateway answered {resp.status_code}")
                raise IngestRejected(resp.status_code, _error_body(resp))
            finally:
                resp.close()

        def chunks() -> Iterator[bytes]:
            try:
                yield from resp.iter_content(chunk_size=64 * 1024)
            except requests.RequestException as e:
                raise IngestUnavailable(str(e)) from e
            finally:
                resp.close()

        return StoredFile(chunks=chunks(), content_disposition=resp.headers.get("Content-Disposition"),
                          close=resp.close)

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
