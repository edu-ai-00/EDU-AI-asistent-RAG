#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-09-06).
"""Apply a CURATOR'S area decisions to existing content — reproducibly, reversibly.

    python3 scripts/domain_migration.py --plan plan.json                    # DRY RUN
    python3 scripts/domain_migration.py --plan plan.json --apply --go
    python3 scripts/domain_migration.py --plan plan.json --verify
    python3 scripts/domain_migration.py --rollback snapshot.json --go
    python3 scripts/domain_migration.py --plan plan.json --remote edu-ai    # dry run there

FOUR SEPARATE OPERATIONS, and the separation is the point: a single "sync"
command that decides for itself what to change is a command nobody can review
and nobody can undo.

    (default)   DRY RUN. Reads, resolves, re-checks every precondition, prints
                exactly what WOULD change and why — and writes NOTHING, not even
                a snapshot. `--apply` alone is still a dry run; `--go` is the
                only word that writes.
    --apply     One transaction per SET. Re-checks the preconditions IN THE SAME
                TRANSACTION as the write, so a set whose content moved since the
                dry run is skipped rather than overwritten. Emits a snapshot
                file first: the exact prior value of every row it touches.
    --verify    Reads back and reports whether the target state holds, including
                the derived chunk labels the writer must have propagated.
    --rollback  Restores a snapshot, row by row, to the value it recorded. NOT a
                bulk reset: `qa_deactivate_packs.py --activate` turns every
                variant on, which is a different thing from "put it back", and
                that distinction has already cost a curated set once.

THE PLAN IS CODES, NOT IDS (spec §7). An id means something different in every
school's catalog, so a plan carrying ids is a plan that only works against the
database it was written on. This resolves codes per entity at run time and
refuses a code the entity cannot resolve.

WHAT IT DOES NOT DO — deliberately, and this is the sharpest limit to
understand. It writes DECLARED labels (document_metadata.document_domain_user,
qa_pack_manifest / qa_entries.domain_ids) and the DERIVED copies retrieval scans
(document_chunks.document_domain_id, qa_variants / qa_embedding.domain_ids).
Those are the columns the area filter reads, and keeping them consistent in one
transaction is the whole job. It does NOT re-chunk, re-embed, re-classify or
touch any text — and it does NOT recompute taxonomy_binding,
document_binding_id or the audience/grade values derived from a binding. If a
plan changes a document's areas in a way that would change its BINDING, that is
a reingest, not this tool: `--verify` reports a binding that no longer matches
its declaration so the case cannot pass silently.

PLAN FORMAT (JSON) — every item records the decision AND its expected prior
state, so a plan is reviewable on its own and an apply can refuse a moved target:

    {
      "note": "why these decisions, and who made them",
      "target": {"database": "edu_ai", "entities": ["ZS_KAVCI_HORY_ZS"]},
      "sets": [
        {
          "id": "faq-provoz-areas",
          "kind": "qa_pack",
          "entity": "ZS_KAVCI_HORY_ZS",
          "pack_id": "faq-provoz",
          "expect_current": [],
          "domains": ["ATTENDANCE", "CATERING"],
          "source": "kurátor 2026-09-06, zápis v ticketu X"
        },
        {
          "id": "faq-provoz-entry-overrides",
          "kind": "qa_entries",
          "entity": "ZS_KAVCI_HORY_ZS",
          "pack_id": "faq-provoz",
          "entries": {"jak-se-omlouva-absence": ["ATTENDANCE"],
                      "do-kdy-odhlasit-obed":  ["CATERING"]},
          "source": "same"
        },
        {
          "id": "sos-rules-areas",
          "kind": "document",
          "entity": "ZS_KAVCI_HORY_SOS",
          "file_uuid": "…-…",
          "expect_current": [],
          "domains": ["ATTENDANCE", "GRADING", "CATERING"],
          "source": "same"
        },
        {
          "id": "kavci-hory-repropagate",
          "kind": "repropagate_chunks",
          "entity": "ZS_KAVCI_HORY",
          "source": "inventory 2026-09-06: 151 chunks with no label under a labelled document"
        }
      ]
    }

`entries` is keyed by ENTRY_KEY, not entry_id: the QA writer resolves identity
through entry_key, so the same CSV rebuilt into another pack does NOT keep its
UUID (spec §6). A plan written against ids would silently miss after a rebuild.

`repropagate_chunks` is the one kind with no `domains`: it copies each active
document's EFFECTIVE declaration (user, else LLM) onto its own chunks. That is
the fix for the inventory's "MISSING label" finding — chunks left behind by a
metadata-only edit — and it invents nothing, which is why it needs no curator
decision beyond naming the school.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pathlib
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")          # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

LOCAL_PSQL = ['docker', 'exec', '-i', 'edu_ai_postgres', 'sh', '-c',
              'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -qAt -F "\x1f"']
REMOTE_PSQL = ('cd /opt/edu_ai && docker compose exec -T postgres sh -c '
               '\'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 '
               '-qAt -F "\x1f"\'')
SEP = "\x1f"
KINDS = ("qa_pack", "qa_entries", "document", "repropagate_chunks")

_remote: str | None = None


def psql(sql: str) -> list[list[str]]:
    cmd = (['ssh', '-o', 'BatchMode=yes', _remote, REMOTE_PSQL] if _remote
           else LOCAL_PSQL)
    p = subprocess.run(cmd, input=sql, capture_output=True, text=True,
                       encoding='utf-8')
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip())
    # strip("\n"), NOT strip(): the field separator is U+001F and
    # "\x1f".isspace() is TRUE in Python, so a bare .strip() eats a TRAILING
    # SEPARATOR — which silently drops an empty last column. Caught on a
    # snapshot row whose prior label was empty: it came back with two fields
    # instead of three and the rollback unpacked the wrong number. An empty last
    # column is exactly the value a migration most needs to record.
    return [line.split(SEP) for line in p.stdout.strip("\n").split("\n") if line]


def lit(value: str) -> str:
    """A single-quoted SQL literal. psql takes SQL on stdin and cannot take a
    parameter, so every value is inlined — and every value therefore goes
    through here. Identifiers are separately pattern-checked by validate()."""
    return "'" + str(value).replace("'", "''") + "'"


# The snapshot's stand-in for a SQL NULL. Needed because array_to_string() maps
# BOTH a NULL column and an empty array to the empty string, and a rollback that
# wrote '{}' over an original NULL would restore the same BEHAVIOUR (array
# overlap is false either way) while leaving a different ROW — so an audit of
# "what did this migration change" could not answer honestly. Chosen to be safe
# in both languages: '\N' is a malformed escape in Python and '\NULL' would be
# read as one.
NULL_TOKEN = "<NULL>"


def arr(ids) -> str:
    """A bigint[] literal for a list of ids, or NULL / '{}' for the two empties."""
    if ids == NULL_TOKEN:
        return "NULL"
    if not ids:
        return "'{}'::bigint[]"
    return "ARRAY[" + ",".join(str(int(i)) for i in ids) + "]::bigint[]"


def codes_of(entity: str) -> dict[str, int]:
    """{code: id} for the entity's OWN resolvable catalog.

    The write side, so this is deliberately the SCHOOL's catalog and not the
    wider set the query-time resolver expands to: a curator labels a source with
    a code their school owns. The resolver's job of ALSO matching the global id
    of the same code happens at search time and needs nothing here.
    """
    rows = psql("SELECT type_code, id FROM "
                f"edu_ai.resolved_taxonomy_rules({lit(entity)}, 'DOMAIN');")
    return {r[0]: int(r[1]) for r in rows}


# ── plan handling ───────────────────────────────────────────────────────────
def validate(plan: dict) -> list[dict]:
    """Structural checks, before a single query runs. Fail early and completely.

    A plan is reviewed by a person and applied by a machine; a typo found half
    way through the apply is a half-migrated database. So every item is checked
    here first, and ALL problems are reported rather than the first.
    """
    problems: list[str] = []
    sets = plan.get("sets")
    if not isinstance(sets, list) or not sets:
        sys.exit("plan: `sets` must be a non-empty list")
    seen: set[str] = set()
    for i, item in enumerate(sets):
        where = f"sets[{i}]"
        sid = item.get("id")
        if not sid or not isinstance(sid, str):
            problems.append(f"{where}: `id` is required (it names the set in the "
                            f"snapshot and in the rollback)")
        elif sid in seen:
            problems.append(f"{where}: duplicate id {sid!r}")
        else:
            seen.add(sid)
        kind = item.get("kind")
        if kind not in KINDS:
            problems.append(f"{where}: kind must be one of {KINDS}")
            continue
        ent = item.get("entity")
        if not ent or not str(ent).replace("_", "").replace("-", "").isalnum():
            problems.append(f"{where}: `entity` must be a plain legal_entity_code")
        if kind != "repropagate_chunks" and not item.get("source"):
            # WHO DECIDED. Not bureaucracy: an area label that nobody can trace
            # back to a decision is indistinguishable from a guess, and spec §6
            # makes the curator's confirmation part of the acceptance.
            problems.append(f"{where}: `source` is required — record who decided "
                            f"and where it is written down")
        if kind == "qa_pack":
            if not item.get("pack_id"):
                problems.append(f"{where}: `pack_id` is required")
            if not isinstance(item.get("domains"), list) or not item["domains"]:
                problems.append(f"{where}: `domains` must be a non-empty list of codes")
        elif kind == "qa_entries":
            if not item.get("pack_id"):
                problems.append(f"{where}: `pack_id` is required")
            entries = item.get("entries")
            if not isinstance(entries, dict) or not entries:
                problems.append(f"{where}: `entries` must be a non-empty "
                                f"{{entry_key: [codes]}} object")
            else:
                for k, v in entries.items():
                    if not isinstance(v, list) or not v:
                        problems.append(f"{where}: entries[{k!r}] must be a "
                                        f"non-empty list of codes")
        elif kind == "document":
            if not item.get("file_uuid"):
                problems.append(f"{where}: `file_uuid` is required")
            if not isinstance(item.get("domains"), list) or not item["domains"]:
                problems.append(f"{where}: `domains` must be a non-empty list of codes")
        # ids in a plan are the mistake this format exists to prevent
        for key in ("domain_ids", "ids"):
            if key in item:
                problems.append(f"{where}: `{key}` is not accepted — a plan carries "
                                f"CODES, because an id means something different "
                                f"in every school's catalog")
    if problems:
        print("PLAN REJECTED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(2)
    return sets


def resolve_codes(item: dict) -> list[int]:
    catalog = codes_of(item["entity"])
    out, missing = [], []
    for code in item.get("domains", []):
        c = str(code).strip().upper()
        (out.append(catalog[c]) if c in catalog else missing.append(c))
    if missing:
        sys.exit(f"set {item['id']}: {item['entity']} cannot resolve "
                 f"{', '.join(missing)} — its catalog holds "
                 f"{', '.join(sorted(catalog)) or '(nothing)'}")
    return sorted(set(out))


# ── reading the CURRENT state, per set ──────────────────────────────────────
def current(item: dict) -> dict:
    """The rows this set would touch, with their exact present values.

    Read once for the dry run's report, and read AGAIN inside the apply
    transaction — see apply(). The two are compared, so a set whose content
    changed between the two is refused rather than overwritten.
    """
    kind, ent = item["kind"], item["entity"]
    if kind == "qa_pack":
        rows = psql(
            "SELECT pack_id, coalesce(array_to_string(domain_ids, ','), ''), "
            "       is_active::text "
            f"  FROM edu_ai.qa_pack_manifest "
            f" WHERE pack_id = {lit(item['pack_id'])} "
            f"   AND legal_entity_code = {lit(ent)};")
        return {"rows": rows}
    if kind == "qa_entries":
        keys = ",".join(lit(k) for k in item["entries"])
        rows = psql(
            "SELECT entry_key, entry_id::text, "
            "       coalesce(array_to_string(domain_ids, ','), ''), is_active::text "
            f"  FROM edu_ai.qa_entries "
            f" WHERE pack_id = {lit(item['pack_id'])} "
            f"   AND legal_entity_code = {lit(ent)} "
            f"   AND entry_key IN ({keys});")
        return {"rows": rows}
    if kind == "document":
        rows = psql(
            "SELECT m.file_uuid::text, m.processing_uuid::text, m.is_active::text, "
            "       coalesce(array_to_string(m.document_domain_user, ','), ''), "
            "       coalesce(array_to_string(m.document_domain_llm, ','), ''), "
            "       (SELECT count(*) FROM edu_ai.document_chunks c "
            "         WHERE c.file_uuid = m.file_uuid "
            "           AND c.processing_uuid = m.processing_uuid)::text, "
            "       coalesce(m.binding_id::text, '') "
            f"  FROM edu_ai.document_metadata m "
            f" WHERE m.file_uuid = {lit(item['file_uuid'])}::uuid "
            f"   AND m.legal_entity_code = {lit(ent)} "
            f" ORDER BY m.is_active DESC;")
        return {"rows": rows}
    # repropagate_chunks
    rows = psql(
        "SELECT m.file_uuid::text, m.processing_uuid::text, "
        "       coalesce(array_to_string("
        "         CASE WHEN cardinality(coalesce(m.document_domain_user, '{}')) > 0 "
        "              THEN m.document_domain_user "
        "              ELSE coalesce(m.document_domain_llm, '{}') END, ','), ''), "
        "       count(c.id)::text, "
        "       count(c.id) FILTER (WHERE coalesce(c.document_domain_id, '{}') "
        "         IS DISTINCT FROM CASE "
        "           WHEN cardinality(coalesce(m.document_domain_user, '{}')) > 0 "
        "           THEN m.document_domain_user "
        "           ELSE coalesce(m.document_domain_llm, '{}') END)::text "
        "  FROM edu_ai.document_metadata m "
        "  JOIN edu_ai.document_chunks c "
        "    ON c.file_uuid = m.file_uuid AND c.processing_uuid = m.processing_uuid "
        f" WHERE m.is_active AND m.legal_entity_code = {lit(ent)} "
        " GROUP BY 1, 2, 3 HAVING count(c.id) FILTER (WHERE "
        "   coalesce(c.document_domain_id, '{}') IS DISTINCT FROM CASE "
        "     WHEN cardinality(coalesce(m.document_domain_user, '{}')) > 0 "
        "     THEN m.document_domain_user "
        "     ELSE coalesce(m.document_domain_llm, '{}') END) > 0 "
        " ORDER BY 1;")
    return {"rows": rows}


# WHICH COLUMN of current()'s result holds the area labels, per kind. A map and
# not an inline index: the three SELECTs have different shapes, and the first
# version of this file used one index for two of them and read `is_active` as an
# id list. Named here so the SELECT and the reader cannot drift apart silently.
DOMAIN_COL = {"qa_pack": 1, "qa_entries": 2, "document": 3,
              "repropagate_chunks": 2}


def _expect_ok(item: dict, cur: dict) -> tuple[bool, str]:
    """Does the CURRENT state match the plan's `expect_current`?

    Optional, and worth using: it turns "apply this decision" into "apply this
    decision to the state I reviewed". Without it a set written against an
    untagged pack would silently overwrite the labels somebody added yesterday.
    """
    if "expect_current" not in item:
        return True, "no expect_current — applied to whatever is there now"
    want = sorted({str(c).strip().upper() for c in (item["expect_current"] or [])})
    catalog = {v: k for k, v in codes_of(item["entity"]).items()}
    for row in cur["rows"]:
        raw = row[DOMAIN_COL[item["kind"]]]
        got = sorted(catalog.get(int(i), f"#{i}") for i in raw.split(",") if i)
        if got != want:
            return False, f"expected {want or '(none)'}, found {got or '(none)'}"
    return True, f"matches expect_current {want or '(none)'}"


# ── the writes ──────────────────────────────────────────────────────────────
# HOW A ROW IS ADDRESSED AND WHAT ITS VALUE IS — one table, four consumers.
#
#   snapshot_sql   SELECTS (table, key, value) triples: the rollback material,
#                  and, re-run inside the transaction, the guard's actual side
#   guard_sql      locks those rows and compares the whole set against them
#   write_sql      restricts every UPDATE to those keys (`_only`)
#   rollback       writes them back, under the same lock and comparison
#
# They used to carry private copies of the same knowledge and the copies had
# drifted twice: the rollback resynced the QA scan surfaces with no WHERE clause
# at all, and the scan surfaces were missing from the map entirely while the
# writes updated them by JOIN — so a variant added to an existing answer was
# written unguarded and could not be restored. A shared map is what makes
# "the guard checks exactly the rows the writes touch, and the rollback restores
# exactly those" true by construction instead of by inspection.
#
#   key_expr  SQL producing the text key used in the snapshot
#   val_expr  SQL producing the stored value ('<NULL>' for a SQL NULL)
#   val_col   the column a rollback writes
ADDRESS = {
    "qa_pack_manifest": {
        "key_expr": "pack_id",
        "val_expr": "coalesce(array_to_string(domain_ids, ','), '<NULL>')",
        "val_col": "domain_ids",
    },
    "qa_entries": {
        "key_expr": "entry_id::text",
        "val_expr": "coalesce(array_to_string(domain_ids, ','), '<NULL>')",
        "val_col": "domain_ids",
    },
    "document_metadata": {
        "key_expr": "file_uuid::text || '|' || processing_uuid::text",
        "val_expr": "coalesce(array_to_string(document_domain_user, ','), '<NULL>')",
        "val_col": "document_domain_user",
    },
    "document_chunks": {
        "key_expr": "id::text",
        "val_expr": "coalesce(array_to_string(document_domain_id, ','), '<NULL>')",
        "val_col": "document_domain_id",
    },
    # THE SCAN SURFACES ARE ROWS TOO. They were missing from this map, and
    # therefore from the snapshot, the guard and the rollback — while the writes
    # updated them by JOIN. So a variant added to an existing answer after the
    # snapshot was modified unguarded and could not be restored: it was not in
    # the file. Both are keyed by variant_id, which is the primary key of each
    # (checked, not assumed: qa_embedding holds at most one row per variant).
    "qa_variants": {
        "key_expr": "variant_id::text",
        "val_expr": "coalesce(array_to_string(domain_ids, ','), '<NULL>')",
        "val_col": "domain_ids",
    },
    "qa_embedding": {
        "key_expr": "variant_id::text",
        "val_expr": "coalesce(array_to_string(domain_ids, ','), '<NULL>')",
        "val_col": "domain_ids",
    },
}


def _keys_of(rows: list[list[str]], table: str) -> list[str]:
    """The snapshot's keys for one table — the ONLY rows a write may touch."""
    return [k for t, k, _v in rows if t == table]


def _only(rows: list[list[str]], table: str) -> str:
    """` AND <key> IN (…)` for that table, or a predicate that matches nothing.

    EVERY write goes through here. The point is stated once, at length, because
    it is the whole difference between this version and the one that lost data:

    a write selected by `pack_id` touches whatever is in the pack AT WRITE TIME,
    which is not the same set the snapshot recorded and the operator reviewed.
    An entry added in between was modified with no guard and no rollback entry —
    verified independently: `new_row_outside_snapshot_modified=true`. Keyed to
    the snapshot, the write cannot reach it: the row is not in the list.

    That also covers the case the guard structurally cannot see — a row inserted
    by a transaction that has not committed, and is therefore invisible to any
    SELECT this one makes. The guard refuses what it can see; this makes the
    rest unreachable.

    No keys means the snapshot recorded nothing for this table, and the honest
    predicate is then `false` rather than "no restriction".
    """
    keys = _keys_of(rows, table)
    if not keys:
        return " AND false"
    return f" AND {ADDRESS[table]['key_expr']} IN ({', '.join(lit(k) for k in keys)})"


def write_sql(item: dict, ids: list[int], rows: list[list[str]]) -> list[str]:
    """The statements for one set. Ordered DECLARATION FIRST, then the derived
    copies, so a reader can see that both halves are in the same transaction —
    which is the difference between this and the "UPDATE metadata and hope"
    that produced the 170 unlabelled chunks the inventory found.

    `rows` is the snapshot, and every statement is restricted to its keys —
    see `_only`. The pack/file predicates are KEPT alongside: they are cheap,
    they document intent, and they mean a mangled snapshot cannot make a
    statement reach into another school.
    """
    kind, ent = item["kind"], item["entity"]
    if kind == "qa_pack":
        p, a, e = lit(item["pack_id"]), arr(ids), lit(ent)
        return [
            # the pack's declaration
            f"UPDATE edu_ai.qa_pack_manifest SET domain_ids = {a} "
            f" WHERE pack_id = {p} AND legal_entity_code = {e}"
            f"{_only(rows, 'qa_pack_manifest')};",
            # entries that carry NO override inherit it (spec: the declaration
            # fills in only what the row does not state itself)
            f"UPDATE edu_ai.qa_entries SET domain_ids = {a} "
            f" WHERE pack_id = {p} AND legal_entity_code = {e} "
            f"   AND (domain_ids IS NULL OR cardinality(domain_ids) = 0)"
            f"{_only(rows, 'qa_entries')};",
            # …and the two SCAN SURFACES follow the entry, always — keyed to the
            # snapshot's variant_ids, so a variant added to an existing answer
            # after the snapshot is left alone rather than written blind
            "UPDATE edu_ai.qa_variants v SET domain_ids = e.domain_ids "
            "  FROM edu_ai.qa_entries e "
            f" WHERE e.entry_id = v.entry_id AND e.pack_id = {p} "
            f"   AND e.legal_entity_code = {e}"
            f"{_only(rows, 'qa_variants').replace(' AND variant_id', ' AND v.variant_id')};",
            "UPDATE edu_ai.qa_embedding q SET domain_ids = e.domain_ids "
            "  FROM edu_ai.qa_entries e "
            f" WHERE e.entry_id = q.entry_id AND e.pack_id = {p} "
            f"   AND e.legal_entity_code = {e}"
            f"{_only(rows, 'qa_embedding').replace(' AND variant_id', ' AND q.variant_id')};",
        ]
    if kind == "qa_entries":
        p, e = lit(item["pack_id"]), lit(ent)
        out = []
        for key, codes in item["entries"].items():
            eids = resolve_codes({**item, "domains": codes})
            out.append(
                f"UPDATE edu_ai.qa_entries SET domain_ids = {arr(eids)} "
                f" WHERE pack_id = {p} AND legal_entity_code = {e} "
                f"   AND entry_key = {lit(key)}{_only(rows, 'qa_entries')};")
        keys = ",".join(lit(k) for k in item["entries"])
        out += [
            "UPDATE edu_ai.qa_variants v SET domain_ids = e.domain_ids "
            "  FROM edu_ai.qa_entries e "
            f" WHERE e.entry_id = v.entry_id AND e.pack_id = {p} "
            f"   AND e.legal_entity_code = {e} AND e.entry_key IN ({keys})"
            f"{_only(rows, 'qa_variants').replace(' AND variant_id', ' AND v.variant_id')};",
            "UPDATE edu_ai.qa_embedding q SET domain_ids = e.domain_ids "
            "  FROM edu_ai.qa_entries e "
            f" WHERE e.entry_id = q.entry_id AND e.pack_id = {p} "
            f"   AND e.legal_entity_code = {e} AND e.entry_key IN ({keys})"
            f"{_only(rows, 'qa_embedding').replace(' AND variant_id', ' AND q.variant_id')};",
        ]
        return out
    if kind == "document":
        fu, a, e = lit(item["file_uuid"]), arr(ids), lit(ent)
        return [
            # the USER declaration — the half a curator owns. The classifier's
            # document_domain_llm is left exactly as it is: overwriting it would
            # destroy the record of what the model said, and the precedence
            # (user wins when non-empty) already makes it irrelevant.
            f"UPDATE edu_ai.document_metadata SET document_domain_user = {a} "
            f" WHERE file_uuid = {fu}::uuid AND legal_entity_code = {e} "
            f"   AND is_active"
            f"{_only(rows, 'document_metadata')};",
            # …and the chunks of the ACTIVE version only. A superseded version's
            # chunks keep their old labels on purpose: they describe what that
            # version said, nothing retrieves them, and rewriting them would
            # destroy the only record of the previous state.
            "UPDATE edu_ai.document_chunks c "
            f"   SET document_domain_id = {a} "
            "  FROM edu_ai.document_metadata m "
            "  WHERE m.file_uuid = c.file_uuid "
            "    AND m.processing_uuid = c.processing_uuid AND m.is_active "
            f"   AND c.file_uuid = {fu}::uuid AND c.legal_entity_code = {e}"
            f"{_only(rows, 'document_chunks').replace(' AND id', ' AND c.id')};",
        ]
    # repropagate_chunks — copies the declaration down, invents nothing, and
    # only onto the chunks the snapshot listed as needing it
    return [
        "UPDATE edu_ai.document_chunks c "
        "   SET document_domain_id = CASE "
        "         WHEN cardinality(coalesce(m.document_domain_user, '{}')) > 0 "
        "         THEN m.document_domain_user "
        "         ELSE coalesce(m.document_domain_llm, '{}') END "
        "  FROM edu_ai.document_metadata m "
        " WHERE m.file_uuid = c.file_uuid "
        "   AND m.processing_uuid = c.processing_uuid AND m.is_active "
        f"  AND c.legal_entity_code = {lit(ent)} "
        "   AND coalesce(c.document_domain_id, '{}') IS DISTINCT FROM CASE "
        "         WHEN cardinality(coalesce(m.document_domain_user, '{}')) > 0 "
        "         THEN m.document_domain_user "
        "         ELSE coalesce(m.document_domain_llm, '{}') END"
        f"{_only(rows, 'document_chunks').replace(' AND id', ' AND c.id')};",
    ]


def guard_sql(item: dict, rows: list[list[str]]) -> str:
    """A DO block that ABORTS the transaction unless the set is EXACTLY as
    snapshotted — same rows, same values.

    Two jobs, and the second is the one the first version of this got wrong.

    **Lock the rows we are about to write.** Without it the check is advisory:
    a concurrent session can commit in the gap between the comparison and the
    UPDATEs, and the comparison would have passed on a value that is no longer
    there. `FOR UPDATE` closes that in both directions — a writer that already
    committed is visible to the comparison, and a writer mid-transaction blocks
    until this one finishes and then sees the migrated value. Not `NOWAIT`:
    waiting for a curator's save to land is correct, refusing to migrate
    because somebody has a row open is not.

    **Compare the WHOLE target set, not just the keys we remember.** The
    previous version restricted the `actual` side to
    `WHERE key IN (SELECT k FROM expected)`, which makes "a row appeared"
    structurally undetectable — the FULL JOIN could never produce a row missing
    from `expected`, and the docstring claimed it could. Meanwhile the writes
    selected by `pack_id`, so an entry added to the pack after the snapshot was
    modified having passed no check at all. Verified independently on a
    temporary table: `new_row_outside_snapshot_modified=true`.

    So `actual` is now `snapshot_sql(item)` — the same selector that produced
    the snapshot, re-run inside the transaction. It enumerates the rows the set
    TARGETS, by pack / file / entity, so a row added since is on the actual side
    with no counterpart in `expected` and the set is refused. One selector, two
    uses: a divergence between "what was recorded" and "what would be written"
    is now impossible to express.

    Belt AND braces: `write_sql` is separately restricted to the snapshot's
    exact keys, so even a row this guard cannot see — one inserted by a
    transaction that has not committed yet, and is therefore invisible — is
    never written.
    """
    if not rows:
        # Nothing to touch is not an error: an already-consistent
        # repropagate_chunks set legitimately snapshots zero rows, and with the
        # writes keyed to the snapshot there is then nothing to write either.
        return ""
    by_table: dict[str, list[str]] = {}
    for table, key, _value in rows:
        if table not in ADDRESS:
            sys.exit(f"guard: unknown table {table!r} in snapshot")
        by_table.setdefault(table, []).append(key)

    locks = "".join(f"""
    PERFORM 1 FROM edu_ai.{table}
      WHERE {ADDRESS[table]['key_expr']} IN ({", ".join(lit(k) for k in keys)})
      FOR UPDATE;""" for table, keys in by_table.items())

    values = ", ".join(f"({lit(t)}, {lit(k)}, {lit(v)})" for t, k, v in rows)
    selector = snapshot_sql(item).rstrip().rstrip(";")
    return f"""DO $guard$
DECLARE n bigint;
BEGIN{locks}

    WITH expected(t, k, v) AS (VALUES {values}),
         actual(t, k, v) AS ({selector})
    SELECT count(*) INTO n
      FROM expected e FULL JOIN actual a USING (t, k)
     WHERE e.k IS NULL          -- a row APPEARED since the snapshot
        OR a.k IS NULL          -- a row DISAPPEARED
        OR e.v IS DISTINCT FROM a.v;   -- a value was edited
    IF n > 0 THEN
        RAISE EXCEPTION 'precondition moved: % row(s) differ from the snapshot '
            '(added, removed or edited since it was read). Nothing was written. '
            'Re-run the dry run and review the set.', n;
    END IF;
END $guard$;
"""

# The scan-surface half of a QA snapshot. Written once and reused by both QA
# kinds, because it must select EXACTLY the rows write_sql updates — the two
# drifting apart is what left new variants unguarded and unrestorable.
def _qa_surface_sql(entry_pred: str) -> str:
    out = []
    for table in ("qa_variants", "qa_embedding"):
        a = ADDRESS[table]
        out.append(
            f"SELECT '{table}', s.{a['key_expr']}, "
            f"       coalesce(array_to_string(s.domain_ids, ','), '<NULL>') "
            f"  FROM edu_ai.{table} s JOIN edu_ai.qa_entries e "
            f"    ON e.entry_id = s.entry_id WHERE {entry_pred}")
    return " UNION ALL ".join(out)


def snapshot_sql(item: dict) -> str:
    """The SELECT whose result IS the rollback — and, re-run, the guard.

    One row per row that WILL BE WRITTEN, carrying its present value; not a
    pack name to re-activate later. Two properties it has to keep, because the
    guard and the writes are both derived from it:

      * it must select exactly what `write_sql` updates. A row the writes touch
        and this does not is a row that is neither guarded nor restorable —
        which is what happened to `qa_variants` / `qa_embedding`, updated by
        JOIN while absent from the snapshot entirely.
      * it must be a SELECTOR, not a key list, so that re-running it inside the
        transaction reveals rows added since.
    """
    kind, ent = item["kind"], item["entity"]
    if kind == "qa_pack":
        pack, e = lit(item["pack_id"]), lit(ent)
        entry_pred = f"e.pack_id = {pack} AND e.legal_entity_code = {e}"
        return (
            "SELECT 'qa_pack_manifest', pack_id, "
            "       coalesce(array_to_string(domain_ids, ','), '<NULL>') "
            f"  FROM edu_ai.qa_pack_manifest WHERE pack_id = {pack} "
            f"   AND legal_entity_code = {e} "
            "UNION ALL "
            "SELECT 'qa_entries', entry_id::text, "
            "       coalesce(array_to_string(domain_ids, ','), '<NULL>') "
            f"  FROM edu_ai.qa_entries WHERE pack_id = {pack} "
            f"   AND legal_entity_code = {e} "
            "UNION ALL " + _qa_surface_sql(entry_pred) + ";")
    if kind == "qa_entries":
        pack, e = lit(item["pack_id"]), lit(ent)
        keys = ",".join(lit(k) for k in item["entries"])
        entry_pred = (f"e.pack_id = {pack} AND e.legal_entity_code = {e} "
                      f"AND e.entry_key IN ({keys})")
        return ("SELECT 'qa_entries', entry_id::text, "
                "       coalesce(array_to_string(domain_ids, ','), '<NULL>') "
                f"  FROM edu_ai.qa_entries WHERE pack_id = {pack} "
                f"   AND legal_entity_code = {e} AND entry_key IN ({keys}) "
                "UNION ALL " + _qa_surface_sql(entry_pred) + ";")
    if kind == "document":
        return (
            "SELECT 'document_metadata', file_uuid::text || '|' || processing_uuid::text, "
            "       coalesce(array_to_string(document_domain_user, ','), '<NULL>') "
            f"  FROM edu_ai.document_metadata WHERE file_uuid = {lit(item['file_uuid'])}::uuid "
            f"   AND legal_entity_code = {lit(ent)} AND is_active "
            "UNION ALL "
            "SELECT 'document_chunks', c.id::text, "
            "       coalesce(array_to_string(c.document_domain_id, ','), '<NULL>') "
            "  FROM edu_ai.document_chunks c JOIN edu_ai.document_metadata m "
            "    ON m.file_uuid = c.file_uuid AND m.processing_uuid = c.processing_uuid "
            f" WHERE m.is_active AND c.file_uuid = {lit(item['file_uuid'])}::uuid "
            f"   AND c.legal_entity_code = {lit(ent)};")
    return (
        "SELECT 'document_chunks', c.id::text, "
        "       coalesce(array_to_string(c.document_domain_id, ','), '<NULL>') "
        "  FROM edu_ai.document_chunks c JOIN edu_ai.document_metadata m "
        "    ON m.file_uuid = c.file_uuid AND m.processing_uuid = c.processing_uuid "
        f" WHERE m.is_active AND c.legal_entity_code = {lit(ent)} "
        "   AND coalesce(c.document_domain_id, '{}') IS DISTINCT FROM CASE "
        "         WHEN cardinality(coalesce(m.document_domain_user, '{}')) > 0 "
        "         THEN m.document_domain_user "
        "         ELSE coalesce(m.document_domain_llm, '{}') END;")


# ── the four operations ─────────────────────────────────────────────────────
def dry_run(sets: list[dict]) -> None:
    print("\nDRY RUN — nothing is written, not even a snapshot.\n")
    for item in sets:
        ids = resolve_codes(item) if item["kind"] != "repropagate_chunks" else []
        cur = current(item)
        ok, why = _expect_ok(item, cur)
        print(f"── set {item['id']}  ({item['kind']}, {item['entity']})")
        if item.get("source"):
            print(f"   decided by : {item['source']}")
        if ids:
            print(f"   target     : {', '.join(item['domains'])}  -> ids {ids}")
        print(f"   rows now   : {len(cur['rows'])}")
        for r in cur["rows"][:6]:
            print(f"      {SEP.join(r).replace(SEP, ' | ')}")
        if len(cur["rows"]) > 6:
            print(f"      … {len(cur['rows']) - 6} more")
        print(f"   precondition: {'OK' if ok else 'MISMATCH'} — {why}")
        if not cur["rows"]:
            print("   !! nothing matched — check entity / pack_id / file_uuid")
        # The SNAPSHOT the apply would take, so the printed statements are the
        # ones that would really run — they are keyed to it. Reading `cur["rows"]`
        # here instead would show a different, wider set: `current()` is the
        # human-readable summary and snapshot_sql is the write target, and the
        # two are deliberately not the same query.
        snap_rows = psql(snapshot_sql(item))
        print(f"   snapshot   : {len(snap_rows)} row(s) across "
              f"{len({r[0] for r in snap_rows})} table(s) — the writes are keyed "
              f"to exactly these")
        print("   statements :")
        for st in write_sql(item, ids, snap_rows):
            print("      " + " ".join(st.split())[:160])
        print()
    print("Add --apply --go to write. --apply without --go is still a dry run.")


def apply(sets: list[dict], out_dir: pathlib.Path, go: bool) -> int:
    """Write the plan. THE SNAPSHOT REACHES DISK BEFORE ANY ROW IS TOUCHED.

    The order below is the whole design, and the previous version had it wrong
    in a way that only shows up on a bad day:

      1. read + resolve + expect-check EVERY set, and snapshot the rows each
         one would touch;
      2. **write the snapshot file** — status `prepared`, nothing changed yet;
      3. apply the sets one transaction at a time, each opening with a guard
         that re-asserts the snapshot INSIDE the transaction;
      4. rewrite the file after every set, so it always describes what has
         actually happened.

    Before, the file was written after the loop. A process killed half way
    through — a dropped SSH connection is enough — left a database with changes
    in it and **no record of the prior values**, which is precisely the moment
    the record is needed. Now the worst case is a snapshot listing a set that
    turns out not to have been applied, and restoring a value to the value it
    already has is a no-op.
    """
    if not go:
        print("\n--apply WITHOUT --go: still a dry run.\n")
        dry_run(sets)
        return 0

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"domain-migration-{stamp}.snapshot.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    snap: dict = {"at": stamp, "target": _remote or "local",
                  "status": "prepared", "sets": {}}

    def flush() -> str:
        """Persist the snapshot and return its checksum. Called before the first
        write and after every set — an fsync'd file is the only thing that
        survives the process."""
        payload = json.dumps(snap, ensure_ascii=False, indent=1)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)          # atomic: never a half-written snapshot
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    # ── 1. read everything first ────────────────────────────────────────────
    plans: list[tuple[dict, list[int], list[list[str]]]] = []
    skipped: list[str] = []
    for item in sets:
        ids = resolve_codes(item) if item["kind"] != "repropagate_chunks" else []
        before = current(item)
        ok, why = _expect_ok(item, before)
        if not ok:
            # NOT a failure of the tool: `expect_current` is what stops a set
            # being applied twice or applied over somebody else's later edit.
            # A re-run of an already-applied plan therefore skips.
            print(f"SKIP {item['id']}: precondition no longer matches — {why}")
            print("     (already applied, or the content moved; nothing written "
                  "for this set)")
            skipped.append(item["id"])
            continue
        rows = psql(snapshot_sql(item))
        snap["sets"][item["id"]] = {
            "kind": item["kind"], "entity": item["entity"],
            "status": "prepared", "rows": rows,
        }
        plans.append((item, ids, rows))

    # ── 2. the snapshot exists on disk BEFORE anything is written ───────────
    digest = flush()
    print(f"\nsnapshot (pre-write): {path}")
    print(f"checksum: sha256={digest}…  status=prepared, "
          f"{sum(len(v['rows']) for v in snap['sets'].values())} row(s) recorded")

    # ── 3. one transaction per set, guarded from inside ─────────────────────
    failed, applied = [], []
    for item, ids, rows in plans:
        guard = guard_sql(item, rows)
        body = "\n".join(write_sql(item, ids, rows))
        sql = f"BEGIN;\n{guard}{body}\nCOMMIT;"
        try:
            psql(sql)
        except RuntimeError as e:
            # The guard raises here too, and that is a normal outcome rather
            # than a crash: the transaction rolled back whole.
            msg = " ".join(str(e).split())
            if "precondition moved" in msg:
                print(f"ABORT {item['id']}: {msg[:200]}")
                snap["sets"][item["id"]]["status"] = "aborted-precondition"
            else:
                print(f"FAIL  {item['id']}: {msg[:200]}")
                snap["sets"][item["id"]]["status"] = "failed"
            snap["sets"][item["id"]]["error"] = msg[:400]
            failed.append(item["id"])
            flush()
            continue
        # AFTER-STATE in the SAME shape as the snapshot, so a rollback can
        # machine-check "has anything changed since we applied this?"
        snap["sets"][item["id"]]["status"] = "applied"
        snap["sets"][item["id"]]["after"] = psql(snapshot_sql(item))
        applied.append(item["id"])
        print(f"OK    {item['id']}: {len(rows)} row(s) touched")
        flush()

    snap["status"] = "done" if not failed else "partial"
    digest = flush()
    print(f"\nsnapshot: {path}")
    print(f"checksum: sha256={digest}…  status={snap['status']}")
    print(f"applied={len(applied)} skipped={len(skipped)} failed={len(failed)}")
    if skipped:
        print(f"  skipped (precondition): {', '.join(skipped)}")
    if failed:
        print(f"  FAILED/ABORTED: {', '.join(failed)}")
    print("Verify with --verify, and keep the snapshot: it is the ONLY exact "
          "rollback (qa_deactivate_packs --activate is not one).")
    return 1 if failed else 0


def verify(sets: list[dict]) -> int:
    print("\nVERIFY — reading back the target state.\n")
    bad = 0
    for item in sets:
        ids = resolve_codes(item) if item["kind"] != "repropagate_chunks" else []
        cur = current(item)
        if item["kind"] == "repropagate_chunks":
            left = sum(int(r[4]) for r in cur["rows"]) if cur["rows"] else 0
            print(f"{item['id']}: {left} chunk(s) still disagree with their "
                  f"document" + ("  OK" if left == 0 else "  !! NOT DONE"))
            bad += 1 if left else 0
            continue
        # PER-ROW EXPECTATION, because a `qa_entries` set has no single target:
        # each entry_key carries its own codes, and comparing them all against a
        # top-level `domains` that does not exist there made verify report a
        # correctly-applied set as a mismatch (caught on the fixture, 2026-09-06).
        for r in cur["rows"]:
            if item["kind"] == "qa_entries":
                want = set(resolve_codes({**item,
                                          "domains": item["entries"][r[0]]}))
            else:
                want = set(ids)
            raw = r[DOMAIN_COL[item["kind"]]]
            got = {int(i) for i in raw.split(",") if i}
            if got != want:
                print(f"{item['id']}: {r[0]} has {sorted(got)}, expected "
                      f"{sorted(want)}  !! MISMATCH")
                bad += 1
        # the derived copies, which is where a metadata-only edit shows up
        if item["kind"] in ("qa_pack", "qa_entries"):
            d = psql("SELECT count(*)::text FROM edu_ai.qa_variants v "
                     "  JOIN edu_ai.qa_entries e ON e.entry_id = v.entry_id "
                     f" WHERE e.pack_id = {lit(item['pack_id'])} "
                     "   AND NOT (coalesce(v.domain_ids, '{}') @> coalesce(e.domain_ids, '{}') "
                     "        AND coalesce(v.domain_ids, '{}') <@ coalesce(e.domain_ids, '{}'));")
            n = int(d[0][0]) if d else 0
            print(f"{item['id']}: scan-surface drift (set-wise) = {n}"
                  + ("  OK" if n == 0 else "  !! the scanned rows disagree"))
            bad += 1 if n else 0
        if item["kind"] == "document":
            d = psql("SELECT count(*)::text FROM edu_ai.document_chunks c "
                     "  JOIN edu_ai.document_metadata m ON m.file_uuid = c.file_uuid "
                     "   AND m.processing_uuid = c.processing_uuid AND m.is_active "
                     f" WHERE c.file_uuid = {lit(item['file_uuid'])}::uuid "
                     "   AND NOT (coalesce(c.document_domain_id, '{}') @> "
                     "            coalesce(m.document_domain_user, '{}') "
                     "        AND coalesce(c.document_domain_id, '{}') <@ "
                     "            coalesce(m.document_domain_user, '{}'));")
            n = int(d[0][0]) if d else 0
            print(f"{item['id']}: chunks vs declaration = {n}"
                  + ("  OK" if n == 0 else "  !! chunks were not re-propagated"))
            bad += 1 if n else 0
            # A BINDING that no longer matches its declaration is a REINGEST,
            # not something this tool may quietly fix — reported so it cannot
            # pass unnoticed.
            b = psql("SELECT coalesce(m.binding_id::text, ''), "
                     "       coalesce(array_to_string(m.document_domain_user, ','), '') "
                     f"  FROM edu_ai.document_metadata m "
                     f" WHERE m.file_uuid = {lit(item['file_uuid'])}::uuid AND m.is_active;")
            for row in b:
                if row[0]:
                    print(f"{item['id']}: binding_id={row[0]} is UNCHANGED — if the "
                          f"new areas change the semantic binding, this needs a "
                          f"targeted reingest, not a label edit")
    print(f"\n{'OK — target state holds' if not bad else f'{bad} problem(s)'}")
    return 1 if bad else 0


def restore_guard_sql(expect: list[tuple[str, str, str]]) -> str:
    """A DO block that locks the rows a restore will write and verifies they
    still hold the value the APPLY left there — inside the restore transaction.

    The rollback used to do this comparison in separate statements BEFORE the
    transaction, which is the same mistake the apply's guard was written to fix,
    made in the other direction: a curator saving between the check and the
    restore had their change overwritten, and the tool had already printed
    "no drift". A printed reassurance is worse than no check.

    Unlike the apply's guard this one is KEY-RESTRICTED, and the difference is
    not an oversight. A rollback writes exactly the rows its snapshot names, so
    a row that has APPEARED elsewhere is none of its business — it will not be
    touched. What matters is that a row it IS about to write has not changed
    (`e.v IS DISTINCT FROM a.v`) and has not disappeared (`a.k IS NULL`).
    """
    if not expect:
        return ""
    by_table: dict[str, list[tuple[str, str]]] = {}
    for table, key, value in expect:
        if table not in ADDRESS:
            sys.exit(f"snapshot names an unknown table {table!r}")
        by_table.setdefault(table, []).append((key, value))

    blocks = []
    for table, pairs in by_table.items():
        a = ADDRESS[table]
        values = ", ".join(f"({lit(k)}, {lit(v)})" for k, v in pairs)
        keys = ", ".join(lit(k) for k, _ in pairs)
        blocks.append(f"""
    PERFORM 1 FROM edu_ai.{table}
      WHERE {a['key_expr']} IN ({keys}) FOR UPDATE;

    WITH expected(k, v) AS (VALUES {values}),
         actual AS (
             SELECT {a['key_expr']} AS k, {a['val_expr']} AS v
               FROM edu_ai.{table}
              WHERE {a['key_expr']} IN (SELECT k FROM expected))
    SELECT count(*) INTO n
      FROM expected e LEFT JOIN actual a USING (k)
     WHERE a.k IS NULL OR e.v IS DISTINCT FROM a.v;
    IF n > 0 THEN
        RAISE EXCEPTION 'changed since the migration: % row(s) of {table} no '
            'longer hold the value the apply left there. Restoring would '
            'destroy a later change. Nothing was written.', n;
    END IF;""")
    return "DO $restore$\nDECLARE n bigint;\nBEGIN" + "".join(blocks) + "\nEND $restore$;\n"


def rollback(path: pathlib.Path, go: bool, force: bool = False) -> int:
    """Restore a snapshot — ONLY the rows it recorded, and only if nothing moved.

    Three things the earlier versions got wrong, each of which turns a rollback
    into its own incident:

    **It touched every row in the database.** The QA scan surfaces were resynced
    with `UPDATE qa_variants v SET … FROM qa_entries e WHERE e.entry_id =
    v.entry_id` — no restriction at all. On production that is ~3 000 variants
    and ~3 000 embeddings rewritten to undo two packs. Bounding it to the
    snapshot's entry ids helped and was still indirect; now the snapshot records
    the variant rows themselves, so each is restored BY ITS OWN KEY and the
    derived resync is gone entirely. A variant added to an answer after the
    snapshot is not in the file and is therefore not touched.

    **It only WARNED about later changes.** A sentence asking the operator to
    check is not a check.

    **It checked before the transaction.** Fixed by `restore_guard_sql`, which
    locks and re-verifies inside it. `--force` skips the verification — never
    the lock.
    """
    snap = json.loads(path.read_text(encoding="utf-8"))
    sets = snap.get("sets", {})
    if not sets:
        sys.exit("snapshot records no sets — nothing to restore")

    # ── which sets may be restored at all ───────────────────────────────────
    # A set that was ABORTED or FAILED changed nothing, so restoring its rows
    # would write values back over themselves at best, and over somebody's
    # later edit at worst. `prepared` is the crash case: the snapshot was
    # written before the transaction and we cannot tell whether it committed,
    # so it IS restorable — that is the whole reason the file is written early.
    restorable = {sid: d for sid, d in sets.items()
                  if d.get("status", "applied") in ("applied", "prepared")}
    for sid, d in sets.items():
        if sid not in restorable:
            print(f"skip {sid}: status={d.get('status')} — it changed nothing")

    # ── the statements: one per recorded row, keyed by that row ─────────────
    stmts: list[str] = []
    expect: list[tuple[str, str, str]] = []
    touched: dict[str, int] = {}
    for sid, data in restorable.items():
        for table, key, value in data.get("rows", []):
            a = ADDRESS.get(table)
            if a is None:
                sys.exit(f"snapshot names an unknown table {table!r}")
            # the sentinel means "this column was NULL", which is not the same
            # row as "this column was an empty array"
            ids = (NULL_TOKEN if value == NULL_TOKEN
                   else [int(i) for i in value.split(",") if i])
            stmts.append(f"UPDATE edu_ai.{table} SET {a['val_col']} = {arr(ids)} "
                         f" WHERE {a['key_expr']} = {lit(key)};")
            touched[table] = touched.get(table, 0) + 1
        # what the APPLY left behind — the yardstick for "has anything changed"
        for row in (data.get("after") or []):
            expect.append((row[0], row[1], row[2]))
        if data.get("after") is None:
            # `prepared`: the apply's outcome is unknown, so there is nothing to
            # compare against and the restore is the point. Said out loud.
            print(f"note {sid}: status=prepared, no after-state recorded — "
                  f"restoring blind, which is what this state is for")

    print(f"\nROLLBACK from {path.name} — {len(stmts)} statement(s) over "
          f"{len(restorable)} set(s); snapshot taken {snap.get('at')} on "
          f"{snap.get('target')}, status={snap.get('status')}")
    print("   rows by table: "
          + ", ".join(f"{t}={n}" for t, n in sorted(touched.items())))
    print("   every statement is keyed to ONE recorded row — nothing else is "
          "touched")
    for st in stmts[:8]:
        print("   " + " ".join(st.split())[:150])
    if len(stmts) > 8:
        print(f"   … {len(stmts) - 8} more")

    # ── a READ-ONLY preview of the drift, for the human ────────────────────
    # The authoritative check is the in-transaction guard below; this exists so
    # the refusal can NAME the rows instead of only counting them.
    drifted: list[str] = []
    for table, key, value in expect:
        a = ADDRESS[table]
        got = psql(f"SELECT {a['val_expr']} FROM edu_ai.{table} "
                   f" WHERE {a['key_expr']} = {lit(key)};")
        now = got[0][0] if got else "<MISSING>"
        if now != value:
            drifted.append(f"{table}[{key}] apply left {value!r}, now {now!r}")
    if drifted:
        print(f"\n!! {len(drifted)} row(s) have CHANGED since this migration was "
              f"applied:")
        for d in drifted[:10]:
            print(f"   {d}")
        if len(drifted) > 10:
            print(f"   … {len(drifted) - 10} more")
        print("\nREFUSING: restoring would silently destroy those later changes.")
        print("Review them, then re-run with --force if the restore is still "
              "what you want.")
        if not force:
            return 2
        print("--force given; the in-transaction verification is SKIPPED too.")

    if not go:
        print("\nDRY RUN — add --go to restore.")
        return 0

    # ── the restore: lock + verify + write, all in ONE transaction ─────────
    # --force drops the VERIFICATION, never the lock: a restore that raced
    # another writer would be neither the old state nor the new one.
    guard = "" if force else restore_guard_sql(expect)
    try:
        psql("BEGIN;\n" + guard + "\n".join(stmts) + "\nCOMMIT;")
    except RuntimeError as e:
        msg = " ".join(str(e).split())
        if "changed since the migration" in msg:
            print(f"\nABORTED: {msg[:300]}")
            print("Something was written between the check above and the "
                  "restore. Nothing was changed. Re-read and decide again.")
            return 2
        raise
    print("restored")
    return 0


def main() -> int:
    global _remote
    ap = argparse.ArgumentParser(
        description="Apply a curator's area decisions. Dry run by default.")
    ap.add_argument("--plan", type=pathlib.Path)
    ap.add_argument("--rollback", type=pathlib.Path, metavar="SNAPSHOT")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--go", action="store_true",
                    help="actually write; without it everything is a dry run")
    ap.add_argument("--force", action="store_true",
                    help="rollback only: restore even though rows changed after "
                         "the apply (it will overwrite those changes)")
    ap.add_argument("--remote", metavar="SSH_HOST",
                    help="target that host's /opt/edu_ai stack instead of local")
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("."),
                    help="where to write the snapshot (default: cwd)")
    args = ap.parse_args()
    _remote = args.remote

    if not args.plan and not args.rollback:
        ap.error("one of --plan or --rollback is required")

    # THE TARGET, named out loud before anything runs. "Nevydávej ad hoc příkazy
    # bez identifikace sady, kterou mění" — and the most expensive version of
    # that mistake is not knowing which database you are on.
    where = args.remote or "LOCAL dev stack"
    row = psql("SELECT current_database(), now()::text, "
               "       (SELECT count(*) FROM edu_ai.legal_entity WHERE is_active)::text;")
    print("=" * 74)
    print(f"TARGET: {where}   database={row[0][0]}   entities={row[0][2]}")
    print(f"        {row[0][1]}")
    print("=" * 74)

    if args.rollback:
        return rollback(args.rollback, args.go, args.force)

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    if plan.get("note"):
        print(f"plan: {plan['note']}")
    # The plan may PIN its target; a mismatch is a stop, not a warning.
    want_db = ((plan.get("target") or {}).get("database"))
    if want_db and want_db != row[0][0]:
        sys.exit(f"plan targets database {want_db!r}, this is {row[0][0]!r}")
    sets = validate(plan)
    print(f"plan: {len(sets)} set(s), "
          f"sha256={hashlib.sha256(args.plan.read_bytes()).hexdigest()[:16]}…")

    if args.verify:
        return verify(sets)
    if args.apply:
        return apply(sets, args.out, args.go)
    dry_run(sets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
