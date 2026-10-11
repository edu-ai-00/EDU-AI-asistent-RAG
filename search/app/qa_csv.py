"""Byte format for the QA pack CSV export (/v1/qa-export).

Split out of server.py on purpose: this module has no DB and no FastAPI
import, so the export's exact bytes are unit-testable without the search
runtime — see tests/test_qa_export_csv.py. server.py still owns the SQL,
the 404 behaviour and _sources_cell; it only hands this module the rows.
"""
import csv
import io

# The legacy 8-column set: no category_code (see CONTRACT.md 5.1).
HEADER = ["entry_key", "canonical_q", "answer_md", "cat", "variants",
          "sources", "is_active", "domains"]


def rows_to_csv_bytes(rows) -> bytes:
    """Turn already-fetched export rows into CSV bytes.

    Each row is (entry_key, canonical_q, answer_md, cat, variants,
    sources_cell, domains_json) — sources_cell and domains_json are already
    formatted by the caller (server.py's _sources_cell and json.dumps).
    is_active is written as TRUE throughout — everything exported here is
    active by definition.
    """
    buf = io.StringIO()
    # A real writer, not string joining: answer_md routinely holds ',', ';',
    # quotes and line breaks, and each of those splits or truncates a
    # hand-built row.
    w = csv.writer(buf, delimiter=",", quoting=csv.QUOTE_MINIMAL,
                    lineterminator="\r\n")
    w.writerow(HEADER)
    for entry_key, canonical_q, answer_md, cat, variants, sources_cell, domains_json in rows:
        w.writerow([entry_key, canonical_q, answer_md, cat, variants,
                    sources_cell, "TRUE", domains_json])
    # utf-8-sig: without the BOM Excel reads the file as the system codepage
    # and every Czech letter comes out as mojibake, which is where a curator
    # gives up.
    return buf.getvalue().encode("utf-8-sig")
