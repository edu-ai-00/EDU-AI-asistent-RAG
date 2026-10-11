"""Cross-component CSV round trip: search/app/qa_csv.py (export writer) vs.
qa_portal/backend/app/services/csv_io.py (portal writer + universal reader).

Both trees ship a top-level package literally named `app` (search/app and
qa_portal/backend/app), so they cannot both sit on sys.path at once — putting
`search/` on sys.path next to `qa_portal/backend` makes one of the two `app`
imports silently resolve to the wrong tree. qa_csv.py is therefore loaded by
file path via importlib, and only qa_portal/backend goes on sys.path for the
normal `from app.services.csv_io import ...` that csv_io's own package
imports (validation.py, FieldError) rely on.

CONTRACT.md §5.1 (v1.4.0): every writer now emits `,`; every reader accepts
both `,` and `;` via deterministic header-based delimiter detection, so old
semicolon packs stay importable without conversion.
"""
from __future__ import annotations

import csv
import io
import json
import sys
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# --- load qa_csv.py by path (no sys.path collision with qa_portal/backend/app) ---
_qa_csv_path = REPO_ROOT / "search" / "app" / "qa_csv.py"
_spec = importlib.util.spec_from_file_location("edu_ai_search_qa_csv", _qa_csv_path)
qa_csv = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(qa_csv)

# --- put qa_portal/backend on sys.path for csv_io's own package imports ---
_backend_root = str(REPO_ROOT / "qa_portal" / "backend")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

from app.services.csv_io import (  # noqa: E402
    HEADER_LEGACY,
    HEADER_NEW,
    parse_pack_csv,
    write_pack_csv,
)

HARD_ENTRY = {
    "entry_key": "qk1",
    "canonical_q": "Jaká je otázka?",
    "answer_md": (
        'Řádek jedna, se "čárkou" a se; středníkem a s "uvozovkami".\n'
        "Druhý řádek odpovědi, s další, čárkou; a středníkem."
    ),
    "cat": "",
    "variants": ["Varianta A", "Varianta B, s čárkou; a středníkem"],
    "sources": [
        {"title": "Web", "url": "https://example.com/a"},
        {"title": "Doc", "url": "https://example.com/b"},
    ],
    "is_active": True,
    "domains": ["SKOLNI_JIDELNA", "DOPRAVA"],
    "category_code": "PROVOZ",
}


def _sources_cell(entry: dict) -> str:
    """Mirrors search/app/server.py::_sources_cell / csv_io._build_source_cell
    exactly — 'Title | url', title omitted when it duplicates the url."""
    lines = []
    for item in entry.get("sources") or []:
        url = (item.get("url") or "").strip()
        title = (item.get("title") or "").strip()
        if url and title and title != url:
            lines.append(f"{title} | {url}")
        elif url or title:
            lines.append(url or title)
    return "\n".join(lines)


def _write_with_csv_writer(entries: list[dict], delimiter: str, *,
                            include_category_column: bool) -> bytes:
    """Independent fixture builder — does not call write_pack_csv or
    rows_to_csv_bytes — proving both readers accept a genuinely third-party
    file, e.g. an old export nobody generated with today's code."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL,
                         lineterminator="\r\n")
    header = HEADER_NEW if include_category_column else HEADER_LEGACY
    writer.writerow(header)
    for e in entries:
        row = [
            e.get("entry_key") or "",
            e.get("canonical_q") or "",
            e.get("answer_md") or "",
            e.get("cat") or "",
            "\n".join(e.get("variants") or []),
            _sources_cell(e),
            "TRUE" if e.get("is_active", True) else "FALSE",
            json.dumps(e.get("domains") or [], ensure_ascii=False),
        ]
        if include_category_column:
            row.append(e.get("category_code") or "")
        writer.writerow(row)
    return buf.getvalue().encode("utf-8-sig")


def _assert_row_matches_entry(row, entry: dict, *, expect_category) -> None:
    assert row.entry_key == entry["entry_key"]
    assert row.canonical_q == entry["canonical_q"]
    assert row.answer_md == entry["answer_md"]
    assert row.variants == entry["variants"]
    assert row.sources == entry["sources"]
    assert row.is_active == entry["is_active"]
    assert row.domains == entry["domains"]
    assert row.category_code == expect_category


# ---------------------------------------------------------------------------
# search/app/qa_csv.py (export writer) -> csv_io.parse_pack_csv (portal reader)
# ---------------------------------------------------------------------------

def test_search_export_bytes_round_trip_through_portal_reader():
    """rows_to_csv_bytes' 8-column legacy-shaped output, with the hardest
    single cell (comma AND semicolon AND a quote AND a newline in answer_md,
    a 'Title | url' sources cell and a non-empty domains JSON array), reads
    back losslessly through the portal's universal reader."""
    export_row = (
        HARD_ENTRY["entry_key"],
        HARD_ENTRY["canonical_q"],
        HARD_ENTRY["answer_md"],
        HARD_ENTRY["cat"],
        "\n".join(HARD_ENTRY["variants"]),
        _sources_cell(HARD_ENTRY),
        json.dumps(HARD_ENTRY["domains"], ensure_ascii=False),
    )

    raw = qa_csv.rows_to_csv_bytes([export_row])

    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    first_line = raw[len(b"\xef\xbb\xbf"):].split(b"\r\n", 1)[0]
    assert first_line == b",".join(h.encode() for h in qa_csv.HEADER)

    parsed = parse_pack_csv(raw, filename="export.csv")
    assert parsed.errors == []
    assert parsed.has_category_column is False
    assert len(parsed.rows) == 1
    # rows_to_csv_bytes always writes TRUE regardless of entry is_active,
    # per its own docstring ("everything exported here is active").
    row = parsed.rows[0]
    from app.services.csv_io import UNSPECIFIED_CATEGORY
    assert row.is_active is True
    assert row.canonical_q == HARD_ENTRY["canonical_q"]
    assert row.answer_md == HARD_ENTRY["answer_md"]
    assert row.variants == HARD_ENTRY["variants"]
    assert row.sources == HARD_ENTRY["sources"]
    assert row.domains == HARD_ENTRY["domains"]
    assert row.category_code is UNSPECIFIED_CATEGORY


def test_portal_writer_9_column_round_trip_including_category_code():
    raw = write_pack_csv([HARD_ENTRY], include_category_column=True)

    assert raw.startswith(b"\xef\xbb\xbf")
    parsed = parse_pack_csv(raw, filename="pack.csv")
    assert parsed.errors == []
    assert parsed.has_category_column is True
    assert len(parsed.rows) == 1
    _assert_row_matches_entry(parsed.rows[0], HARD_ENTRY,
                               expect_category=HARD_ENTRY["category_code"])


# ---------------------------------------------------------------------------
# old semicolon packs — same logical rows, ';' delimiter, both header shapes
# ---------------------------------------------------------------------------

def test_semicolon_8_column_pack_still_parses_with_zero_errors():
    raw = _write_with_csv_writer([HARD_ENTRY], ";", include_category_column=False)

    parsed = parse_pack_csv(raw, filename="old_pack.csv")
    assert parsed.errors == []
    assert parsed.has_category_column is False
    assert len(parsed.rows) == 1
    from app.services.csv_io import UNSPECIFIED_CATEGORY
    _assert_row_matches_entry(parsed.rows[0], HARD_ENTRY,
                               expect_category=UNSPECIFIED_CATEGORY)


def test_semicolon_9_column_pack_still_parses_with_zero_errors():
    raw = _write_with_csv_writer([HARD_ENTRY], ";", include_category_column=True)

    parsed = parse_pack_csv(raw, filename="old_pack.csv")
    assert parsed.errors == []
    assert parsed.has_category_column is True
    assert len(parsed.rows) == 1
    _assert_row_matches_entry(parsed.rows[0], HARD_ENTRY,
                               expect_category=HARD_ENTRY["category_code"])


# ---------------------------------------------------------------------------
# failure: neither known header shape
# ---------------------------------------------------------------------------

def test_unrecognised_header_yields_one_error_and_no_rows():
    raw = "col_a,col_b,col_c\r\nx,y,z\r\n".encode("utf-8-sig")

    parsed = parse_pack_csv(raw, filename="bad.csv")

    assert len(parsed.errors) == 1
    assert parsed.rows == []
    err = parsed.errors[0]
    assert err.field == "file"
    assert "unexpected header" in err.message
