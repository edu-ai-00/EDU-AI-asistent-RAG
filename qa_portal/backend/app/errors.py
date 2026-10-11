"""The one error envelope, per contracts/errors.yaml and CONTRACT.md §7.

{"error": {"code": str, "message": str, "retryable": bool, "fields": [...]?}}
A client branches on `code`, never on the message text.
"""
from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Raise this anywhere in a route/service; the handler below serialises it.

    `code` is one of the stable codes in CONTRACT.md §7. `fields` is only ever
    populated for 422s. Never construct the JSON body by hand elsewhere.
    """

    def __init__(self, status_code: int, code: str, message: str,
                 retryable: bool = False,
                 fields: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.fields = fields

    def body(self) -> dict:
        err: dict[str, Any] = {
            "code": self.code, "message": self.message, "retryable": self.retryable,
        }
        if self.fields is not None:
            err["fields"] = self.fields
        return {"error": err}


def bad_request(message: str) -> AppError:
    return AppError(400, "BAD_REQUEST", message, retryable=False)


def forbidden(message: str = "Policy did not allow this school or operation") -> AppError:
    return AppError(403, "FORBIDDEN", message, retryable=False)


def not_found(message: str = "Resource not found") -> AppError:
    return AppError(404, "NOT_FOUND", message, retryable=False)


def version_conflict(message: str, current_etag: str | None = None) -> AppError:
    fields = [{"field": "expected_etag", "message": f"current pack_etag is {current_etag!r}"}] \
        if current_etag is not None else None
    return AppError(409, "PACK_VERSION_CONFLICT", message, retryable=False, fields=fields)


def metadata_divergent(message: str, divergent_fields: list[str] | None = None,
                        divergent_entries: list[str] | None = None) -> AppError:
    fields = None
    if divergent_fields:
        fields = [{"field": f, "message": "differs across entries in this pack"}
                   for f in divergent_fields]
    err = AppError(409, "PACK_METADATA_DIVERGENT", message, retryable=False, fields=fields)
    err.divergent_entries = divergent_entries or []
    return err


def publish_in_progress(message: str = "Another publish is already running for this pack") -> AppError:
    return AppError(409, "PUBLISH_IN_PROGRESS", message, retryable=True)


def delete_confirmation_mismatch(message: str = "confirm_pack_id does not match the pack in the path") -> AppError:
    return AppError(409, "DELETE_CONFIRMATION_MISMATCH", message, retryable=False)


def payload_too_large(message: str = "File exceeds the size limit") -> AppError:
    return AppError(413, "PAYLOAD_TOO_LARGE", message, retryable=False)


def unsupported_media(message: str = "File is neither CSV nor ZIP") -> AppError:
    return AppError(415, "UNSUPPORTED_MEDIA_TYPE", message, retryable=False)


def validation_error(message: str, fields: list[dict[str, Any]]) -> AppError:
    return AppError(422, "VALIDATION_ERROR", message, retryable=False, fields=fields)


def import_entity_mismatch(message: str) -> AppError:
    return AppError(422, "IMPORT_ENTITY_MISMATCH", message, retryable=False)


def unknown_category(message: str, fields: list[dict[str, Any]]) -> AppError:
    return AppError(422, "UNKNOWN_CATEGORY", message, retryable=False, fields=fields)


def inactive_category(message: str, fields: list[dict[str, Any]]) -> AppError:
    return AppError(422, "INACTIVE_CATEGORY", message, retryable=False, fields=fields)


def unknown_domain(message: str, fields: list[dict[str, Any]]) -> AppError:
    return AppError(422, "UNKNOWN_DOMAIN", message, retryable=False, fields=fields)


def internal_error(message: str = "Unexpected error") -> AppError:
    return AppError(500, "INTERNAL_ERROR", message, retryable=True)


def upstream_error(message: str = "Upstream service unreachable or failed") -> AppError:
    return AppError(502, "UPSTREAM_ERROR", message, retryable=True)


def publish_uploader_unavailable(message: str) -> AppError:
    return AppError(503, "PUBLISH_UPLOADER_UNAVAILABLE", message, retryable=False)


def unavailable(message: str = "Dependency temporarily unavailable") -> AppError:
    return AppError(503, "UNAVAILABLE", message, retryable=True)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body())


async def request_validation_exception_handler(request: Request,
                                                 exc: RequestValidationError) -> JSONResponse:
    """A body that fails pydantic's own model validation (bad regex, missing
    field, unknown field under extra="forbid") must still come back as the
    contract's Error envelope, not FastAPI's default `{"detail": [...]}`.
    """
    fields = []
    for e in exc.errors():
        loc = [str(p) for p in e.get("loc", ()) if p != "body"]
        fields.append({"field": ".".join(loc) or "body", "message": e.get("msg", "invalid")})
    err = validation_error("The request body did not pass validation", fields)
    return JSONResponse(status_code=err.status_code, content=err.body())


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Never leak internals on the wire; the traceback belongs in the log only,
    # and only with identifiers — never with question/answer text (AGENTS.md).
    return JSONResponse(status_code=500, content=internal_error().body())
