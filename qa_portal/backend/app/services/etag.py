"""The pack_etag recipe from CONTRACT.md §3.

Authoritative computation lives in the search service (one owner, so it
cannot drift from a second implementation here). This module exists so
tests can build a self-consistent fake search backend without duplicating
the recipe by hand in every test file.
"""
from __future__ import annotations

import hashlib
import json


def _canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_pack_etag(version: int, built_at: str | None, entries: list[dict]) -> str:
    normalized = []
    for e in entries:
        normalized.append({
            "entry_key": e.get("entry_key"),
            "entry_id": e.get("entry_id"),
            "canonical_q": e.get("canonical_q"),
            "answer_md": e.get("answer_md"),
            "category_code": e.get("category_code"),
            "domains": sorted(e.get("domains") or []),
            "variants": sorted(e.get("variants") or []),
            "sources": e.get("sources") or [],
            "is_active": bool(e.get("is_active", True)),
        })
    normalized.sort(key=lambda e: (e["entry_key"] or ""))
    payload = {"version": version, "built_at": built_at, "entries": normalized}
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
