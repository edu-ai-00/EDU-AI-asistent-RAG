"""Delimiter behaviour of csv_io (CONTRACT.md §5.1 "Oddělovač", v1.4.0):
write always uses ",", read accepts "," and ";" via deterministic header
detection. Pure unit tests against csv_io directly — no HTTP, no mocks.
"""
from __future__ import annotations

import csv
import io
import json

from app.services.csv_io import (
    HEADER_LEGACY,
    HEADER_NEW,
    UNSPECIFIED_CATEGORY,
    parse_pack_csv,
    write_pack_csv,
)

HARD_ENTRY = {
    "entry_key": "qk1",
    "canonical_q": "Jaká je otázka?",
    "answer_md": 'Řádek jedna, se "čárkou" a se; středníkem.\nDruhý řádek odpovědi.',
    "cat": "",
    "variants": ["Varianta A", "Varianta B, s čárkou"],
    "sources": [
        {"title": "Web", "url": "https://example.com/a"},
        {"title": "Doc", "url": "https://example.com/b"},
    ],
    "is_active": True,
    "domains": ["SKOLNI_JIDELNA", "DOPRAVA"],
    "category_code": "PROVOZ",
}


def _write_with_delimiter(entries: list[dict], delimiter: str, *,
                           include_category_column: bool = True) -> bytes:
    """Independent fixture builder (does NOT reuse write_pack_csv's own
    delimiter) — proves the ';' read path works against packs csv_io itself
    never writes, e.g. genuinely old exports."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL,
                         lineterminator="\r\n")
    header = HEADER_NEW if include_category_column else HEADER_LEGACY
    writer.writerow(header)
    for e in entries:
        sources_cell = "\n".join(
            f"{s['title']} | {s['url']}" if s.get("title") and s.get("url")
            and s["title"] != s["url"] else (s.get("url") or s.get("title") or "")
            for s in e.get("sources") or []
        )
        row = [
            e.get("entry_key") or "",
            e.get("canonical_q") or "",
            e.get("answer_md") or "",
            e.get("cat") or "",
            "\n".join(e.get("variants") or []),
            sources_cell,
            "TRUE" if e.get("is_active", True) else "FALSE",
            json.dumps(e.get("domains") or [], ensure_ascii=False),
        ]
        if include_category_column:
            row.append(e.get("category_code") or "")
        writer.writerow(row)
    return buf.getvalue().encode("utf-8-sig")


def _assert_row_matches_entry(row, entry: dict) -> None:
    assert row.entry_key == entry["entry_key"]
    assert row.canonical_q == entry["canonical_q"]
    assert row.answer_md == entry["answer_md"]
    assert row.variants == entry["variants"]
    assert row.sources == entry["sources"]
    assert row.is_active == entry["is_active"]
    assert row.domains == entry["domains"]
    assert row.category_code == entry["category_code"]


def test_write_pack_csv_uses_comma_and_round_trips_hard_cases():
    raw = write_pack_csv([HARD_ENTRY], include_category_column=True)

    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    first_line = raw[len(b"\xef\xbb\xbf"):].split(b"\r\n", 1)[0]
    assert first_line == (
        b"entry_key,canonical_q,answer_md,cat,variants,sources,is_active,"
        b"domains,category_code")

    parsed = parse_pack_csv(raw, filename="pack.csv")
    assert parsed.errors == []
    assert parsed.has_category_column is True
    assert len(parsed.rows) == 1
    _assert_row_matches_entry(parsed.rows[0], HARD_ENTRY)


def test_semicolon_pack_parses_identically_old_packs_still_import():
    raw = _write_with_delimiter([HARD_ENTRY], ";", include_category_column=True)

    parsed = parse_pack_csv(raw, filename="pack.csv")
    assert parsed.errors == []
    assert parsed.has_category_column is True
    assert len(parsed.rows) == 1
    _assert_row_matches_entry(parsed.rows[0], HARD_ENTRY)


def test_legacy_header_parses_in_both_delimiters():
    legacy_entry = dict(HARD_ENTRY)
    raw_comma = write_pack_csv([legacy_entry], include_category_column=False)
    raw_semi = _write_with_delimiter([legacy_entry], ";", include_category_column=False)

    for raw in (raw_comma, raw_semi):
        parsed = parse_pack_csv(raw, filename="pack.csv")
        assert parsed.errors == []
        assert parsed.has_category_column is False
        assert len(parsed.rows) == 1
        assert parsed.rows[0].category_code is UNSPECIFIED_CATEGORY


def test_unknown_header_is_a_single_field_error_with_no_rows():
    raw = "a,b,c\r\n1,2,3\r\n".encode("utf-8-sig")

    parsed = parse_pack_csv(raw, filename="bad.csv")

    assert len(parsed.errors) == 1
    assert parsed.rows == []
    err = parsed.errors[0]
    assert err.field == "file"
    assert "unexpected header" in err.message
