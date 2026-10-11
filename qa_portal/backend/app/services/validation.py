"""Structural validation shared by the online editor and CSV/ZIP import.

CONTRACT.md §5.3: the WHOLE input is validated before any write — a single
bad domain or category code means no QA change in that pack at all. This
module only produces findings; callers decide whether to abort a pack.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from .. import errors
from ..adapters.search import SearchClient, SearchUnavailable


@dataclass
class FieldError:
    field: str
    message: str
    code: str = "VALIDATION_ERROR"
    file: str | None = None
    row: int | None = None

    def to_error_field(self) -> dict:
        """Shape for the generic Error.fields[] (field, message [, file, row])."""
        d: dict = {"field": self.field, "message": self.message}
        if self.file is not None:
            d["file"] = self.file
        if self.row is not None:
            d["row"] = self.row
        return d

    def to_import_error(self) -> dict:
        """Shape for ImportPreview.packs[].errors[] (adds code)."""
        d = {"field": self.field, "code": self.code, "message": self.message}
        if self.file is not None:
            d["file"] = self.file
        if self.row is not None:
            d["row"] = self.row
        return d


def validate_required_text(entry: dict, *, file: str | None = None,
                            row: int | None = None) -> list[FieldError]:
    errors = []
    if not (entry.get("canonical_q") or "").strip():
        errors.append(FieldError("canonical_q", "canonical_q is required", file=file, row=row))
    if not (entry.get("answer_md") or "").strip():
        errors.append(FieldError("answer_md", "answer_md is required", file=file, row=row))
    return errors


def validate_domains(domains: list[str], allowed: set[str], *, file: str | None = None,
                      row: int | None = None) -> list[FieldError]:
    errors = []
    for i, d in enumerate(domains or []):
        if d not in allowed:
            errors.append(FieldError(
                f"domains[{i}]", f"unknown domain code {d!r} for this school",
                code="UNKNOWN_DOMAIN", file=file, row=row))
    return errors


def validate_category(code: str | None, catalog: dict[str, dict], *,
                       previous_code: str | None = None, file: str | None = None,
                       row: int | None = None) -> list[FieldError]:
    """catalog: {code: {"is_active": bool}}. A category kept unchanged from the
    previous state may stay inactive (CONTRACT.md §4.3); only a NEW assignment
    of an inactive/unknown code is rejected.
    """
    if code is None:
        return []
    if code == previous_code:
        return []
    info = catalog.get(code)
    if info is None:
        return [FieldError("category_code", f"unknown category code {code!r}",
                            code="UNKNOWN_CATEGORY", file=file, row=row)]
    if not info.get("is_active", False):
        return [FieldError("category_code",
                            f"category {code!r} is inactive and cannot be newly assigned",
                            code="INACTIVE_CATEGORY", file=file, row=row)]
    return []


def fetch_code_lists(search: SearchClient, school: str) -> dict[str, list[dict]]:
    """'role'/'school_grade' code lists for pack_metadata validation
    (CONTRACT.md §2.4a). Unlike the gateway, the portal never accepts
    unvalidated metadata when this is unreachable — 503, not a silent pass.
    """
    try:
        return search.get_code_lists(school)
    except SearchUnavailable as e:
        raise errors.unavailable(f"code lists unavailable: {e}")


def validate_pack_metadata(meta: dict, code_lists: dict) -> list[FieldError]:
    """CONTRACT.md §2.4a: a new pack's confirmed metadata, checked against the
    'role'/'school_grade' code lists. Caller must fetch code_lists itself and
    treat SearchUnavailable as errors.unavailable() — this function never
    silently accepts an unverified code (that would let the gateway's
    soft-dependency behaviour leak into the portal).
    """
    errors_: list[FieldError] = []

    visibility = meta.get("visibility")
    if visibility not in ("entity", "organization"):
        errors_.append(FieldError(
            "pack_metadata.visibility",
            f"visibility must be 'entity' or 'organization', got {visibility!r}"))

    roles = meta.get("audience_roles") or []
    role_codes = {c["code"].upper() for c in code_lists.get("role", [])}
    if not roles:
        errors_.append(FieldError(
            "pack_metadata.audience_roles", "at least one role is required"))
    for i, r in enumerate(roles):
        if str(r).upper() not in role_codes:
            errors_.append(FieldError(
                f"pack_metadata.audience_roles[{i}]", f"unknown role code {r!r}"))

    grades = meta.get("school_grade_codes") or []
    grade_codes = {c["code"].upper() for c in code_lists.get("school_grade", [])}
    for i, g in enumerate(grades):
        if str(g).upper() not in grade_codes:
            errors_.append(FieldError(
                f"pack_metadata.school_grade_codes[{i}]", f"unknown school_grade code {g!r}"))

    # ISO shape first: an unparsable date would otherwise travel all the way to
    # the gateway and come back as an opaque UPSTREAM_ERROR (CONTRACT.md §2.4a).
    dates: dict[str, _dt.date | None] = {}
    for fname in ("valid_from", "valid_to"):
        raw = meta.get(fname)
        if raw in (None, ""):
            dates[fname] = None
            continue
        try:
            dates[fname] = _dt.date.fromisoformat(str(raw))
        except ValueError:
            dates[fname] = None
            errors_.append(FieldError(
                f"pack_metadata.{fname}", f"expected an ISO date (YYYY-MM-DD), got {raw!r}"))
    if dates["valid_from"] and dates["valid_to"] and dates["valid_from"] > dates["valid_to"]:
        errors_.append(FieldError(
            "pack_metadata.valid_from",
            f"valid_from {meta.get('valid_from')!r} is after valid_to {meta.get('valid_to')!r}"))

    return errors_


def find_duplicate_entry_keys(entry_keys: list[str], *, file: str | None = None,
                               rows: list[int | None] | None = None) -> list[FieldError]:
    seen: set[str] = set()
    errors = []
    for i, k in enumerate(entry_keys):
        if not k:
            continue
        if k in seen:
            row = rows[i] if rows else None
            errors.append(FieldError("entry_key", f"duplicate entry_key {k!r} in this pack",
                                      file=file, row=row))
        seen.add(k)
    return errors
