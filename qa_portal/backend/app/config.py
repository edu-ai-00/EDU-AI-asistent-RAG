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

    preview_ttl_seconds: int = int(
        os.environ.get("QA_PORTAL_PREVIEW_TTL_SECONDS", str(15 * 60)))
    export_ttl_seconds: int = int(
        os.environ.get("QA_PORTAL_EXPORT_TTL_SECONDS", str(60 * 60)))
    lock_ttl_seconds: int = int(
        os.environ.get("QA_PORTAL_LOCK_TTL_SECONDS", "120"))

    portal_version: str = os.environ.get("QA_PORTAL_VERSION", "dev")
    contract_version: str = "qa-portal/1.3.0"
    ui_default_lang: str = os.environ.get("QA_PORTAL_UI_DEFAULT_LANG", "cs")


settings = Settings()
