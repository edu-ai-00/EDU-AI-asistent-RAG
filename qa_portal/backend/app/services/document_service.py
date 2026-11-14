"""Documents page services (CONTRACT.md §14): options, upload validation,
read/(de)activation passthrough to search and deletion through the gateway.

The portal has no database: reads and (de)activation go to search
/internal/qa-admin/documents*, deletion to the gateway DELETE
/v1/documents/{file_uuid}, ingest through the queue (document_queue.py).
Logs carry school, file_uuid, job_id and counts — never titles, URLs or names.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.requests import Request

from .. import errors
from ..adapters.ingest import (IngestClient, IngestNotFound, IngestRejected, IngestUnavailable,
                               StoredFile)
from ..adapters.search import (SearchBadRequest, SearchClient, SearchNotFound,
                               SearchServerError, SearchUnavailable,
                               SearchValidationError)
from ..api.models import (CheckSourcesIn, DocumentMetadataIn, DocumentValidityIn,
                          PreviewUrlIn)
from ..config import settings
from ..state.store import StateStore
from . import document_queue, pack_service
from .labels import code_options
from .validation import fetch_code_lists

logger = logging.getLogger("qa_portal.documents")

STATUSES = ("active", "inactive", "all")
VALIDITIES = ("current", "expired", "future", "expiring", "all")
FORMATS = ("pdf", "docx", "pptx", "xlsx", "web")
VERSION_KEYS = ("file_uuid", "processing_uuid", "ingested_at", "is_active", "chunk_count",
                "file_format", "content_sha256")
STATS_KEYS = ("active", "inactive", "expired", "expiring_30", "future", "qa_entries_active")
REFRESH_KEYS = ("visibility", "audience_roles", "document_type", "document_language",
                "school_subject", "school_grade_codes", "domain_codes", "valid_from",
                "valid_to")
MAX_URL_LENGTH = 2048
MULTIPART_SLACK_BYTES = 1024 * 1024  # form fields + multipart framing around the file
CHUNK_BYTES = 1024 * 1024
CHECK_SOURCES_LIMIT = 50  # documents fetched per source (a file name can recur per uploader)


# ── helpers ─────────────────────────────────────────────────────────────
def canonical_uuid(value: str, field: str = "file_uuid") -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise errors.bad_request(f"{field} is not a UUID")


def _upstream_message(body) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if "detail" in body:
            d = body["detail"]
            return d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)
    return str(body)[:500]


def _call_search(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except SearchNotFound:
        raise errors.not_found("no such document for this school")
    except SearchBadRequest as e:
        raise errors.bad_request(_upstream_message(e.body))
    except SearchValidationError as e:
        err = (e.body or {}).get("error") if isinstance(e.body, dict) else None
        fields = err.get("fields") if isinstance(err, dict) else None
        raise errors.validation_error("search rejected the request", fields or [])
    except SearchUnavailable as e:
        raise errors.search_failure(e, "the document catalog is unavailable")


def _uploader(search: SearchClient, school: str) -> dict:
    """Technical uploader (§2.2); a search 500 is 502 like everywhere else here."""
    try:
        return search.get_uploader(school)
    except SearchNotFound:
        raise errors.publish_uploader_unavailable(
            f"no technical uploader is configured for {school!r}")
    except SearchServerError as e:
        raise errors.search_failure(e, "uploader lookup failed")
    except SearchUnavailable as e:
        logger.info("uploader lookup for %s failed (%s)", school, type(e).__name__)
        raise errors.publish_uploader_unavailable("uploader lookup failed")


# ── reads and (de)activation ────────────────────────────────────────────
def list_documents(search: SearchClient, school: str, status: str, q: str | None,
                   limit: int, cursor: str | None, *, type_code: str | None = None,
                   role: str | None = None, validity: str | None = None,
                   source: str | None = None, file_format: str | None = None) -> dict:
    if validity is not None and validity not in VALIDITIES:
        raise errors.validation_error(
            "validity is not supported",
            [{"field": "validity", "message": "one of " + ", ".join(VALIDITIES)}])
    if file_format is not None and file_format not in FORMATS:
        raise errors.validation_error(
            "format is not supported",
            [{"field": "format", "message": "one of " + ", ".join(FORMATS)}])
    data = _call_search(search.list_documents, school, status, q or None, limit, cursor,
                        type_code=type_code or None, role=role or None,
                        validity=validity or None, source=source or None,
                        file_format=file_format or None)
    return {"documents": data.get("documents", []), "next_cursor": data.get("next_cursor")}


def get_document(search: SearchClient, school: str, file_uuid: str) -> dict:
    data = _call_search(search.get_document, school, canonical_uuid(file_uuid))
    document = {k: v for k, v in (data.get("document") or {}).items() if k != "file_name"}
    # Whitelist: the stored `<file_uuid>_<name>` never leaves the portal (§14.10).
    versions = [{k: v.get(k) for k in VERSION_KEYS} for v in data.get("versions", [])]
    return {"document": document or None, "versions": versions}


def get_stats(search: SearchClient, school: str) -> dict:
    """§14.12: per-document counts and the active QA entries, passed through."""
    data = _call_search(search.get_document_stats, school)
    return {k: data.get(k) for k in STATS_KEYS}


def list_chunks(search: SearchClient, school: str, file_uuid: str, limit: int,
                cursor: str | None) -> dict:
    """§14.13: the text chunks of one version (the cursor is search's own)."""
    data = _call_search(search.list_document_chunks, school, canonical_uuid(file_uuid),
                        limit, cursor or None)
    return {"chunks": data.get("chunks") or [], "total": data.get("total"),
            "next_cursor": data.get("next_cursor")}


def refresh_document(search: SearchClient, store: StateStore, school: str, file_uuid: str,
                     principal_subject: str) -> dict:
    """POST refresh (§14.14): re-ingest a web document with the shown version's
    metadata and no level-1 links. A file document is 422."""
    fu = canonical_uuid(file_uuid)
    document = _call_search(search.get_document, school, fu).get("document") or {}
    source_url = document.get("source_url")
    if document.get("source_kind") != "web" or not source_url:
        raise errors.validation_error(
            "only a web document can be refreshed",
            [{"field": "file_uuid", "message": "this document was uploaded as a file"}])
    if document.get("visibility") not in ("entity", "organization"):
        raise errors.validation_error(
            "the document's visibility cannot be re-ingested",
            [{"field": "visibility", "message": "must be entity or organization"}])
    uploader = _uploader(search, school)
    batch_id = str(uuid.uuid4())
    document_queue.check_capacity(store, school, batch_id)
    params = {k: document.get(k) for k in REFRESH_KEYS}
    for key in ("audience_roles", "school_grade_codes", "domain_codes"):
        params[key] = list(params[key] or [])
    params.update(file_name=None, save_level_1_child=False, max_child_links=None)
    job = document_queue.enqueue_job(
        store, job_id=document_queue.new_job_id(), school=school, batch_id=batch_id,
        source_kind="url", source=source_url, params=params, user_id=uploader["user_id"],
        principal_subject=principal_subject, payload_part=None)
    logger.info("documents: refresh queued school=%s file_uuid=%s job_id=%s",
                school, fu, job["job_id"])
    return {"job": document_queue.public_job(job)}


def set_document_active(search: SearchClient, school: str, file_uuid: str,
                        active: bool) -> dict:
    fu = canonical_uuid(file_uuid)
    fn = search.activate_document if active else search.deactivate_document
    data = _call_search(fn, school, fu)
    logger.info("documents: %s school=%s file_uuid=%s",
                "activated" if active else "deactivated", school, fu)
    return {"document": data.get("document")}


def set_document_validity(search: SearchClient, school: str, file_uuid: str,
                          body: DocumentValidityIn) -> dict:
    """PATCH validity (§14.8): the dates are validated BEFORE search is called."""
    fu = canonical_uuid(file_uuid)
    clean, fields = _check_dates(body.valid_from, body.valid_to, "")
    if fields:
        raise errors.validation_error("the validity did not pass validation", fields)
    data = _call_search(search.update_document_validity, school, fu,
                        clean["valid_from"], clean["valid_to"])
    logger.info("documents: validity updated school=%s file_uuid=%s", school, fu)
    return {"document": data.get("document")}


def _clean_source(kind: str, value: str) -> str:
    value = value.strip()
    return _basename(value) if kind == "file" else value


def check_sources(search: SearchClient, school: str, body: CheckSourcesIn) -> dict:
    """POST check-sources (§14.8): which sources already exist and whether a new
    upload would retire them (url always; file only for the technical uploader's)."""
    items = body.sources
    if len(items) > settings.max_document_batch_items:
        raise errors.validation_error(
            "too many sources",
            [{"field": "sources",
              "message": f"at most {settings.max_document_batch_items} sources per request"}])
    fields = [{"field": f"sources[{i}].value", "message": "must be 1-2048 characters"}
              for i, it in enumerate(items)
              if not 0 < len(_clean_source(it.kind, it.value)) <= MAX_URL_LENGTH]
    if fields:
        raise errors.validation_error("the sources did not pass validation", fields)

    uploader_name = None
    if any(it.kind == "file" for it in items):
        uploader_name = _uploader(search, school).get("display_name")
    cache: dict[tuple[str, str], tuple[dict | None, bool]] = {}
    results = []
    for it in items:
        value = _clean_source(it.kind, it.value)
        key = (it.kind, value)
        if key not in cache:
            data = _call_search(search.list_documents, school, "all", None,
                                CHECK_SOURCES_LIMIT, None, source=value)
            cache[key] = _pick_existing(it.kind, value, data.get("documents") or [],
                                        uploader_name)
        existing, supersedes = cache[key]
        results.append({"kind": it.kind, "value": it.value, "existing": existing,
                        "supersedes": supersedes})
    logger.info("documents: checked school=%s sources=%d existing=%d", school, len(items),
                sum(1 for r in results if r["existing"]))
    return {"results": results}


def _pick_existing(kind: str, value: str, docs: list[dict],
                   uploader_name: str | None) -> tuple[dict | None, bool]:
    if kind == "url":
        hit = next((d for d in docs if d.get("source_url") == value), None)
        return (hit, True) if hit else (None, False)
    named = [d for d in docs if d.get("original_file_name") == value]
    if not named:
        return None, False
    # The ingest flow keys files by name AND uploader: only the technical
    # uploader's document is retired by a new upload.
    own = next((d for d in named
                if uploader_name is not None and d.get("user_name") == uploader_name), None)
    return (own, True) if own else (named[0], False)


def preview_url(search: SearchClient, ingest: IngestClient, school: str,
                body: PreviewUrlIn) -> dict:
    """POST preview-url (§14.8): validated like an upload item, then a gateway
    dry run as the technical uploader. 4xx -> 400 with its message, else 503."""
    fields: list[dict] = []
    url = body.url.strip()
    if not _valid_url(url):
        fields.append({"field": "url", "message": "must be an absolute http(s) URL"})
    max_links = body.max_child_links
    if max_links is None:
        max_links = settings.default_document_child_links
    elif not 1 <= max_links <= settings.max_document_child_links:
        fields.append({"field": "max_child_links",
                       "message": f"an integer 1-{settings.max_document_child_links}"})
    if fields:
        raise errors.validation_error("the preview did not pass validation", fields)
    clean, code_errors = validate_metadata(body.metadata, search, school)
    if code_errors:
        raise errors.validation_error("the metadata did not pass validation", code_errors)
    uploader = _uploader(search, school)
    try:
        result = ingest.preview_document_url(
            legal_entity=school, user_id=uploader["user_id"], visibility=clean["visibility"],
            audience_roles=clean["audience_roles"], url=url,
            save_level_1_child=body.save_level_1_child, max_child_links=max_links,
            document_type=clean["document_type"],
            document_language=clean["document_language"],
            school_subject=clean["school_subject"],
            school_grade_codes=clean["school_grade_codes"],
            domain_codes=clean["domain_codes"],
            valid_from=clean["valid_from"], valid_to=clean["valid_to"])
    except IngestRejected as e:
        raise errors.bad_request(_upstream_message(e.body))
    except IngestUnavailable:
        raise errors.unavailable("the ingest gateway is unavailable")
    files = [{"source_url": f.get("source_url"), "source_type": f.get("source_type"),
              "page_role": f.get("page_role")} for f in (result or {}).get("files") or []]
    logger.info("documents: url preview school=%s files=%d", school, len(files))
    return {"files": files}


def open_download(ingest: IngestClient, school: str, file_uuid: str) -> StoredFile:
    """§14.10: the gateway stream of one version's stored file."""
    fu = canonical_uuid(file_uuid)
    try:
        stored = ingest.open_document_file(school, fu)
    except IngestNotFound:
        raise errors.not_found("the file of this version is not stored")
    except IngestRejected as e:
        raise errors.bad_request(_upstream_message(e.body))
    except IngestUnavailable:
        raise errors.unavailable("the ingest gateway is unavailable")
    logger.info("documents: download school=%s file_uuid=%s", school, fu)
    return stored


def delete_document(ingest: IngestClient, school: str, file_uuid: str,
                    confirm_file_uuid: str) -> dict:
    """The retyped confirmation is checked BEFORE the gateway is called."""
    fu = canonical_uuid(file_uuid)
    try:
        confirm = str(uuid.UUID(str(confirm_file_uuid)))
    except ValueError:
        confirm = None
    if confirm != fu:
        raise errors.delete_confirmation_mismatch(
            "confirm_file_uuid does not match the document in the path")
    try:
        result = ingest.delete_document(school, fu)
    except IngestNotFound:
        raise errors.not_found("no such document for this school")
    except IngestRejected as e:
        raise errors.bad_request(_upstream_message(e.body))
    except IngestUnavailable:
        raise errors.unavailable("the ingest gateway is unavailable")
    deleted = (result or {}).get("deleted") or {}
    logger.info("documents: deleted school=%s file_uuid=%s versions=%s chunks=%s files_removed=%s",
                school, fu, deleted.get("versions"), deleted.get("chunks"),
                (result or {}).get("files_removed"))
    return result


# ── options ─────────────────────────────────────────────────────────────
def _document_types(search: SearchClient) -> list[dict]:
    try:
        return search.get_document_types()
    except SearchUnavailable as e:
        raise errors.search_failure(e, "document types unavailable")


def _validity_periods(search: SearchClient, school: str) -> list[dict]:
    try:
        raw = search.get_validity_periods(school)
    except SearchUnavailable as e:
        raise errors.search_failure(e, "validity periods unavailable")
    return [{"id": p.get("id"), "name": p.get("name"), "valid_from": p.get("valid_from"),
             "valid_to": p.get("valid_to"), "is_default": bool(p.get("is_default"))}
            for p in raw]


def build_options(search: SearchClient, school: str, lang: str | None) -> dict:
    """DocumentOptions (CONTRACT.md §14.4), labels resolved as §4.1."""
    code_lists = fetch_code_lists(search, school)
    default = settings.ui_default_lang
    return {
        "visibility": ["entity", "organization"],
        "audience_roles": code_options(code_lists.get("role"), lang, default),
        "school_grades": code_options(code_lists.get("school_grade"), lang, default),
        "school_subjects": code_options(code_lists.get("school_subject"), lang, default),
        "languages": code_options(code_lists.get("language"), lang, default),
        "document_types": code_options(_document_types(search), lang, default,
                                       code_key="type_code"),
        "domains": pack_service.get_school_domains(search, school, lang),
        "validity_periods": _validity_periods(search, school),
        "file_extensions": list(settings.document_file_extensions),
        "max_file_bytes": settings.max_document_file_bytes,
        "max_batch_items": settings.max_document_batch_items,
        "max_child_links": settings.max_document_child_links,
    }


# ── metadata validation ─────────────────────────────────────────────────
def _canonical_map(codes) -> dict[str, str]:
    return {str(c).casefold(): str(c) for c in codes}


def _blank_to_none(value):
    return value if value not in ("", None) else None


def _check_dates(raw_from, raw_to, prefix: str) -> tuple[dict, list[dict]]:
    """ISO dates (blank = null) and from <= to; prefix namespaces the 422 fields."""
    fields: list[dict] = []
    dates: dict[str, dt.date | None] = {}
    for name, value in (("valid_from", raw_from), ("valid_to", raw_to)):
        raw = _blank_to_none(value)
        dates[name] = None
        if raw is not None:
            try:
                dates[name] = dt.date.fromisoformat(raw)
            except ValueError:
                fields.append({"field": f"{prefix}{name}",
                               "message": f"expected an ISO date (YYYY-MM-DD), got {raw!r}"})
    if dates["valid_from"] and dates["valid_to"] and dates["valid_from"] > dates["valid_to"]:
        fields.append({"field": f"{prefix}valid_from", "message": "valid_from is after valid_to"})
    return ({n: d.isoformat() if d else None for n, d in dates.items()}, fields)


def validate_metadata(meta: DocumentMetadataIn, search: SearchClient,
                      school: str) -> tuple[dict, list[dict]]:
    """Every code against the school's DocumentOptions; returns the metadata
    with canonical codes plus the 422 `fields` list."""
    code_lists = fetch_code_lists(search, school)
    roles = _canonical_map(c["code"] for c in code_lists.get("role", []))
    grades = _canonical_map(c["code"] for c in code_lists.get("school_grade", []))
    subjects = _canonical_map(c["code"] for c in code_lists.get("school_subject", []))
    languages = _canonical_map(c["code"] for c in code_lists.get("language", []))
    types = _canonical_map(t["type_code"] for t in _document_types(search))
    domains = _canonical_map(pack_service.domain_code_set(search, school))

    fields: list[dict] = []

    def one(name: str, value, allowed: dict[str, str]):
        value = _blank_to_none(value)
        if value is None:
            return None
        hit = allowed.get(str(value).casefold())
        if hit is None:
            fields.append({"field": f"metadata.{name}", "message": f"unknown code {value!r}"})
        return hit

    def many(name: str, values: list[str], allowed: dict[str, str]) -> list[str]:
        out = []
        for i, v in enumerate(values):
            hit = allowed.get(str(v).casefold())
            if hit is None:
                fields.append({"field": f"metadata.{name}[{i}]", "message": f"unknown code {v!r}"})
            elif hit not in out:
                out.append(hit)
        return out

    clean = {
        "visibility": meta.visibility,
        "audience_roles": many("audience_roles", meta.audience_roles, roles),
        "document_type": one("document_type", meta.document_type, types),
        "document_language": one("document_language", meta.document_language, languages),
        "school_subject": one("school_subject", meta.school_subject, subjects),
        "school_grade_codes": many("school_grade_codes", meta.school_grade_codes, grades),
        "domain_codes": many("domain_codes", meta.domain_codes, domains),
    }
    date_clean, date_fields = _check_dates(meta.valid_from, meta.valid_to, "metadata.")
    clean.update(date_clean)
    fields += date_fields
    return clean, fields


# ── upload ──────────────────────────────────────────────────────────────
def _basename(name: str) -> str:
    # Browsers may send a path; only the last component is the file's name.
    return PureWindowsPath(PurePosixPath(name).name).name.strip()


def _extension(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def _valid_url(url: str) -> bool:
    if len(url) > MAX_URL_LENGTH or any(c.isspace() for c in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def _parse_metadata(raw) -> tuple[DocumentMetadataIn | None, list[dict]]:
    if not isinstance(raw, str) or not raw.strip():
        return None, [{"field": "metadata", "message": "required, a JSON object"}]
    try:
        data = json.loads(raw)
    except ValueError:
        return None, [{"field": "metadata", "message": "not valid JSON"}]
    if not isinstance(data, dict):
        return None, [{"field": "metadata", "message": "must be a JSON object"}]
    try:
        return DocumentMetadataIn.model_validate(data), []
    except ValidationError as e:
        out = []
        for err in e.errors():
            loc = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.get("loc", ()))
            out.append({"field": f"metadata{loc}", "message": err.get("msg", "invalid")})
        return None, out


async def _copy_bounded(upload: UploadFile, dest, limit: int) -> int:
    """Copies the upload to `dest`, reading at most limit+1 bytes."""
    total = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await upload.read(min(CHUNK_BYTES, limit + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise errors.payload_too_large(
                        f"the file exceeds the {limit}-byte limit")
                out.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    if total == 0:
        dest.unlink(missing_ok=True)
        raise errors.validation_error("the file is empty",
                                      [{"field": "file", "message": "empty file"}])
    return total


async def accept_upload(*, request: Request, school: str, search: SearchClient,
                        store: StateStore, principal_subject: str) -> dict:
    """POST /schools/{school}/documents (CONTRACT.md §14.4). Policy and CSRF
    already ran as route dependencies; the body is read only here."""
    limit = settings.max_document_file_bytes
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > limit + MULTIPART_SLACK_BYTES:
        raise errors.payload_too_large(f"the upload exceeds the {limit}-byte limit")
    try:
        form = await request.form(max_files=1, max_fields=16)
    except Exception:
        raise errors.bad_request("malformed multipart/form-data body")
    try:
        return await _accept_form(form, school=school, search=search, store=store,
                                  principal_subject=principal_subject)
    finally:
        await form.close()


async def _accept_form(form, *, school: str, search: SearchClient, store: StateStore,
                       principal_subject: str) -> dict:
    fields: list[dict] = []
    upload = form.get("file")
    if isinstance(upload, str):
        fields.append({"field": "file", "message": "must be a file part"})
        upload = None
    url_raw = form.get("url")
    url = url_raw.strip() if isinstance(url_raw, str) and url_raw.strip() else None
    if fields or (upload is None) == (url is None):
        raise errors.validation_error(
            "send exactly one of file or url",
            fields or [{"field": "file", "message": "exactly one of file or url is required"},
                       {"field": "url", "message": "exactly one of file or url is required"}])

    filename = None
    if upload is not None:
        filename = _basename(upload.filename or "")
        if not filename:
            raise errors.validation_error("the file has no name",
                                          [{"field": "file", "message": "missing file name"}])
        ext = _extension(filename)
        if ext not in settings.document_file_extensions:
            raise errors.unsupported_media(
                f"extension {ext or '(none)'!r} is not supported; allowed: "
                + ", ".join(settings.document_file_extensions))
    elif not _valid_url(url):
        fields.append({"field": "url", "message": "must be an absolute http(s) URL"})

    save_raw = form.get("save_level_1_child")
    save_children = False
    if save_raw not in (None, ""):
        if not isinstance(save_raw, str) or save_raw.lower() not in ("true", "false"):
            fields.append({"field": "save_level_1_child", "message": "must be true or false"})
        else:
            save_children = save_raw.lower() == "true"
            if save_children and upload is not None:
                fields.append({"field": "save_level_1_child", "message": "only with url"})

    links_raw = form.get("max_child_links")
    max_links = settings.default_document_child_links
    if links_raw not in (None, ""):
        try:
            max_links = int(str(links_raw))
            if not 1 <= max_links <= settings.max_document_child_links:
                raise ValueError
        except ValueError:
            fields.append({"field": "max_child_links",
                           "message": f"an integer 1-{settings.max_document_child_links}"})

    batch_raw = form.get("batch_id")
    batch_id = None
    try:
        batch_id = str(uuid.UUID(str(batch_raw))) if isinstance(batch_raw, str) else None
    except ValueError:
        pass
    if batch_id is None:
        fields.append({"field": "batch_id", "message": "required, a UUID"})

    meta, meta_errors = _parse_metadata(form.get("metadata"))
    fields += meta_errors
    if fields:
        raise errors.validation_error("the upload did not pass validation", fields)

    clean, code_errors = validate_metadata(meta, search, school)
    if code_errors:
        raise errors.validation_error("the metadata did not pass validation", code_errors)

    uploader = _uploader(search, school)
    document_queue.check_capacity(store, school, batch_id)

    job_id = document_queue.new_job_id()
    part = None
    if upload is not None:
        part = store.job_file_path(f"{job_id}.part")
        await _copy_bounded(upload, part, settings.max_document_file_bytes)
    params = {**clean, "file_name": filename,
              "save_level_1_child": save_children if url else False,
              "max_child_links": max_links if url else None}
    try:
        job = document_queue.enqueue_job(
            store, job_id=job_id, school=school, batch_id=batch_id,
            source_kind="file" if upload is not None else "url",
            source=filename if upload is not None else url, params=params,
            user_id=uploader["user_id"], principal_subject=principal_subject,
            payload_part=part)
    except BaseException:
        if part is not None:
            part.unlink(missing_ok=True)
        raise
    return {"job": document_queue.public_job(job)}
