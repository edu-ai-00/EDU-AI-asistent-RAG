"""Request bodies — HTTP transport layer only. Mirrors
contracts/qa-portal/openapi.yaml; `extra="forbid"` matches the frozen
schemas' `additionalProperties: false`.

Response bodies are plain dicts built by the services layer, already shaped
to the contract's schemas — no second, drifting copy of those shapes here.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    url: str


class EntryWriteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_key: str | None = None
    canonical_q: str = Field(min_length=1)
    answer_md: str = Field(min_length=1)
    variants: list[str] = Field(default_factory=list)
    sources: list[SourceIn] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    category_code: str | None = None
    is_active: bool = True

    def to_dict(self) -> dict:
        d = self.model_dump()
        d["sources"] = [s if isinstance(s, dict) else s.model_dump() for s in self.sources]
        return d


class PackMetadataIn(BaseModel):
    """Mirrors openapi.yaml PackMetadataIn (CONTRACT.md §2.4a). Portal never
    invents these values — required to confirm a NEW pack's metadata,
    forbidden for an existing pack (which keeps reading its own state)."""
    model_config = ConfigDict(extra="forbid")
    visibility: str
    audience_roles: list[str] = Field(min_length=1)
    school_grade_codes: list[str] = Field(default_factory=list)
    valid_from: str | None = None
    valid_to: str | None = None


class PreviewPackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_etag: str
    entries: list[EntryWriteIn]
    pack_metadata: PackMetadataIn | None = None


class PublishPackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_id: str
    expected_etag: str


class PublishImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_etags: dict[str, str] = Field(default_factory=dict)
    confirm_replace: bool = False


class CreatePackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pack_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
    lang: str
    metadata: PackMetadataIn | None = None


class DeletePackRequest(BaseModel):
    """Body for DELETE .../packs/{pack} (CONTRACT.md §13.3) — the administrator
    must retype the pack_id; a mismatch is 409, checked before any I/O."""
    model_config = ConfigDict(extra="forbid")
    confirm_pack_id: str


class CreateCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,62}$")
    labels: dict[str, str]
    sort_order: int | None = None
    notes: str | None = None


class CreateExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pack_ids: list[str] = Field(min_length=1)
    format: str = "zip"
    lang: str | None = None
