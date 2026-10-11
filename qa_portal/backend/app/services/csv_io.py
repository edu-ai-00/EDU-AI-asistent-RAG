"""CSV parsing/generation for one QA pack — server-side only (CONTRACT.md §5).

Column order (new format, CONTRACT.md §5.1):
    entry_key,canonical_q,answer_md,cat,variants,sources,is_active,domains,category_code
UTF-8 with BOM, `,` delimiter, CRLF, RFC 4180 quoting on write (CONTRACT.md
§5.1 "Oddělovač", v1.4.0). Reads accept both `,` and `;` — the header row is
parsed with each delimiter in turn and whichever yields a known header
(HEADER_NEW or HEADER_LEGACY) wins; this keeps old semicolon packs importable
without conversion. search/app/server.py::qa_export is being switched to `,`
in the same fix pack, so both sides of the round trip stay lossless.
The legacy 8-column format (no category_code) is still accepted on import.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field

from .validation import FieldError

HEADER_NEW = ["entry_key", "canonical_q", "answer_md", "cat", "variants",
              "sources", "is_active", "domains", "category_code"]
HEADER_LEGACY = HEADER_NEW[:-1]

# Sentinels distinguishing "not sent" from "sent empty" on the CSV boundary
# (CONTRACT.md §5.1 for category_code; brief.md for domains inheritance).
INHERIT_DOMAINS = object()
UNSPECIFIED_CATEGORY = object()


@dataclass
class ParsedRow:
    row: int
    entry_key: str | None
    canonical_q: str
    answer_md: str
    cat_legacy: str
    variants: list[str]
    sources: list[dict]
    is_active: bool
    domains: object          # list[str] | INHERIT_DOMAINS
    category_code: object    # str | None | UNSPECIFIED_CATEGORY


@dataclass
class ParsedCsv:
    rows: list[ParsedRow] = field(default_factory=list)
    has_category_column: bool = False
    errors: list[FieldError] = field(default_factory=list)


def _parse_source_line(line: str) -> dict:
    """Inverse of _build_source_cell (best-effort — see module docstring for
    the one ambiguous case this shares with the existing exporter)."""
    line = line.strip()
    if not line:
        return None
    if " | " in line:
        title, url = line.split(" | ", 1)
        return {"title": title.strip(), "url": url.strip()}
    return {"title": line, "url": line}


def _parse_sources_cell(cell: str) -> list[dict]:
    if not cell:
        return []
    out = []
    for line in cell.split("\n"):
        parsed = _parse_source_line(line)
        if parsed:
            out.append(parsed)
    return out


def _build_source_cell(sources: list[dict]) -> str:
    """Exact inverse pairing with _parse_sources_cell; mirrors
    search/app/server.py::_sources_cell so a round trip is stable."""
    lines = []
    for item in sources or []:
        url = (item.get("url") or "").strip()
        title = (item.get("title") or "").strip()
        if url and title and title != url:
            lines.append(f"{title} | {url}")
        elif url or title:
            lines.append(url or title)
    return "\n".join(lines)


def _read_header(text: str, delimiter: str) -> list[str] | None:
    """Parse just the header row with the given delimiter; None if the file
    has no rows at all."""
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        return next(reader)
    except StopIteration:
        return None


def parse_pack_csv(raw: bytes, *, filename: str | None = None) -> ParsedCsv:
    result = ParsedCsv()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        result.errors.append(FieldError("file", "not valid UTF-8 text", file=filename, row=1))
        return result

    # Deterministic delimiter detection (CONTRACT.md §5.1): try `,` then `;`,
    # pick whichever parses the header into a known column set. Never guess
    # from character frequency (no csv.Sniffer).
    header_comma = _read_header(text, ",")
    if header_comma is None:
        result.errors.append(FieldError("file", "empty file", file=filename, row=1))
        return result
    header_semi = _read_header(text, ";")

    if header_comma in (HEADER_NEW, HEADER_LEGACY):
        delimiter, header = ",", header_comma
    elif header_semi in (HEADER_NEW, HEADER_LEGACY):
        delimiter, header = ";", header_semi
    else:
        result.errors.append(FieldError(
            "file", f"unexpected header {header_comma!r}", file=filename, row=1))
        return result

    result.has_category_column = header == HEADER_NEW

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    next(reader)  # header already validated above

    expected_len = len(header)
    for line_no, raw_row in enumerate(reader, start=2):
        if not raw_row or (len(raw_row) == 1 and raw_row[0] == ""):
            continue  # tolerate a trailing blank line
        if len(raw_row) != expected_len:
            result.errors.append(FieldError(
                "row", f"expected {expected_len} columns, got {len(raw_row)}",
                file=filename, row=line_no))
            continue
        cells = dict(zip(header, raw_row))

        entry_key = cells["entry_key"].strip() or None

        is_active_raw = cells["is_active"].strip().upper()
        if is_active_raw in ("TRUE", "1", "YES"):
            is_active = True
        elif is_active_raw in ("FALSE", "0", "NO", ""):
            is_active = is_active_raw != ""
        else:
            result.errors.append(FieldError(
                "is_active", f"not a boolean: {cells['is_active']!r}",
                file=filename, row=line_no))
            is_active = True

        domains_cell = cells["domains"].strip()
        if domains_cell == "":
            domains: object = INHERIT_DOMAINS
        else:
            try:
                parsed = json.loads(domains_cell)
                if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
                    raise ValueError("domains must be a JSON array of strings")
                domains = [d.strip().upper() for d in parsed if d.strip()]
            except (ValueError, TypeError):
                result.errors.append(FieldError(
                    "domains", f"not a JSON array of codes: {domains_cell!r}",
                    file=filename, row=line_no))
                domains = []

        if result.has_category_column:
            cat_cell = cells["category_code"].strip()
            category_code: object = cat_cell or None
        else:
            category_code = UNSPECIFIED_CATEGORY

        result.rows.append(ParsedRow(
            row=line_no, entry_key=entry_key,
            canonical_q=cells["canonical_q"], answer_md=cells["answer_md"],
            cat_legacy=cells["cat"],
            variants=[v for v in cells["variants"].split("\n") if v.strip()],
            sources=_parse_sources_cell(cells["sources"]),
            is_active=is_active, domains=domains, category_code=category_code,
        ))
    return result


def write_pack_csv(entries: list[dict], *, include_category_column: bool = True) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter=",", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    header = HEADER_NEW if include_category_column else HEADER_LEGACY
    writer.writerow(header)
    for e in entries:
        row = [
            e.get("entry_key") or "",
            e.get("canonical_q") or "",
            e.get("answer_md") or "",
            e.get("cat") or "",
            "\n".join(e.get("variants") or []),
            _build_source_cell(e.get("sources")),
            "TRUE" if e.get("is_active", True) else "FALSE",
            json.dumps(e.get("domains") or [], ensure_ascii=False),
        ]
        if include_category_column:
            row.append(e.get("category_code") or "")
        writer.writerow(row)
    return buf.getvalue().encode("utf-8-sig")
