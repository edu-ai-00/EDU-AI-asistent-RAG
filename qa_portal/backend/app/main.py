"""ASGI entrypoint. Wires the API routers under /qa-api/v1 and, if present,
serves the built SPA at /knowledge/ from static/qa (qa_portal/Dockerfile copies
the frontend build there; in dev the directory does not exist and that must not break
import or startup — see CONTRACT.md §8 and the Dockerfile comment on the
SPA fallback never swallowing /qa-api/ 404s).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from . import errors
from .api import ask, catalog, context, documents, exports, imports, operations, packs
from .api.deps import get_ingest_client, get_search_client, get_store
from .config import settings
from .services import document_queue, publish_service

logger = logging.getLogger("qa_portal.main")

API_PREFIX = "/qa-api/v1"

# Relative to this package, not a hard-coded /app path, so it also resolves
# to qa_portal/backend/static/qa when run outside the container.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static" / "qa"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """CONTRACT.md §6: any operation left accepted/processing by a previous
    process must be marked unknown and looked up by its known processing_uuid
    before this instance answers new requests. The store dir or the ingest
    upstream may be unavailable at boot (fresh volume, dependency still
    starting) — that must degrade to a logged warning, never a crash loop.
    """
    try:
        publish_service.reconcile_operations_on_startup(
            store=get_store(), ingest=get_ingest_client(), search=get_search_client())
    except Exception as e:
        logger.warning("startup reconcile: skipped (%s)", e)
    # Document queue (CONTRACT.md §14.5): same rule — degrade, never crash.
    worker = None
    try:
        document_queue.reconcile_jobs_on_startup(get_store())
        document_queue.prune_jobs(get_store())
    except Exception as e:
        logger.warning("document queue startup reconcile: skipped (%s)", type(e).__name__)
    if settings.document_worker_enabled:
        try:
            worker = document_queue.start_worker(get_store(), get_ingest_client())
        except Exception as e:
            logger.warning("document queue worker: not started (%s)", type(e).__name__)
    yield
    if worker is not None:
        worker.stop()


app = FastAPI(title="EDU-AI QA portal", version="1.0.0", lifespan=lifespan)

app.add_exception_handler(errors.AppError, errors.app_error_handler)
app.add_exception_handler(RequestValidationError, errors.request_validation_exception_handler)
app.add_exception_handler(Exception, errors.unhandled_exception_handler)

app.include_router(context.router, prefix=API_PREFIX)
app.include_router(catalog.router, prefix=API_PREFIX)
app.include_router(packs.router, prefix=API_PREFIX)
app.include_router(imports.router, prefix=API_PREFIX)
app.include_router(exports.router, prefix=API_PREFIX)
app.include_router(operations.router, prefix=API_PREFIX)
app.include_router(documents.router, prefix=API_PREFIX)
app.include_router(ask.router, prefix=API_PREFIX)


@app.get(f"{API_PREFIX}/{{full_path:path}}", include_in_schema=False)
def api_not_found(full_path: str) -> JSONResponse:
    """Catches any /qa-api/v1/* path unmatched by a real route above and
    returns the JSON error envelope. Registered after the real routers so it
    only fires as a last resort; keeps API 404s out of the SPA fallback.
    """
    raise errors.not_found(f"no such API route: /{full_path}")


def mount_spa(application: FastAPI, static_dir: Path) -> None:
    """Serves the built SPA at /knowledge and /knowledge/<path> (CONTRACT.md
    §14.8). Unknown files fall back to index.html (client-side routes)."""
    root = static_dir.resolve()
    assets = root / "assets"

    @application.get("/knowledge", include_in_schema=False)
    @application.get("/knowledge/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str = "") -> FileResponse:
        candidate = (root / full_path).resolve()
        if full_path and candidate.is_file() and root in candidate.parents:
            # Vite names assets by content hash, so they never change in place.
            hashed = assets in candidate.parents
            cache = "public, max-age=31536000, immutable" if hashed else "no-cache"
            return FileResponse(candidate, headers={"Cache-Control": cache})
        # index.html must be revalidated, or a browser keeps the previous
        # release's bundle after a deploy.
        return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})


if STATIC_DIR.is_dir():
    mount_spa(app, STATIC_DIR)
