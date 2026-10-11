#!/usr/bin/env python3
"""Emit approved organization metadata SQL for an EMPTY, isolated clean reference.

This tool does not connect to a database. Pipe its output to psql only on your
own reference, then use gen_clean_install.sh --seed-only to regenerate the dump.
The created_at value is a fixed seed timestamp, never a production timestamp.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "postgres" / "seed" / "clean_organizations.json"
FIELDS = ("legal_entity_code", "parent_code", "display_name", "kind", "level", "grade_levels", "is_active")
ENVELOPE = {"format", "source_sha", "source_rows_sha256", "date", "seed_created_at", "deactivate_codes", "rows"}
CODE = re.compile(r"[A-Z][A-Z0-9_]*")
LEVELS = {"MS", "ZS", "SS"}


def rows_hash(rows: list[dict]) -> str:
    # Preserve the approved export's field and row order; exclude envelope data.
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate(data: dict) -> None:
    if set(data) != ENVELOPE or data["format"] != 1:
        raise ValueError("unsupported organization source envelope")
    if not re.fullmatch(r"[0-9a-f]{40}", data["source_sha"]):
        raise ValueError("source_sha must identify the approved source commit")
    if not re.fullmatch(r"[0-9a-f]{64}", data["source_rows_sha256"]):
        raise ValueError("invalid source_rows_sha256")
    datetime.strptime(data["date"], "%Y-%m-%d")
    stamp = datetime.fromisoformat(data["seed_created_at"])
    if stamp.tzinfo != timezone.utc:
        raise ValueError("seed_created_at must be an explicit UTC seed timestamp")
    rows = data["rows"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("rows must be a nonempty list")
    codes = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(FIELDS):
            raise ValueError("organization rows allow exactly the seven approved metadata fields")
        code = row["legal_entity_code"]
        if not isinstance(code, str) or not CODE.fullmatch(code):
            raise ValueError("invalid legal_entity_code")
        codes.append(code)
        if not isinstance(row["display_name"], str) or not row["display_name"].strip():
            raise ValueError("display_name must be nonempty")
        if row["kind"] not in {"legal_person", "unit"} or row["level"] not in LEVELS | {None}:
            raise ValueError("invalid kind or level")
        parent = row["parent_code"]
        if (row["kind"] == "unit") != (parent is not None):
            raise ValueError("units require a parent; legal persons cannot have one")
        if parent is not None and (not isinstance(parent, str) or not CODE.fullmatch(parent)):
            raise ValueError("invalid parent_code")
        if row["is_active"] is not True:
            raise ValueError("approved organizations must be active")
        grades = row["grade_levels"]
        if grades is not None:
            if not isinstance(grades, dict) or not grades or not set(grades) <= LEVELS:
                raise ValueError("invalid grade_levels map")
            for values in grades.values():
                if (not isinstance(values, list) or not values or
                    any(not isinstance(v, str) or not v.isdigit() for v in values) or
                    len(values) != len(set(values))):
                    raise ValueError("grade_levels must contain unique numeric string codes")
    if len(codes) != len(set(codes)):
        raise ValueError("duplicate legal_entity_code")
    owners = {row["legal_entity_code"]: row for row in rows}
    for row in rows:
        if row["parent_code"] is not None:
            parent = owners.get(row["parent_code"])
            if parent is None or parent["kind"] != "legal_person":
                raise ValueError("parent must be a legal person in the approved source")
    demos = data["deactivate_codes"]
    if (not isinstance(demos, list) or not demos or
        any(not isinstance(c, str) or not CODE.fullmatch(c) for c in demos) or
        len(demos) != len(set(demos)) or set(demos) & set(codes)):
        raise ValueError("invalid or overlapping deactivate_codes")
    if rows_hash(rows) != data["source_rows_sha256"]:
        raise ValueError("approved organization rows checksum differs")


def literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return literal(json.dumps(value, ensure_ascii=False, sort_keys=True)) + "::jsonb"
    return "'" + value.replace("'", "''") + "'"


def render(data: dict) -> str:
    validate(data)
    rows = sorted(data["rows"], key=lambda row: (row["parent_code"] is not None, row["legal_entity_code"]))
    demos = ", ".join(literal(c) for c in sorted(data["deactivate_codes"]))
    allowed = ", ".join(literal(c) for c in sorted(data["deactivate_codes"] + [r["legal_entity_code"] for r in rows]))
    columns = ", ".join(FIELDS)
    values = ",\n".join("  (" + ", ".join(literal(row[c]) for c in FIELDS) + ")" for row in rows)
    target_fields = ", ".join("t." + c for c in FIELDS)
    input_fields = ", ".join("i." + c for c in FIELDS)
    stamp = literal(data["seed_created_at"]) + "::timestamptz"
    return f"""-- Generated by scripts/seed_clean_organizations.py; do not hand-edit.
-- Approved public metadata source: {data['source_sha']} ({data['date']}).
-- created_at is a deterministic seed value, not imported production metadata.
-- Apply only to an empty, isolated reference built from init_clean.
BEGIN;
SET LOCAL client_encoding = 'UTF8';
SET LOCAL standard_conforming_strings = on;
CREATE TEMP TABLE clean_org_input ON COMMIT DROP AS
SELECT {columns} FROM edu_ai.legal_entity WITH NO DATA;
INSERT INTO clean_org_input ({columns}) VALUES
{values};
CREATE TEMP TABLE clean_org_demo_before ON COMMIT DROP AS
SELECT * FROM edu_ai.legal_entity WHERE legal_entity_code IN ({demos});
DO $clean_organizations$
BEGIN
  IF (SELECT count(*) FROM edu_ai.document_metadata)
     + (SELECT count(*) FROM edu_ai.qa_entries)
     + (SELECT count(*) FROM edu_ai.question_answer_log) <> 0 THEN
    RAISE EXCEPTION 'organization seed requires an empty isolated reference';
  END IF;
  IF (SELECT count(*) FROM clean_org_demo_before) <> {len(data['deactivate_codes'])} OR
     EXISTS (SELECT 1 FROM edu_ai.legal_entity WHERE legal_entity_code NOT IN ({allowed})) THEN
    RAISE EXCEPTION 'reference organization set differs from the clean seed';
  END IF;
  IF EXISTS (
    SELECT 1 FROM edu_ai.legal_entity t JOIN clean_org_input i USING (legal_entity_code)
    WHERE ROW({target_fields}) IS DISTINCT FROM ROW({input_fields})
       OR t.created_at IS DISTINCT FROM {stamp}
       OR t.ico IS NOT NULL OR t.domains IS NOT NULL OR t.notes IS NOT NULL
       OR t.red_izo IS NOT NULL OR t.units IS NOT NULL
  ) THEN
    RAISE EXCEPTION 'existing approved organization differs; refusing to overwrite it';
  END IF;
END
$clean_organizations$;
INSERT INTO edu_ai.legal_entity ({columns}, created_at)
SELECT {columns}, {stamp} FROM clean_org_input
ORDER BY parent_code NULLS FIRST, legal_entity_code
ON CONFLICT (legal_entity_code) DO NOTHING;
UPDATE edu_ai.legal_entity SET is_active = false
WHERE legal_entity_code IN ({demos}) AND is_active;
DO $clean_organizations$
BEGIN
  IF (SELECT count(*) FROM edu_ai.legal_entity WHERE is_active) <> {len(rows)} OR
     EXISTS (
       SELECT 1 FROM clean_org_input i LEFT JOIN edu_ai.legal_entity t USING (legal_entity_code)
       WHERE ROW({target_fields}) IS DISTINCT FROM ROW({input_fields})
          OR t.created_at IS DISTINCT FROM {stamp}
          OR t.ico IS NOT NULL OR t.domains IS NOT NULL OR t.notes IS NOT NULL
          OR t.red_izo IS NOT NULL OR t.units IS NOT NULL
     ) THEN
    RAISE EXCEPTION 'approved active organization metadata verification failed';
  END IF;
  IF EXISTS (
    SELECT 1 FROM clean_org_demo_before b LEFT JOIN edu_ai.legal_entity t USING (legal_entity_code)
    WHERE t.is_active IS DISTINCT FROM false
       OR (to_jsonb(t) - 'is_active') IS DISTINCT FROM (to_jsonb(b) - 'is_active')
  ) THEN
    RAISE EXCEPTION 'synthetic metadata changed beyond is_active';
  END IF;
END
$clean_organizations$;
COMMIT;
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    args = parser.parse_args()
    try:
        sql = render(json.loads(args.source.read_text(encoding="utf-8")))
    except (ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stdout.write(sql)


if __name__ == "__main__":
    main()
