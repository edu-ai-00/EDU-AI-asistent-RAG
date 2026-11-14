"""Document ingest queue (CONTRACT.md §14.5).

Jobs live in the state volume as `jobs/<job_id>.json`, the uploaded file as
`jobs/files/<job_id>`. ONE worker thread takes queued jobs FIFO by
`created_at` and calls the synchronous gateway `POST /v1/ingest`; there is no
automatic retry. §14.14: a job whose content hash equals the current version's
ends `skipped` without an ingest call, and the hash of an ingested file is
recorded in search once its chunks are stored. `_LOCK` guards every read-modify-write of job records, so a
request thread and the worker never clobber each other (single process only,
like state/lock.py). Logs carry job_id, school, status and counts only.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .. import errors
from ..adapters.ingest import IngestClient, IngestRejected, IngestUnavailable
from ..adapters.search import (SearchBadRequest, SearchClient, SearchNotFound,
                               SearchValidationError)
from ..config import settings
from ..state.store import StateStore

logger = logging.getLogger("qa_portal.documents.queue")

JOBS_NS = "jobs"
ACTIVE = ("queued", "processing")
FINISHED = ("completed", "failed", "unknown", "skipped")
MAX_LISTED_JOBS = 200
LOOKUP_LIMIT = 50  # documents fetched when looking for the current version of a source
SHA256_RE = re.compile(r"[0-9a-f]{64}")
ORPHAN_PAYLOAD_AGE_SECONDS = 3600
PUBLIC_KEYS = ("job_id", "batch_id", "school", "source_kind", "source", "status",
               "created_at", "started_at", "finished_at", "processing_uuid", "result",
               "stored_chunks", "error")

_LOCK = threading.RLock()
_WAKE = threading.Event()
_last_seq = 0


def _now_dt() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def _parse_ts(value) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _next_seq() -> int:
    # Strictly increasing tie-breaker for jobs created within one clock tick.
    global _last_seq
    with _LOCK:
        _last_seq = max(time.time_ns(), _last_seq + 1)
        return _last_seq


def public_job(job: dict) -> dict:
    """DocumentJob (CONTRACT.md §14.4) — internal keys never leave the portal."""
    return {k: job.get(k) for k in PUBLIC_KEYS}


def _valid_hash(value) -> str | None:
    return value if isinstance(value, str) and SHA256_RE.fullmatch(value) else None


def _file_sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as e:
        logger.warning("document queue: payload hash not computed (%s)", type(e).__name__)
        return None
    return digest.hexdigest()


def _sort_key(job: dict) -> tuple:
    return (job.get("created_at") or "", job.get("_seq") or 0, job.get("job_id") or "")


def _load_all(store: StateStore) -> list[dict]:
    jobs = []
    for job_id in store.list_ids(JOBS_NS):
        job = store.get(JOBS_NS, job_id)
        if job is not None:
            jobs.append(job)
    return jobs


def _remove_payload(store: StateStore, job_id: str) -> None:
    for path in (store.job_file_path(job_id), store.job_file_path(f"{job_id}.part")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("document queue: payload of job %s not removed (%s)",
                           job_id, type(e).__name__)


def _check_capacity(jobs: list[dict], school: str, batch_id: str) -> None:
    in_batch = sum(1 for j in jobs if j.get("school") == school and j.get("batch_id") == batch_id)
    if in_batch >= settings.max_document_batch_items:
        raise errors.validation_error(
            f"a batch holds at most {settings.max_document_batch_items} items",
            [{"field": "batch_id",
              "message": f"this batch already has {in_batch} items"}])
    active = sum(1 for j in jobs if j.get("school") == school and j.get("status") in ACTIVE)
    if active >= settings.document_queue_school_cap:
        raise errors.ingest_queue_full(
            f"the school already has {active} queued or processing documents")


def check_capacity(store: StateStore, school: str, batch_id: str) -> None:
    """Early check before the upload is copied; enqueue_job re-checks atomically."""
    with _LOCK:
        _check_capacity(_load_all(store), school, batch_id)


def new_job_id() -> str:
    return str(uuid.uuid4())


def enqueue_job(store: StateStore, *, job_id: str, school: str, batch_id: str,
                source_kind: str, source: str, params: dict, user_id: str,
                principal_subject: str, payload_part: Path | None) -> dict:
    """Stores the job (and moves its payload into place) under the lock, after
    the batch and school caps pass. Raises AppError (422/429) otherwise."""
    content_sha256 = _file_sha256(payload_part) if payload_part is not None else None
    with _LOCK:
        _check_capacity(_load_all(store), school, batch_id)
        if payload_part is not None:
            os.replace(payload_part, store.job_file_path(job_id))
        job = {
            "job_id": job_id, "batch_id": batch_id, "school": school,
            "source_kind": source_kind, "source": source, "status": "queued",
            "created_at": _now(), "started_at": None, "finished_at": None,
            "processing_uuid": None, "result": None, "stored_chunks": None, "error": None,
            "_seq": _next_seq(), "_params": params, "_user_id": user_id,
            "_principal_subject": principal_subject,
            "_content_sha256": content_sha256,   # FILE jobs only (§14.14)
        }
        store.save(JOBS_NS, job_id, job)
    _WAKE.set()
    logger.info("document queue: job %s queued (school=%s, kind=%s)", job_id, school, source_kind)
    return job


# ── worker side ─────────────────────────────────────────────────────────
def _claim_next(store: StateStore) -> dict | None:
    with _LOCK:
        queued = sorted((j for j in _load_all(store) if j.get("status") == "queued"),
                        key=_sort_key)
        if not queued:
            return None
        job = queued[0]
        job["status"] = "processing"
        job["started_at"] = _now()
        store.save(JOBS_NS, job["job_id"], job)
        return job


def _detail_message(body) -> str:
    """Gateway errors are FastAPI `{"detail": ...}`; keep the detail as text."""
    if isinstance(body, dict):
        if "detail" in body:
            detail = body["detail"]
            text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        elif isinstance(body.get("error"), dict):
            text = str(body["error"].get("message") or body["error"])
        else:
            text = json.dumps(body, ensure_ascii=False)
    else:
        text = str(body)
    return text[:1000]


_REJECTED_CODES = {413: "PAYLOAD_TOO_LARGE", 415: "UNSUPPORTED_MEDIA_TYPE",
                   422: "VALIDATION_ERROR"}


def _failed(code: str, message: str) -> dict:
    return {"status": "failed", "error": {"code": code, "message": message}}


def _web_page_hash(ingest: IngestClient, job: dict, common: dict, params: dict) -> str | None:
    """The gateway dry run's content hash of the page (§14.14)."""
    result = ingest.preview_document_url(
        url=job["source"], save_level_1_child=False,
        max_child_links=params.get("max_child_links"), **common)
    files = (result or {}).get("files") or []
    return _valid_hash(files[0].get("content_sha256")) if files else None


def _same_metadata(doc: dict, common: dict) -> bool:
    """Skipping is only safe when the declared metadata match too: otherwise a
    re-upload with a new validity or audience would be silently dropped. Fields the
    job leaves to the LLM (type, language, empty domains) are not compared."""
    def norm(v):
        return sorted(v or [])
    if (doc.get("visibility") != common["visibility"]
            or norm(doc.get("audience_roles")) != norm(common["audience_roles"])
            or doc.get("valid_from") != common["valid_from"]
            or doc.get("valid_to") != common["valid_to"]
            or norm(doc.get("school_grade_codes")) != norm(common["school_grade_codes"])
            or (doc.get("school_subject") or None) != (common["school_subject"] or None)):
        return False
    for key in ("document_type", "document_language"):
        if common[key] and doc.get(key) != common[key]:
            return False
    if common["domain_codes"] and norm(doc.get("domain_codes")) != norm(common["domain_codes"]):
        return False
    return True


def _unchanged_version(store: StateStore, ingest: IngestClient, search: SearchClient | None,
                       job: dict, common: dict) -> str | None:
    """§14.14: the file_uuid of the document's current version when it carries
    the same content hash as this job; None otherwise. Any lookup failure means
    "not unchanged" (the job is ingested normally); only identifiers are logged."""
    params = job.get("_params") or {}
    if search is None or (job["source_kind"] == "url" and params.get("save_level_1_child")):
        return None
    try:
        if job["source_kind"] == "file":
            new_hash = _valid_hash(job.get("_content_sha256"))
            source = params.get("file_name") or job["source"]
            uploader = (search.get_uploader(job["school"]) or {}).get("display_name")
            if not uploader:
                return None
        else:
            new_hash = _web_page_hash(ingest, job, common, params)
            source, uploader = job["source"], None
        if new_hash is None:
            return None
        data = search.list_documents(job["school"], "all", None, LOOKUP_LIMIT, None,
                                     source=source)
        for doc in data.get("documents") or []:
            if job["source_kind"] == "url":
                same = doc.get("source_url") == source
            else:
                same = (doc.get("original_file_name") == source
                        and doc.get("user_name") == uploader)
            if same:
                # An inactive document is not "current": re-uploading restores it;
                # nor is a version that stored no chunks (a failed ingest).
                if (doc.get("is_active") and doc.get("content_sha256") == new_hash
                        and (doc.get("chunk_count") or 0) > 0
                        and _same_metadata(doc, common)):
                    return doc.get("file_uuid")
                return None
    except Exception as e:
        logger.info("document queue: unchanged check for job %s failed (%s)",
                    job.get("job_id"), type(e).__name__)
    return None


def _run_job(store: StateStore, ingest: IngestClient, job: dict,
             search: SearchClient | None = None) -> dict:
    """Calls the gateway for one job; returns the fields to merge into it."""
    params = job.get("_params") or {}
    common = {
        "legal_entity": job["school"], "user_id": job["_user_id"],
        "visibility": params["visibility"], "audience_roles": params["audience_roles"],
        "document_type": params.get("document_type"),
        "document_language": params.get("document_language"),
        "school_subject": params.get("school_subject"),
        "school_grade_codes": params.get("school_grade_codes") or [],
        "domain_codes": params.get("domain_codes") or [],
        "valid_from": params.get("valid_from"), "valid_to": params.get("valid_to"),
    }
    current = _unchanged_version(store, ingest, search, job, common)
    if current:
        return {"status": "skipped", "processing_uuid": None,
                "result": {"reason": "unchanged", "file_uuid": current}}
    try:
        if job["source_kind"] == "file":
            path = store.job_file_path(job["job_id"])
            if not path.is_file():
                return _failed("INTERNAL_ERROR", "the uploaded file is no longer available")
            with open(path, "rb") as fh:
                result = ingest.run_document_ingest(
                    file=fh, filename=params.get("file_name") or job["source"], **common)
        else:
            result = ingest.run_document_ingest(
                url=job["source"], save_level_1_child=bool(params.get("save_level_1_child")),
                max_child_links=params.get("max_child_links"), **common)
    except IngestRejected as e:
        return _failed(_REJECTED_CODES.get(e.status_code, "BAD_REQUEST"),
                       _detail_message(e.body))
    except IngestUnavailable:
        return _failed("UPSTREAM_ERROR", "the ingest gateway is unreachable or failed")
    except Exception as e:  # one job must never take the worker down
        logger.warning("document queue: job %s raised (%s)", job["job_id"], type(e).__name__)
        return _failed("INTERNAL_ERROR", "unexpected error")

    result = result if isinstance(result, dict) else {}
    summary = {k: result.get(k) for k in ("status", "total_files", "ok_count", "error_count")}
    update = {"processing_uuid": result.get("processing_uuid"), "result": summary}
    if result.get("status") in ("completed", "partial"):
        update["status"] = "completed"
        update["_file_hashes"] = [
            {"file_uuid": f["file_uuid"], "content_sha256": h, "recorded": False}
            for f in result.get("files") or []
            if isinstance(f, dict) and f.get("file_uuid")
            and (h := _valid_hash(f.get("content_sha256")))]
        update["_hash_recorded"] = False
    else:
        update.update(_failed("UPSTREAM_ERROR",
                              f"ingest reported status {result.get('status')!r}"))
    return update


def _finish(store: StateStore, job: dict, update: dict) -> dict:
    with _LOCK:
        current = store.get(JOBS_NS, job["job_id"]) or job
        current.update(update)
        current["finished_at"] = _now()
        store.save(JOBS_NS, job["job_id"], current)
    _remove_payload(store, job["job_id"])
    result = current.get("result") or {}
    logger.info("document queue: job %s %s (school=%s, ok=%s, errors=%s)",
                job["job_id"], current.get("status"), current.get("school"),
                result.get("ok_count"), result.get("error_count"))
    return current


def process_next_job(store: StateStore, ingest: IngestClient,
                     search: SearchClient | None = None) -> str | None:
    """Processes the oldest queued job synchronously; None when nothing is queued.
    Without `search` no job is ever skipped as unchanged."""
    job = _claim_next(store)
    if job is None:
        return None
    _finish(store, job, _run_job(store, ingest, job, search))
    return job["job_id"]


# ── listing ─────────────────────────────────────────────────────────────
def _sum_chunks(status: dict | None) -> int:
    total = 0
    for f in (status or {}).get("files") or []:
        try:
            total += int(f.get("chunks") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
    return total


def _record_hashes(store: StateStore, search: SearchClient, job: dict) -> None:
    """§14.14: PATCH the content hash of each stored file, best effort. A 404 or
    a refused value is final; anything else is retried on a later listing."""
    hashes = job.get("_file_hashes") or []
    for item in hashes:
        if item.get("recorded"):
            continue
        try:
            search.update_document_content_hash(job["school"], item["file_uuid"],
                                                item["content_sha256"])
        except (SearchNotFound, SearchBadRequest, SearchValidationError) as e:
            logger.info("document queue: hash of job %s not recordable (%s)",
                        job.get("job_id"), type(e).__name__)
        except Exception as e:
            logger.info("document queue: hash of job %s not recorded yet (%s)",
                        job.get("job_id"), type(e).__name__)
            continue
        item["recorded"] = True
    done = all(i.get("recorded") for i in hashes)
    with _LOCK:
        fresh = store.get(JOBS_NS, job["job_id"])
        if fresh is not None:
            fresh["_file_hashes"], fresh["_hash_recorded"] = hashes, done
            store.save(JOBS_NS, job["job_id"], fresh)
    job["_hash_recorded"] = done


def _refresh_stored_chunks(store: StateStore, ingest: IngestClient, jobs: list[dict],
                           search: SearchClient | None = None) -> None:
    """§14.5: only by the job's KNOWN processing_uuid, only for a recently
    completed job without a positive count; failures leave it unchanged. Once
    the count is positive the file hashes are recorded (§14.14), same window."""
    now = _now_dt()
    window = settings.document_stored_chunks_window_seconds
    for job in jobs:
        if job.get("status") != "completed":
            continue
        finished = _parse_ts(job.get("finished_at"))
        in_window = finished is not None and (now - finished).total_seconds() < window
        if job.get("stored_chunks") in (None, 0):
            proc_uuid = job.get("processing_uuid")
            if not proc_uuid or not in_window:
                continue
            try:
                total = _sum_chunks(ingest.get_status(proc_uuid))
            except Exception as e:
                logger.info("document queue: chunk lookup for job %s failed (%s)",
                            job.get("job_id"), type(e).__name__)
                continue
            if total <= 0:
                continue
            with _LOCK:
                fresh = store.get(JOBS_NS, job["job_id"])
                if fresh is not None:
                    fresh["stored_chunks"] = total
                    store.save(JOBS_NS, job["job_id"], fresh)
            job["stored_chunks"] = total
        if (search is not None and in_window and job.get("_file_hashes")
                and not job.get("_hash_recorded")):
            _record_hashes(store, search, job)


def list_jobs(store: StateStore, ingest: IngestClient, school: str,
              batch_id: str | None, search: SearchClient | None = None) -> list[dict]:
    """Newest first (max 200); with batch_id only that batch, oldest first."""
    with _LOCK:
        jobs = [j for j in _load_all(store) if j.get("school") == school]
    if batch_id is not None:
        jobs = sorted((j for j in jobs if j.get("batch_id") == batch_id), key=_sort_key)
    else:
        jobs = sorted(jobs, key=_sort_key, reverse=True)[:MAX_LISTED_JOBS]
    _refresh_stored_chunks(store, ingest, jobs, search)
    return [public_job(j) for j in jobs]


# ── restart and housekeeping ────────────────────────────────────────────
def reconcile_jobs_on_startup(store: StateStore) -> int:
    """`queued` stays queued; `processing` becomes `unknown` — the gateway
    returns processing_uuid only at the end, so nothing can be looked up."""
    changed = 0
    with _LOCK:
        for job in _load_all(store):
            if job.get("status") != "processing":
                continue
            job["status"] = "unknown"
            job["finished_at"] = _now()
            store.save(JOBS_NS, job["job_id"], job)
            _remove_payload(store, job["job_id"])
            changed += 1
            logger.info("document queue: job %s (school=%s) -> unknown after restart",
                        job["job_id"], job.get("school"))
    return changed


def prune_jobs(store: StateStore, now: dt.datetime | None = None) -> int:
    """Removes finished jobs older than the retention, and stale payload files
    that no queued/processing job owns."""
    now = now or _now_dt()
    cutoff = now - dt.timedelta(seconds=settings.document_job_retention_seconds)
    pruned = 0
    with _LOCK:
        live: set[str] = set()
        for job in _load_all(store):
            if job.get("status") in ACTIVE:
                live.add(job["job_id"])
                continue
            finished = _parse_ts(job.get("finished_at"))
            if finished is not None and finished < cutoff:
                store.delete(JOBS_NS, job["job_id"])
                _remove_payload(store, job["job_id"])
                pruned += 1
        files_dir = store.job_file_path("x").parent
        for path in files_dir.iterdir():
            if path.name in live:
                continue
            try:
                age = now.timestamp() - path.stat().st_mtime
                if age > ORPHAN_PAYLOAD_AGE_SECONDS:
                    path.unlink()
            except OSError:
                continue
    if pruned:
        logger.info("document queue: pruned %d finished jobs", pruned)
    return pruned


class DocumentQueueWorker:
    """The single worker thread (concurrency 1, CONTRACT.md §14.5)."""

    def __init__(self, store: StateStore, ingest: IngestClient, idle_seconds: float = 2.0,
                 clock: Callable[[], float] = time.monotonic,
                 search: SearchClient | None = None):
        self._store = store
        self._ingest = ingest
        self._search = search
        self._idle = idle_seconds
        self._clock = clock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="qa-document-queue", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        _WAKE.set()
        self._thread.join(timeout)

    def _run(self) -> None:
        next_prune = self._clock() + settings.document_job_prune_interval_seconds
        while not self._stop.is_set():
            _WAKE.clear()
            done = None
            try:
                if self._clock() >= next_prune:
                    next_prune = self._clock() + settings.document_job_prune_interval_seconds
                    prune_jobs(self._store)
                done = process_next_job(self._store, self._ingest, self._search)
            except Exception as e:
                logger.warning("document queue: worker iteration failed (%s)", type(e).__name__)
            if done is None:
                _WAKE.wait(self._idle)


def start_worker(store: StateStore, ingest: IngestClient,
                 search: SearchClient | None = None) -> DocumentQueueWorker:
    if str(settings.ingest_concurrency).strip() != "1":
        logger.warning("document queue: QA_PORTAL_INGEST_CONCURRENCY=%s is not supported; using 1",
                       settings.ingest_concurrency)
    if search is None:
        from ..api.deps import get_search_client  # lazy: api.deps imports the services
        search = get_search_client()
    worker = DocumentQueueWorker(store, ingest, search=search)
    worker.start()
    return worker
