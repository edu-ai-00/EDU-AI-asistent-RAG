"""Runtime configuration. No PG_* here on purpose — this service has no DB.

Everything is read from the environment so the lead's Dockerfile/compose can
wire it without touching this code. Defaults match CONTRACT.md §1 for local
dev/tests; production values come from the environment.
"""
from __future__ import annotations

import os


class Settings:
    # Internal search API — read-only data + catalogs + uploader (CONTRACT.md §9).
    search_base_url: str = os.environ.get(
        "QA_PORTAL_SEARCH_BASE_URL", "http://edu_ai_search:8090")
    # Existing public v1 endpoints on the same search service (entities, domains,
    # qa-packs) — already public today, not part of the new internal router.
    search_v1_base_url: str = os.environ.get(
        "QA_PORTAL_SEARCH_V1_BASE_URL", "http://edu_ai_search:8090/v1")
    # Ingest gateway — publish only, multipart CSV (CONTRACT.md §1).
    ingest_base_url: str = os.environ.get(
        "QA_PORTAL_INGEST_BASE_URL", "http://edu_ai_api:8080")

    # Named volume for previews/operations/exports/locks (CONTRACT.md §1, §3, §6).
    state_dir: str = os.environ.get("QA_ADMIN_STATE_DIR", "/var/lib/qa_admin_state")

    # CSRF/origin allowlist (CONTRACT.md §8). Same-origin UI+API in production;
    # tests and local dev add their own origins via env.
    allowed_origins: tuple[str, ...] = tuple(
        o.strip() for o in os.environ.get(
            "QA_PORTAL_ALLOWED_ORIGINS", "").split(",") if o.strip())

    http_timeout_seconds: float = float(
        os.environ.get("QA_PORTAL_HTTP_TIMEOUT_SECONDS", "10"))

    # Try a question (§14.9): a full answer is one LLM call, so a long timeout.
    ask_timeout_seconds: float = float(
        os.environ.get("QA_PORTAL_ASK_TIMEOUT_SECONDS", "90"))

    preview_ttl_seconds: int = int(
        os.environ.get("QA_PORTAL_PREVIEW_TTL_SECONDS", str(15 * 60)))
    export_ttl_seconds: int = int(
        os.environ.get("QA_PORTAL_EXPORT_TTL_SECONDS", str(60 * 60)))
    lock_ttl_seconds: int = int(
        os.environ.get("QA_PORTAL_LOCK_TTL_SECONDS", "120"))

    portal_version: str = os.environ.get("QA_PORTAL_VERSION", "dev")
    contract_version: str = "qa-portal/1.8.0"
    ui_default_lang: str = os.environ.get("QA_PORTAL_UI_DEFAULT_LANG", "cs")

    # Documents (CONTRACT.md §14.4-§14.5). Limits mirror the contract; the
    # gateway ingest is synchronous and slow, hence its own long timeout.
    document_ingest_timeout_seconds: float = float(
        os.environ.get("QA_PORTAL_DOCUMENT_INGEST_TIMEOUT_SECONDS", "1800"))
    # URL preview (§14.8): the gateway skips Langflow, so a modest timeout is enough.
    document_preview_timeout_seconds: float = float(
        os.environ.get("QA_PORTAL_DOCUMENT_PREVIEW_TIMEOUT_SECONDS", "120"))
    # Stored-file download (§14.10): connect/read timeout of the gateway stream.
    document_download_timeout_seconds: float = float(
        os.environ.get("QA_PORTAL_DOCUMENT_DOWNLOAD_TIMEOUT_SECONDS", "60"))
    document_delete_timeout_seconds: float = float(
        os.environ.get("QA_PORTAL_DOCUMENT_DELETE_TIMEOUT_SECONDS", "60"))
    # No .txt: the gateway lists it but has no extraction flow for it (always fails).
    document_file_extensions: tuple[str, ...] = (".pdf", ".docx", ".pptx", ".xlsx")
    max_document_file_bytes: int = 52428800
    max_document_batch_items: int = 100
    max_document_child_links: int = 10
    default_document_child_links: int = 5
    document_queue_school_cap: int = 300
    document_job_retention_seconds: int = 7 * 24 * 3600
    document_job_prune_interval_seconds: int = 3600
    document_stored_chunks_window_seconds: int = 600
    # Only 1 is supported (§14.5); any other value is logged and ignored.
    ingest_concurrency: str = os.environ.get("QA_PORTAL_INGEST_CONCURRENCY", "1")
    # "0" keeps lifespan from starting the queue worker thread.
    document_worker_enabled: bool = os.environ.get(
        "QA_PORTAL_DOCUMENT_WORKER", "1").strip() not in ("0", "false", "no")


settings = Settings()
