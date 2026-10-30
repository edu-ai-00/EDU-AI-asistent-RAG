"""Read a QA set: a workbook (.xlsx, standard library only) or a QA pack CSV.

The lab tool runs inside the edu_ai_search image, which has no openpyxl, and a
new dependency for one reader is not worth it: an .xlsx is a zip of XML parts.
Only what a QA sheet needs is read — shared strings, inline strings and plain
values of the first sheet (or a named one). Formulas, styles and dates are not
interpreted.

Two workbook layouts are recognised by their header row:

  * QA pack   canonical_q | answer | sources | Alternativa 1..n
              (a QA pack workbook — the shape the QA pack upload expects)
  * loose     question in column A, answer in column G
              (a free sheet of questions and answers)

A .csv is read as the application reads a QA pack (the QAJsonReader component
of langflow_flows/ingestion/EDU_AI_QA.json, contracts/qa-portal/CONTRACT.md
5.1): UTF-8 with or without BOM, delimiter `,` or `;` chosen by which one makes
the header a known QA header, the same column aliases, variants and sources one
per line. The columns the review does not touch (entry_key, cat, category_code,
domains, is_active) are kept in item["pack"] so an export can hand them back
unchanged — a curator's round trip must not drop a pack's categories or areas.
"""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
import zipfile

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_T = "{%s}t" % _NS["m"]


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _sheet_paths(z: zipfile.ZipFile) -> dict[str, str]:
    """{sheet name: part path}, resolved through the workbook relationships."""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = {r.get("Id"): r.get("Target") for r in rels}
    out = {}
    for s in wb.find("m:sheets", _NS):
        t = target.get(s.get("{%s}id" % _NS["r"]), "")
        t = t.lstrip("/")
        out[s.get("name")] = t if t.startswith("xl/") else "xl/" + t
    return out


def read_rows(path: str, sheet: str | None = None) -> list[list[str]]:
    """All non-empty rows of one sheet as lists of strings ('' for a gap)."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(_T)))
        paths = _sheet_paths(z)
        if sheet is None:
            part = next(iter(paths.values()))
        elif sheet in paths:
            part = paths[sheet]
        else:
            raise KeyError(f"sheet {sheet!r} not found; sheets: {', '.join(paths)}")
        rows = []
        for r in ET.fromstring(z.read(part)).iter("{%s}row" % _NS["m"]):
            cells: dict[int, str] = {}
            for c in r.findall("m:c", _NS):
                kind = c.get("t")
                if kind == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter(_T))
                else:
                    v = c.find("m:v", _NS)
                    if v is None or v.text is None:
                        continue
                    val = shared[int(v.text)] if kind == "s" else v.text
                cells[_col_index(c.get("r"))] = val
            if any((v or "").strip() for v in cells.values()):
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
        return rows


def _cell(row: list[str], i: int) -> str:
    return (row[i] if i < len(row) else "").strip()


# The application's QA CSV header aliases (QAJsonReader), lowercase.
_CSV_ALIASES = {
    "canonical_q": ("q", "question", "canonical_q"),
    "answer": ("a", "answer", "answer_md"),
    "cat": ("cat", "category"),
    "category_code": ("category_code",),
    "variants": ("variants", "phrasings", "alt", "alt_questions"),
    "sources": ("sources", "zdroje", "asset_refs"),
    "entry_key": ("entry_key",),
    "is_active": ("is_active",),
    "domains": ("domains",),
}


def _csv_columns(header: list[str]) -> dict[str, int] | None:
    """{field: column index} when the header is a QA header, else None."""
    h = [c.strip().lower() for c in header]
    cols = {}
    for field, names in _CSV_ALIASES.items():
        idx = next((i for i, c in enumerate(h) if c in names), None)
        if idx is not None:
            cols[field] = idx
    return cols if "canonical_q" in cols and "answer" in cols else None


def load_csv(path: str) -> list[dict]:
    """QA items from a QA pack CSV, with the untouched columns in item["pack"].

    The delimiter is the one with which the header parses as a QA header —
    deterministic, like the portal, never a guess by character frequency.
    """
    with open(path, encoding="utf-8-sig", newline="") as fh:
        text = fh.read()
    for delim in (",", ";"):
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        cols = _csv_columns(rows[0]) if rows else None
        if cols:
            break
    else:
        raise ValueError("unexpected header: not a QA pack CSV "
                         "(needs canonical_q/question/q and answer/answer_md/a)")
    has_code = "category_code" in cols
    items = []
    for n, row in enumerate(rows[1:], 1):
        get = lambda f: (row[cols[f]] if f in cols and cols[f] < len(row) else "").strip()  # noqa: E731
        q, answer = get("canonical_q"), get("answer")
        if not q or not answer:          # the application refuses such a row too
            continue
        items.append({
            "id": f"{n:03d}",
            "canonical_q": q,
            "alternatives": [v.strip() for v in get("variants").splitlines() if v.strip()],
            "answer": answer,
            "sources": get("sources"),
            "pack": {"entry_key": get("entry_key"), "cat": get("cat"),
                     "category_code": get("category_code") if has_code else None,
                     "domains": get("domains"), "is_active": get("is_active")},
        })
    return items


def load_items(path: str, sheet: str | None = None) -> list[dict]:
    """QA items: {id, canonical_q, alternatives[], answer, sources[, pack]}.

    Rows without an answer are skipped — there is nothing to neutralise.
    """
    if str(path).lower().endswith(".csv"):
        return load_csv(path)
    rows = read_rows(path, sheet)
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    items = []
    if "canonical_q" in header and "answer" in header:
        qi, ai = header.index("canonical_q"), header.index("answer")
        si = header.index("sources") if "sources" in header else None
        alt = [i for i, h in enumerate(header) if h.startswith("alternativ")]
        for n, row in enumerate(rows[1:], 1):
            answer = _cell(row, ai)
            if not answer:
                continue
            items.append({
                "id": f"{n:03d}",
                "canonical_q": _cell(row, qi),
                "alternatives": [a for a in (_cell(row, i) for i in alt) if a],
                "answer": answer,
                # some cells carry the CSV quoting of an earlier export ("…")
                "sources": (_cell(row, si) if si is not None else "").strip('"').strip(),
            })
    else:
        for n, row in enumerate(rows, 1):
            q, answer = _cell(row, 0), _cell(row, 6)
            if not q or not answer or q.startswith("http"):
                continue
            items.append({"id": f"{n:03d}", "canonical_q": q, "alternatives": [],
                          "answer": answer, "sources": ""})
    return items
