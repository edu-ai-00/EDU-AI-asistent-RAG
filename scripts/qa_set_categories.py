#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-09-04).
"""Set qa_entries.tags->>'category' for one pack, from a pack CSV.

    docker compose run --rm -v /opt/edu_ai/scripts:/app/scripts \
        -v /opt/edu_ai/QA:/app/QA --entrypoint python edu_ai_search \
        scripts/qa_set_categories.py QA/<pack>.csv --pack faq-jpz [--go]

Why not a re-ingest. The category is the ONLY field changing, and /v1/ingest takes
the whole scoping as form fields — entity, visibility, roles, ročník set, level,
domains. Getting one of them wrong silently rewrites who can see 110 curated
answers, and `faq-jpz` on production is `organization` + a ročník set + domains,
which no manifest in this repo carries. So: touch the one field, leave the rest
of the row alone, and keep the CSV as the file to re-ingest FROM later.

Matched on canonical_q, not on entry_key: the key is derived at ingest and a CSV
written by hand has it empty. A question that does not match is REPORTED, never
guessed at — a near-match here would tag the wrong answer.

Dry run by default; --go is the only thing that writes.
"""
from __future__ import annotations

import argparse
import csv
import io
import sys

sys.path.insert(0, "/app")

from app.db import get_cursor  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--pack", required=True)
    ap.add_argument("--go", action="store_true", help="write (default: dry run)")
    a = ap.parse_args(argv)

    rows = list(csv.DictReader(io.open(a.csv_path, encoding="utf-8-sig"), delimiter=","))
    want = {}
    for r in rows:
        q, cat = (r.get("canonical_q") or "").strip(), (r.get("cat") or "").strip()
        if q and cat:
            want[q] = cat
    print(f"{a.csv_path}: {len(rows)} řádků, {len(want)} s kategorií")

    with get_cursor() as cur:
        cur.execute(
            "SELECT entry_id::text, canonical_q, coalesce(tags->>'category', '') "
            "FROM edu_ai.qa_entries WHERE pack_id = %s", (a.pack,))
        db = cur.fetchall()
        print(f"pack '{a.pack}': {len(db)} entry v DB")

        by_q = {(q or "").strip(): (eid, cur_cat) for eid, q, cur_cat in db}
        missing = [q for q in want if q not in by_q]
        untouched = [q for q in by_q if q not in want]
        changes = [(by_q[q][0], q, by_q[q][1], c) for q, c in want.items()
                   if q in by_q and by_q[q][1] != c]

        if missing:
            print(f"\nV DB NENALEZENO ({len(missing)}) — nic se u nich nezmění:")
            for q in missing[:10]:
                print(f"    {q}")
        if untouched:
            print(f"\nV CSV CHYBÍ ({len(untouched)}) — zůstanou, jak jsou:")
            for q in untouched[:10]:
                print(f"    {q}")
        print(f"\nke změně: {len(changes)} z {len(db)}")
        for _eid, q, old, new in changes[:5]:
            print(f"    {old!r} -> {new!r}   {q[:52]}")
        if len(changes) > 5:
            print(f"    … a dalších {len(changes) - 5}")

        if not a.go:
            print("\nDRY RUN — nic nezapsáno. Spusť s --go.")
            return 0
        for eid, _q, _old, new in changes:
            cur.execute(
                "UPDATE edu_ai.qa_entries "
                "SET tags = jsonb_set(coalesce(tags, '{}'::jsonb), '{category}', to_jsonb(%s::text)) "
                "WHERE entry_id = %s", (new, eid))
        print(f"\nZAPSÁNO: {len(changes)} entry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
