"""File-based state for previews, operations and exports (CONTRACT.md §1, §6).

Not a second database of published QA data — a short-lived operational store
for content the portal has already validated (previews) and for publish
correlation (operations), plus generated export blobs. Lives in the named
volume `qa_admin_state`, outside the static frontend directory.

Single-instance assumption applies here too (see state/lock.py): writes are
atomic per-file (write temp + os.replace) but there is no cross-process
coordination beyond the pack lock.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class StateStore:
    def __init__(self, base_dir: str | Path):
        self._base = Path(base_dir)
        for sub in ("previews", "operations", "exports", "locks"):
            (self._base / sub).mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        return self._base

    # ── generic JSON record helpers ─────────────────────────────────────
    def _path(self, namespace: str, record_id: str) -> Path:
        return self._base / namespace / f"{record_id}.json"

    def save(self, namespace: str, record_id: str, record: dict) -> None:
        path = self._path(namespace, record_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def get(self, namespace: str, record_id: str) -> dict | None:
        path = self._path(namespace, record_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def delete(self, namespace: str, record_id: str) -> None:
        try:
            self._path(namespace, record_id).unlink()
        except FileNotFoundError:
            pass

    def list_ids(self, namespace: str) -> list[str]:
        return [p.stem for p in (self._base / namespace).glob("*.json")]

    # ── previews ─────────────────────────────────────────────────────────
    def save_preview(self, preview_id: str, record: dict) -> None:
        self.save("previews", preview_id, record)

    def get_preview(self, preview_id: str) -> dict | None:
        return self.get("previews", preview_id)

    # ── operations ───────────────────────────────────────────────────────
    def save_operation(self, operation_id: str, record: dict) -> None:
        self.save("operations", operation_id, record)

    def get_operation(self, operation_id: str) -> dict | None:
        return self.get("operations", operation_id)

    def list_operation_ids(self) -> list[str]:
        return self.list_ids("operations")

    # ── exports ──────────────────────────────────────────────────────────
    def save_export_meta(self, export_id: str, record: dict) -> None:
        self.save("exports", export_id, record)

    def get_export_meta(self, export_id: str) -> dict | None:
        return self.get("exports", export_id)

    def save_export_blob(self, export_id: str, content: bytes, ext: str) -> None:
        path = self._base / "exports" / f"{export_id}.{ext}"
        path.write_bytes(content)

    def read_export_blob(self, export_id: str, ext: str) -> bytes | None:
        path = self._base / "exports" / f"{export_id}.{ext}"
        if not path.exists():
            return None
        return path.read_bytes()
