"""Publish orchestration — the other half of CONTRACT.md §3 and §6.

Publish uses ONLY the content stored under preview_id; the request body is
just {preview_id, expected_etag} (single pack) or {expected_etags, ...} (bulk
import). The pack lock is acquired BEFORE the 202 is returned and released
only when the background write+verify finishes, so "check -> write -> verify"
is one uninterrupted critical section per (school, pack) — see state/lock.py
for why this is single-instance only.

The ingest call is synchronous (existing gateway). It is scheduled as a
Starlette BackgroundTask, which Starlette runs a plain `def` callable through
`run_in_threadpool` automatically — that is the "off the event loop" thread
CONTRACT.md §6 asks for, without hand-rolling one.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

from .. import errors
from ..adapters.ingest import IngestClient, IngestRejected, IngestUnavailable
from ..adapters.search import SearchClient
from ..state.lock import PackLock, PublishInProgress
from ..state.store import StateStore
from . import csv_io
from .pack_service import get_pack_state

logger = logging.getLogger("qa_portal.publish")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def public_operation(op: dict) -> dict:
    """Strip internal bookkeeping keys (leading `_`) before the HTTP response."""
    return {k: v for k, v in op.items() if not k.startswith("_")}


def _uploader_or_raise(search: SearchClient, school: str) -> dict:
    from ..adapters.search import SearchNotFound, SearchUnavailable
    try:
        return search.get_uploader(school)
    except SearchNotFound:
        raise errors.publish_uploader_unavailable(
            f"no technical uploader is configured for {school!r}")
    except SearchUnavailable as e:
        raise errors.publish_uploader_unavailable(f"uploader lookup failed: {e}")


def _existing_pack_metadata_confirmed(metadata: dict) -> bool:
    """§6/§2.4: an existing pack's stored metadata must already carry a
    reach — never invented by an `or` fallback at ingest time.
    """
    return bool(metadata.get("visibility")) and bool(metadata.get("audience_roles"))


def _ingest_domains_and_metadata(metadata: dict) -> dict:
    return {
        "domains": metadata.get("domain_codes") or [],
        "audience_roles": metadata.get("audience_roles") or [],
        "school_subject_grades": metadata.get("school_grade_codes") or [],
        "valid_from": metadata.get("valid_from"),
        "valid_to": metadata.get("valid_to"),
        "visibility": metadata.get("visibility") or "entity",
    }


def begin_pack_publish(*, search: SearchClient, store: StateStore, school: str,
                        pack_id: str, preview_id: str, expected_etag: str,
                        lock_ttl_seconds: int) -> tuple[dict, "PackLock", dict]:
    """Synchronous part: validate, lock, mint the operation. Returns
    (operation_record, held_lock, execution_context) for the caller to hand
    to run_pack_publish as a BackgroundTask. Raises AppError on any rejection
    — in which case NO operation is minted and NO lock is left held.
    """
    preview = store.get_preview(preview_id)
    if preview is None or preview.get("kind") != "pack":
        raise errors.not_found(f"no such preview {preview_id!r}")
    if preview["school"] != school or preview["pack_id"] != pack_id:
        # A path parameter must never widen what a stored preview covers.
        raise errors.not_found(f"preview {preview_id!r} does not belong to this pack")

    lock = PackLock(store.base_dir, school, pack_id, lock_ttl_seconds)
    try:
        lock.acquire()
    except PublishInProgress:
        raise errors.publish_in_progress()

    try:
        uploader = _uploader_or_raise(search, school)
        state = get_pack_state(search, school, pack_id)
        if state is not None and not state.get("metadata_uniform", True):
            lock.release()
            raise errors.metadata_divergent(
                f"pack {pack_id!r} has divergent per-entry metadata; publish refused",
                divergent_fields=state.get("divergent_fields"),
                divergent_entries=state.get("divergent_entries"))
        current_etag = state["pack_etag"] if state else ""
        if current_etag != expected_etag:
            lock.release()
            raise errors.version_conflict(
                "the pack changed since this preview was taken", current_etag=current_etag)
    except Exception:
        lock.release()
        raise

    if state is None:
        # §2.4a: a new pack has no state to keep metadata in — the ONLY source
        # is the confirmed copy the preview stored, never the publish body.
        metadata = preview.get("pack_metadata")
        if metadata is None:
            lock.release()
            raise errors.validation_error(
                "this preview carries no confirmed pack_metadata for a new pack",
                [{"field": "pack_metadata", "message": "missing from the stored preview"}])
    else:
        # §2.4: an existing pack always keeps reading its own state's metadata.
        metadata = state.get("metadata", {})
        if not _existing_pack_metadata_confirmed(metadata):
            lock.release()
            raise errors.validation_error(
                f"pack {pack_id!r} has no confirmed visibility/audience_roles; "
                "its metadata must be confirmed like a new pack's before publish",
                [{"field": "pack_metadata", "message": "missing visibility or audience_roles"}])

    operation_id = str(uuid.uuid4())
    op = {
        "operation_id": operation_id, "status": "accepted", "school": school,
        "pack_id": pack_id, "preview_id": preview_id, "processing_uuid": None,
        "started_at": _now(), "finished_at": None, "result_etag": None,
        "packs": [], "summary": None, "error": None,
        "_expected_etag": expected_etag,
    }
    store.save_operation(operation_id, op)
    ctx = {
        "uploader": uploader, "metadata": metadata,
        "entries": preview["entries"],
    }
    return op, lock, ctx


def run_pack_publish(*, operation_id: str, school: str, pack_id: str,
                      store: StateStore, search: SearchClient, ingest: IngestClient,
                      lock: PackLock, ctx: dict) -> None:
    """Runs in a thread (BackgroundTask). Always releases `lock` on exit."""
    op = store.get_operation(operation_id)
    try:
        op["status"] = "processing"
        store.save_operation(operation_id, op)

        entries = ctx["entries"]
        csv_bytes = csv_io.write_pack_csv(entries, include_category_column=True)
        ing_meta = _ingest_domains_and_metadata(ctx["metadata"])

        try:
            result = ingest.run_ingest(
                csv_bytes=csv_bytes, filename=f"{pack_id}.csv", legal_entity=school,
                user_id=ctx["uploader"]["user_id"], visibility=ing_meta["visibility"],
                pack_id=pack_id, qa_mode="merge", domains=ing_meta["domains"],
                audience_roles=ing_meta["audience_roles"],
                school_subject_grades=ing_meta["school_subject_grades"],
                valid_from=ing_meta["valid_from"], valid_to=ing_meta["valid_to"])
        except IngestRejected as e:
            op["status"] = "failed"
            op["error"] = {"code": "UPSTREAM_ERROR", "message": str(e.body), "retryable": False}
            return
        except IngestUnavailable as e:
            op["status"] = "failed"
            op["error"] = {"code": "UPSTREAM_ERROR", "message": str(e), "retryable": True}
            return

        op["processing_uuid"] = result.get("processing_uuid")
        store.save_operation(operation_id, op)

        if result.get("status") == "failed" or result.get("error_count", 0) and \
                not result.get("ok_count"):
            op["status"] = "failed"
            op["error"] = {"code": "UPSTREAM_ERROR",
                            "message": "ingest reported failure for every file",
                            "retryable": True}
            return

        # Truthful completion: only claim success once a fresh read shows the
        # pack actually moved (CONTRACT.md §6). A genuine no-op preview (no
        # added/changed/deactivated rows) is allowed to keep the same etag.
        new_state = get_pack_state(search, school, pack_id)
        new_etag = new_state["pack_etag"] if new_state else None
        no_op_expected = not entries  # nothing was ever staged to change
        if new_etag is not None and (new_etag != op.get("_expected_etag") or no_op_expected):
            op["status"] = "completed"
            op["result_etag"] = new_etag
        else:
            op["status"] = "failed"
            op["error"] = {
                "code": "UPSTREAM_ERROR", "retryable": True,
                "message": ("ingest reported success but a re-read of the pack did not "
                            "show the expected change — treated as unverified, not completed"),
            }
    except Exception as e:  # a step here must never crash the thread silently
        logger.warning("publish: operation %s raised (%s)", operation_id, e)
        op["status"] = "failed"
        op["error"] = {"code": "INTERNAL_ERROR", "message": "unexpected error", "retryable": True}
    finally:
        op["finished_at"] = _now()
        store.save_operation(operation_id, op)
        lock.release()


def begin_import_publish(*, search: SearchClient, store: StateStore, school: str,
                          preview_id: str, expected_etags: dict, confirm_replace: bool,
                          lock_ttl_seconds: int) -> tuple[dict, dict, dict]:
    preview = store.get_preview(preview_id)
    if preview is None or preview.get("kind") != "import":
        raise errors.not_found(f"no such import preview {preview_id!r}")
    if preview["school"] != school:
        raise errors.not_found(f"preview {preview_id!r} does not belong to this school")

    mode = preview.get("mode", "merge")
    if mode == "replace" and not confirm_replace:
        raise errors.validation_error(
            "confirm_replace must be true to publish a replace-mode import",
            [{"field": "confirm_replace", "message": "required (true) for replace mode"}])

    uploader = _uploader_or_raise(search, school)

    packs_state: list[dict] = []
    locks: dict[str, PackLock] = {}
    ctx_packs: list[dict] = []
    try:
        for p in preview["packs"]:
            pack_id = p["pack_id"]
            if not p["ok"]:
                packs_state.append({"pack_id": pack_id, "status": "skipped",
                                     "message": "preview reported validation errors for this pack"})
                continue
            expected = expected_etags.get(pack_id)
            if expected is None:
                packs_state.append({"pack_id": pack_id, "status": "failed",
                                     "message": "expected_etag missing for this pack"})
                continue
            lock = PackLock(store.base_dir, school, pack_id, lock_ttl_seconds)
            try:
                lock.acquire()
            except PublishInProgress:
                packs_state.append({"pack_id": pack_id, "status": "failed",
                                     "message": "another publish is already running for this pack"})
                continue
            # Finding 2: this pack's lock is not yet in `locks{}`, so anything
            # unexpected here must release it itself before propagating —
            # otherwise it leaks until TTL.
            try:
                state = get_pack_state(search, school, pack_id)
                current_etag = state["pack_etag"] if state else ""
                if current_etag != expected:
                    lock.release()
                    packs_state.append({"pack_id": pack_id, "status": "failed",
                                         "message": f"pack_etag conflict (current={current_etag!r})"})
                    continue
                if state is not None and not state.get("metadata_uniform", True):
                    lock.release()
                    packs_state.append({"pack_id": pack_id, "status": "failed",
                                         "message": "PACK_METADATA_DIVERGENT"})
                    continue
                if state is None:
                    # §2.4c: a new pack's metadata was confirmed at preview time
                    # (or never confirmed); the publish body is never a source.
                    metadata = p.get("pack_metadata")
                    if metadata is None:
                        lock.release()
                        packs_state.append({"pack_id": pack_id, "status": "failed",
                                             "message": "PACK_METADATA_REQUIRED"})
                        continue
                else:
                    metadata = state.get("metadata", {})
                    # §6: an existing pack with no confirmed reach must never
                    # have one invented by ingest's `or` fallbacks.
                    if not _existing_pack_metadata_confirmed(metadata):
                        lock.release()
                        packs_state.append({"pack_id": pack_id, "status": "failed",
                                             "message": "PACK_METADATA_REQUIRED"})
                        continue
            except Exception:
                lock.release()
                raise
            locks[pack_id] = lock
            ctx_packs.append({"pack_id": pack_id, "entries": p["entries"],
                               "metadata": metadata,
                               "expected_etag": expected})
            packs_state.append({"pack_id": pack_id, "status": "pending", "message": ""})
    except Exception:
        for lock in locks.values():
            lock.release()
        raise

    operation_id = str(uuid.uuid4())
    op = {
        "operation_id": operation_id, "status": "accepted", "school": school, "pack_id": None,
        "preview_id": preview_id, "processing_uuid": None, "started_at": _now(),
        "finished_at": None, "result_etag": None, "packs": packs_state, "summary": None,
        "error": None,
    }
    store.save_operation(operation_id, op)
    ctx = {"uploader": uploader, "mode": mode, "packs": ctx_packs}
    return op, locks, ctx


def run_import_publish(*, operation_id: str, school: str, store: StateStore,
                        search: SearchClient, ingest: IngestClient,
                        locks: dict[str, PackLock], ctx: dict) -> None:
    """Runs in a thread (BackgroundTask). Each pack's own lock is released as
    soon as that pack is done — a slow pack never blocks the others' locks
    for longer than its own write, and a crash mid-loop only leaves the
    CURRENT pack's lock held (cleared by its TTL).
    """
    op = store.get_operation(operation_id)
    by_id = {p["pack_id"]: p for p in op["packs"]}
    try:
        op["status"] = "processing"
        store.save_operation(operation_id, op)

        for pc in ctx["packs"]:
            pack_id = pc["pack_id"]
            lock = locks[pack_id]
            entry = by_id[pack_id]
            try:
                csv_bytes = csv_io.write_pack_csv(pc["entries"], include_category_column=True)
                ing_meta = _ingest_domains_and_metadata(pc["metadata"])
                try:
                    result = ingest.run_ingest(
                        csv_bytes=csv_bytes, filename=f"{pack_id}.csv", legal_entity=school,
                        user_id=ctx["uploader"]["user_id"], visibility=ing_meta["visibility"],
                        pack_id=pack_id, qa_mode=ctx["mode"], domains=ing_meta["domains"],
                        audience_roles=ing_meta["audience_roles"],
                        school_subject_grades=ing_meta["school_subject_grades"],
                        valid_from=ing_meta["valid_from"], valid_to=ing_meta["valid_to"])
                except (IngestRejected, IngestUnavailable) as e:
                    entry["status"] = "failed"
                    entry["message"] = str(e)
                    continue

                new_state = get_pack_state(search, school, pack_id)
                new_etag = new_state["pack_etag"] if new_state else None
                no_op = not pc["entries"]
                if new_etag is not None and (no_op or new_etag != pc["expected_etag"]):
                    entry["status"] = "ok"
                    entry["message"] = ""
                else:
                    entry["status"] = "failed"
                    entry["message"] = "ingest reported success but pack_etag did not change"
                if result.get("status") == "failed":
                    entry["status"] = "failed"
                    entry["message"] = "ingest reported failure for this pack"
            finally:
                lock.release()

        # §6: `completed` only when NO pack ended `failed` — `packs[]` stays
        # authoritative. A `skipped` pack (preview said not ok) alone does
        # not make the whole operation failed.
        failed_count = sum(1 for p in op["packs"] if p["status"] == "failed")
        if failed_count:
            op["status"] = "failed"
            op["error"] = {
                "code": "IMPORT_PACKS_FAILED",
                "message": f"{failed_count} of {len(op['packs'])} packs failed",
                "retryable": True,
            }
        else:
            op["status"] = "completed"
    except Exception as e:
        logger.warning("import publish: operation %s raised (%s)", operation_id, e)
        op["status"] = "failed"
        op["error"] = {"code": "INTERNAL_ERROR", "message": "unexpected error", "retryable": True}
        for lock in locks.values():
            lock.release()
    finally:
        op["finished_at"] = _now()
        store.save_operation(operation_id, op)


def reconcile_operations_on_startup(*, store: StateStore, ingest: IngestClient,
                                     search: SearchClient) -> None:
    """CONTRACT.md §6: an operation stuck in accepted/processing after a
    restart is marked unknown and looked up by its KNOWN processing_uuid —
    never blindly re-run, never derived from since_seconds.
    """
    for op_id in store.list_operation_ids():
        op = store.get_operation(op_id)
        if not op or op.get("status") not in ("accepted", "processing"):
            continue
        op["status"] = "unknown"
        proc_uuid = op.get("processing_uuid")
        if proc_uuid:
            try:
                status = ingest.get_status(proc_uuid)
            except Exception as e:
                logger.warning("startup reconcile: ingest-status lookup failed for %s (%s)",
                               op_id, e)
                status = None
            if status is not None:
                op["_reconcile_note"] = f"ingest-status for known processing_uuid: {status.get('status')}"
        else:
            op["_reconcile_note"] = ("no processing_uuid was recorded before the restart; "
                                      "compare pack_etag manually before retrying")
        op["finished_at"] = op.get("finished_at") or _now()
        store.save_operation(op_id, op)
        logger.info("startup reconcile: operation %s (%s/%s) -> unknown",
                     op_id, op.get("school"), op.get("pack_id"))
