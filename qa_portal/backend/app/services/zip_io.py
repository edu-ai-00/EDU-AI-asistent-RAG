"""ZIP export/import container — manifest.json + packs/<pack_id>.csv
(CONTRACT.md §5.2). Never contains passwords, tokens or a user_id.
"""
from __future__ import annotations

import io
import json
import zipfile


class BadZip(Exception):
    """Not a well-formed portal ZIP (missing/invalid manifest.json)."""


def parse_import_zip(raw: bytes) -> tuple[dict, dict[str, bytes]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as e:
        raise BadZip(str(e)) from e
    names = zf.namelist()
    if "manifest.json" not in names:
        raise BadZip("manifest.json missing from ZIP")
    try:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise BadZip(f"manifest.json is not valid JSON: {e}") from e
    files = {n: zf.read(n) for n in names
             if n.startswith("packs/") and n.endswith(".csv")}
    return manifest, files


def build_export_zip(manifest: dict, pack_csvs: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for pack_id, csv_bytes in pack_csvs.items():
            zf.writestr(f"packs/{pack_id}.csv", csv_bytes)
    return buf.getvalue()
