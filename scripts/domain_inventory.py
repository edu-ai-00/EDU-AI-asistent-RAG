#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-09-06).
"""What is tagged with which AREA, per school — the report that decides whether
the area picker can be switched on for a deployment.

    python3 scripts/domain_inventory.py                       # local dev stack
    python3 scripts/domain_inventory.py --remote edu-ai       # production, READ-ONLY
    python3 scripts/domain_inventory.py --entity ZS_KAVCI_HORY_ZS
    python3 scripts/domain_inventory.py --json > /tmp/inventory.json

READ-ONLY. Every statement is a SELECT; there is no --go and no write path, so
it is safe to point at production. The one thing it does that needs saying: it
prints CODES, COUNTS and IDENTIFIERS — never a question, an answer or a chunk of
a document. That is deliberate (AGENTS.md: no user content outside the query
log), and it is why the output can be pasted into a deployment note.

WHAT IT ANSWERS, and why each section exists rather than being derivable:

  catalog      which DOMAIN codes the school may name at all. Nearest-owner-
               exclusive, so a school with its own catalog does NOT see the
               global one — a code that "obviously exists" may be unusable here.
  packs        every ACTIVE pack, its area labels, and how many of its entries
               are labelled DIFFERENTLY from it. A pack whose entries disagree
               with its manifest is the mixed pack that needs splitting or
               per-entry curation; a pack with no labels is invisible the moment
               anyone picks an area.
  entries      per school: active answers, how many carry an area, and how many
               carry an area id the school's catalog does not contain. The last
               number is the one that cannot be seen from the application: an
               id from another school's catalog silently matches nothing.
  documents    active document versions and their chunks, split by whether the
               EFFECTIVE area (user declaration, else the classifier's — the
               precedence the ingest applies) is present. Joined on file_uuid
               AND processing_uuid, so a superseded version's chunks are not
               counted against the active metadata row.
  drift        where the denormalised copies disagree: qa_variants /
               qa_embedding against qa_entries, and chunks against their
               document's declaration. Arrays cannot carry a foreign key and a
               copy cannot be constrained, so a wrong value does not raise — it
               silently mislabels a source, and only a query like this sees it.
  reachable    the same corpus counted through the ROLE and VALIDITY filters,
               per role. A chunk that exists is not a chunk a parent can reach,
               and a coverage report that ignores that overstates what the
               picker will find. Reported separately rather than folded in,
               because "how much is tagged" and "how much a given caller can
               reach" are two different questions and conflating them is how a
               report ends up promising content that no user sees.

WHAT IT DOES NOT DO: decide anything. Which area an untagged source belongs to
is a curatorial judgement (spec §6) — this report says WHERE the judgement is
missing, and scripts/domain_migration.py applies the one a curator wrote down.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

# The report is Czech and box-drawn, and a Windows console defaults to cp1250,
# which cannot encode either. Reconfiguring the stream is the fix that keeps the
# output identical on both platforms — stripping the characters instead would
# mean the server and the dev machine print different reports, and one of them
# would end up pasted into a deployment note.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")          # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

# The local dev stack. Both forms read the container's own env, so no credential
# is written here or passed on a command line.
LOCAL_PSQL = ['docker', 'exec', '-i', 'edu_ai_postgres', 'sh', '-c',
              'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -qAt -F "\x1f"']
# The server runs the stack from /opt/edu_ai under compose, and SSH passes stdin
# straight through — the same shape scripts/qa_deactivate_packs.py uses.
REMOTE_PSQL = ('cd /opt/edu_ai && docker compose exec -T postgres sh -c '
               '\'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 '
               '-qAt -F "\x1f"\'')

SEP = "\x1f"
_remote: str | None = None


def psql(sql: str) -> list[list[str]]:
    """Rows as lists of strings. SQL on STDIN, never -c.

    NOTE on the query constants below: they are str.format templates, so an
    empty-array literal is written `'{{}}'` and reaches psql as `'{}'`. It is
    ugly and it is the alternative to building the entity predicate by string
    concatenation, which is how an inlined identifier turns into an injection.

    -c would send the whole string as one query, and psql reports only the last
    result of such a batch; on stdin each statement is its own. Same reason
    scripts/qa_deactivate_packs.py does it this way.
    """
    cmd = (['ssh', '-o', 'BatchMode=yes', _remote, REMOTE_PSQL] if _remote
           else LOCAL_PSQL)
    p = subprocess.run(cmd, input=sql, capture_output=True, text=True,
                       encoding='utf-8')
    if p.returncode != 0:
        sys.exit(f"psql failed:\n{p.stderr.strip()}")
    # strip("\n"), NOT strip(): the field separator is U+001F and
    # "\x1f".isspace() is TRUE in Python, so a bare .strip() eats a TRAILING
    # SEPARATOR — which silently drops an empty last column. Caught on a
    # migration snapshot row whose prior label was empty: it came back with two
    # fields instead of three and the rollback unpacked the wrong number. An
    # empty last column is exactly the value a migration most needs to record.
    return [line.split(SEP) for line in p.stdout.strip("\n").split("\n") if line]


def run(template: str, filters: dict) -> list[list[str]]:
    """Format a query template and run it. THE only way a query reaches psql.

    Every Q_* constant goes through here even when it has no {placeholder},
    because str.format is also what un-doubles the `'{{}}'` empty-array
    escapes — Q_DRIFT was sent raw on the first run and Postgres rejected
    `"{{}}"` as a malformed array literal. One path, no exceptions to remember.
    """
    return psql(template.format(**filters))


def table(title: str, headers: list[str], rows: list[list[str]],
          note: str | None = None) -> None:
    print(f"\n── {title} " + "─" * max(0, 66 - len(title)))
    if note:
        print(f"   {note}")
    if not rows:
        print("   (nic)")
        return
    widths = [max(len(str(headers[i])),
                  max((len(str(r[i])) for r in rows), default=0))
              for i in range(len(headers))]
    print("   " + "  ".join(str(h).ljust(w) for h, w in zip(headers, widths)))
    print("   " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("   " + "  ".join(str(c).ljust(w) for c, w in zip(r, widths)))


# ── the queries ─────────────────────────────────────────────────────────────
Q_ENTITIES = """
SELECT e.legal_entity_code, coalesce(e.kind, '?'), coalesce(e.level, '-'),
       coalesce(e.parent_code, '-')
  FROM edu_ai.legal_entity e
 WHERE e.is_active {entity_filter}
 ORDER BY coalesce(e.parent_code, e.legal_entity_code), e.legal_entity_code;
"""

Q_CATALOG = """
SELECT e.legal_entity_code,
       (SELECT count(*) FROM edu_ai.resolved_taxonomy_rules(e.legal_entity_code, 'DOMAIN')),
       -- OWN or INHERITED: nearest-owner-exclusive means a school with any row
       -- of its own sees ONLY its own, so this distinction decides whether a
       -- code from the global set is usable here at all
       (SELECT count(*) FROM edu_ai.taxonomy_rules t
         WHERE t.taxonomy_type_code = 'DOMAIN' AND t.is_active
           AND t.legal_entity_code = e.legal_entity_code),
       (SELECT string_agg(type_code, ',' ORDER BY sort_order)
          FROM edu_ai.resolved_taxonomy_rules(e.legal_entity_code, 'DOMAIN'))
  FROM edu_ai.legal_entity e
 WHERE e.is_active {entity_filter}
 ORDER BY 1;
"""

Q_PACKS = """
SELECT m.legal_entity_code, m.pack_id, m.visibility,
       coalesce(array_to_string(m.audience_roles, ','), '-'),
       coalesce(cardinality(m.domain_ids), 0),
       -- the manifest's areas as CODES; an id that does not resolve in this
       -- entity's catalog shows up as the literal id, which is the finding
       coalesce((SELECT string_agg(coalesce(t.type_code, '#' || d::text), ',' ORDER BY 1)
                   FROM unnest(m.domain_ids) d
                   LEFT JOIN edu_ai.resolved_taxonomy_rules(m.legal_entity_code, 'DOMAIN') t
                          ON t.id = d), '-'),
       (SELECT count(*) FROM edu_ai.qa_entries e
         WHERE e.pack_id = m.pack_id AND e.is_active),
       (SELECT count(*) FROM edu_ai.qa_entries e
         WHERE e.pack_id = m.pack_id AND e.is_active
           AND (e.domain_ids IS NULL OR cardinality(e.domain_ids) = 0)),
       -- entries whose own labels DIFFER from the pack's: the mixed pack
       (SELECT count(*) FROM edu_ai.qa_entries e
         WHERE e.pack_id = m.pack_id AND e.is_active
           AND e.domain_ids IS NOT NULL AND cardinality(e.domain_ids) > 0
           AND NOT (e.domain_ids @> coalesce(m.domain_ids, '{{}}')
                AND e.domain_ids <@ coalesce(m.domain_ids, '{{}}')))
  FROM edu_ai.qa_pack_manifest m
 WHERE m.is_active {pack_entity_filter}
 ORDER BY 1, 2;
"""

Q_ENTRY_IDS = """
SELECT e.legal_entity_code,
       count(*) FILTER (WHERE e.is_active),
       count(*) FILTER (WHERE e.is_active AND e.domain_ids IS NOT NULL
                          AND cardinality(e.domain_ids) > 0),
       -- an area id the entry's OWN school cannot resolve: matches nothing, and
       -- nothing in the application will ever say so
       (SELECT count(DISTINCT e2.entry_id) FROM edu_ai.qa_entries e2
          CROSS JOIN LATERAL unnest(e2.domain_ids) d
         WHERE e2.is_active AND e2.legal_entity_code = e.legal_entity_code
           AND NOT EXISTS (
                 SELECT 1 FROM edu_ai.resolved_taxonomy_rules(e2.legal_entity_code, 'DOMAIN') t
                  WHERE t.id = d))
  FROM edu_ai.qa_entries e
 WHERE e.legal_entity_code IS NOT NULL {entry_entity_filter}
 GROUP BY e.legal_entity_code
 ORDER BY 1;
"""

# file_uuid AND processing_uuid — the pair every retriever joins on. On
# file_uuid alone an active metadata row is joined to every processing run of
# that file, which counts a superseded version's chunks as current.
Q_DOCS = """
SELECT c.legal_entity_code,
       count(DISTINCT c.file_uuid),
       count(*),
       count(*) FILTER (WHERE c.document_domain_id IS NOT NULL
                          AND cardinality(c.document_domain_id) > 0),
       count(DISTINCT c.file_uuid) FILTER (WHERE c.document_domain_id IS NULL
                          OR cardinality(c.document_domain_id) = 0)
  FROM edu_ai.document_chunks c
  JOIN edu_ai.document_metadata m
    ON m.file_uuid = c.file_uuid AND m.processing_uuid = c.processing_uuid
   AND m.is_active
 WHERE c.legal_entity_code IS NOT NULL {chunk_entity_filter}
 GROUP BY c.legal_entity_code
 ORDER BY 1;
"""

# EFFECTIVE area = non-empty user declaration, else the classifier's. The same
# precedence the ingest applies (EDU_AI_LOAD_MARKDOWN), so this is what a chunk
# SHOULD carry — and the next query is where it does not.
Q_DOC_DECL = """
SELECT m.legal_entity_code,
       count(*) FILTER (WHERE cardinality(coalesce(m.document_domain_user, '{{}}')) > 0),
       count(*) FILTER (WHERE cardinality(coalesce(m.document_domain_user, '{{}}')) = 0
                          AND cardinality(coalesce(m.document_domain_llm, '{{}}')) > 0),
       count(*) FILTER (WHERE cardinality(coalesce(m.document_domain_user, '{{}}')) = 0
                          AND cardinality(coalesce(m.document_domain_llm, '{{}}')) = 0)
  FROM edu_ai.document_metadata m
 WHERE m.is_active AND m.legal_entity_code IS NOT NULL {meta_entity_filter}
 GROUP BY m.legal_entity_code
 ORDER BY 1;
"""

# THE DIFFERENCE THAT MATTERS IS NOT "differs" — it is HOW.
#
# The filter is `domain_ids && ARRAY[…]` (array OVERLAP), so array ORDER is
# irrelevant to it. Measured on production 2026-09-06: all 2 782 differing
# qa_variants rows hold the SAME SET in a different order (@> and <@ both hold),
# which is a cosmetic writer inconsistency and changes no result. Reporting that
# as one number next to a real mislabel would make the deployment gate
# meaningless — every deployment would show thousands of "drift" rows and
# everyone would learn to ignore the line.
#
# So each check is split into the three kinds:
#   order-only   same set, different order. Harmless for `&&`. Cosmetic.
#   MISSING      the scanned row has NO label while its source has one. The row
#                becomes unreachable in EVERY area. Measured: 151 chunks of
#                ZS_KAVCI_HORY, whose documents are labelled and whose chunks
#                are not — the "UPDATE on metadata alone is not enough" case.
#   SET DIFFERS  both carry labels and they are not the same set. The row is
#                reachable in the WRONG areas. Measured: 94 chunks of
#                ZS_KAVCI_HORY_ZS.
# Only the last two block switching the picker on for that school.
Q_DRIFT = """
WITH qv AS (
  SELECT coalesce(v.domain_ids, '{{}}') AS scanned,
         coalesce(e.domain_ids, '{{}}') AS source
    FROM edu_ai.qa_variants v JOIN edu_ai.qa_entries e ON e.entry_id = v.entry_id
   WHERE v.is_active AND e.is_active
     AND coalesce(v.domain_ids, '{{}}') IS DISTINCT FROM coalesce(e.domain_ids, '{{}}')),
qe AS (
  SELECT coalesce(q.domain_ids, '{{}}') AS scanned,
         coalesce(e.domain_ids, '{{}}') AS source
    FROM edu_ai.qa_embedding q JOIN edu_ai.qa_entries e ON e.entry_id = q.entry_id
   WHERE q.is_active AND e.is_active
     AND coalesce(q.domain_ids, '{{}}') IS DISTINCT FROM coalesce(e.domain_ids, '{{}}')),
-- the chunk against its document's EFFECTIVE declaration (user, else LLM — the
-- precedence the ingest applies). The one the migration has to keep true: an
-- UPDATE on document_metadata alone leaves every chunk carrying the old labels,
-- and the chunk is what retrieval filters on.
dc AS (
  SELECT coalesce(c.document_domain_id, '{{}}') AS scanned,
         CASE WHEN cardinality(coalesce(m.document_domain_user, '{{}}')) > 0
              THEN m.document_domain_user
              ELSE coalesce(m.document_domain_llm, '{{}}') END AS source
    FROM edu_ai.document_chunks c
    JOIN edu_ai.document_metadata m
      ON m.file_uuid = c.file_uuid AND m.processing_uuid = c.processing_uuid
     AND m.is_active
   WHERE coalesce(c.document_domain_id, '{{}}') IS DISTINCT FROM
         CASE WHEN cardinality(coalesce(m.document_domain_user, '{{}}')) > 0
              THEN m.document_domain_user
              ELSE coalesce(m.document_domain_llm, '{{}}') END)
SELECT 'qa_variants vs qa_entries (active)', count(*)::text,
       count(*) FILTER (WHERE scanned @> source AND scanned <@ source)::text,
       count(*) FILTER (WHERE cardinality(scanned) = 0
                          AND cardinality(source) > 0)::text,
       count(*) FILTER (WHERE cardinality(scanned) > 0 AND cardinality(source) > 0
                          AND NOT (scanned @> source AND scanned <@ source))::text
  FROM qv
UNION ALL
SELECT 'qa_embedding vs qa_entries (active)', count(*)::text,
       count(*) FILTER (WHERE scanned @> source AND scanned <@ source)::text,
       count(*) FILTER (WHERE cardinality(scanned) = 0
                          AND cardinality(source) > 0)::text,
       count(*) FILTER (WHERE cardinality(scanned) > 0 AND cardinality(source) > 0
                          AND NOT (scanned @> source AND scanned <@ source))::text
  FROM qe
UNION ALL
SELECT 'chunks vs document declaration', count(*)::text,
       count(*) FILTER (WHERE scanned @> source AND scanned <@ source)::text,
       count(*) FILTER (WHERE cardinality(scanned) = 0
                          AND cardinality(source) > 0)::text,
       count(*) FILTER (WHERE cardinality(scanned) > 0 AND cardinality(source) > 0
                          AND NOT (scanned @> source AND scanned <@ source))::text
  FROM dc
UNION ALL
SELECT 'active packs with NO area label',
       count(*)::text, '0', count(*)::text, '0'
  FROM edu_ai.qa_pack_manifest m
 WHERE m.is_active
   AND (m.domain_ids IS NULL OR cardinality(m.domain_ids) = 0);
"""

# WHERE the blocking kinds sit, so the report names the schools rather than
# leaving someone to write this query themselves.
Q_DRIFT_WHERE = """
WITH dc AS (
  SELECT c.legal_entity_code,
         coalesce(c.document_domain_id, '{{}}') AS scanned,
         CASE WHEN cardinality(coalesce(m.document_domain_user, '{{}}')) > 0
              THEN m.document_domain_user
              ELSE coalesce(m.document_domain_llm, '{{}}') END AS source
    FROM edu_ai.document_chunks c
    JOIN edu_ai.document_metadata m
      ON m.file_uuid = c.file_uuid AND m.processing_uuid = c.processing_uuid
     AND m.is_active
   WHERE c.legal_entity_code IS NOT NULL {chunk_entity_filter})
SELECT legal_entity_code,
       count(*) FILTER (WHERE cardinality(scanned) = 0
                          AND cardinality(source) > 0)::text,
       count(*) FILTER (WHERE cardinality(scanned) > 0 AND cardinality(source) > 0
                          AND NOT (scanned @> source AND scanned <@ source))::text
  FROM dc
 GROUP BY 1
HAVING count(*) FILTER (WHERE cardinality(scanned) = 0
                          AND cardinality(source) > 0) > 0
    OR count(*) FILTER (WHERE cardinality(scanned) > 0 AND cardinality(source) > 0
                          AND NOT (scanned @> source AND scanned <@ source)) > 0
 ORDER BY 1;
"""

# Through the ROLE and VALIDITY filters, per role — what a caller of that role
# can actually reach in an area, which is not the same as what is tagged.
Q_REACHABLE = """
WITH roles AS (SELECT code FROM edu_ai.code_list
                WHERE list_code = 'role' AND is_active)
SELECT r.code,
       (SELECT count(*) FROM edu_ai.document_chunks c
          JOIN edu_ai.document_metadata m
            ON m.file_uuid = c.file_uuid AND m.processing_uuid = c.processing_uuid
           AND m.is_active
         WHERE c.visibility IN ('entity', 'organization')
           AND c.audience_roles && ARRAY[r.code]
           AND (c.valid_from IS NULL OR c.valid_from <= CURRENT_DATE)
           AND (c.valid_to   IS NULL OR c.valid_to   >= CURRENT_DATE)
           {chunk_entity_filter}),
       (SELECT count(*) FROM edu_ai.document_chunks c
          JOIN edu_ai.document_metadata m
            ON m.file_uuid = c.file_uuid AND m.processing_uuid = c.processing_uuid
           AND m.is_active
         WHERE c.visibility IN ('entity', 'organization')
           AND c.audience_roles && ARRAY[r.code]
           AND (c.valid_from IS NULL OR c.valid_from <= CURRENT_DATE)
           AND (c.valid_to   IS NULL OR c.valid_to   >= CURRENT_DATE)
           AND cardinality(coalesce(c.document_domain_id, '{{}}')) > 0
           {chunk_entity_filter}),
       (SELECT count(*) FROM edu_ai.qa_variants v
          JOIN edu_ai.qa_entries e ON e.entry_id = v.entry_id
         WHERE v.is_active AND e.is_active
           AND v.visibility IN ('entity', 'organization')
           AND (v.audience_roles IS NULL OR v.audience_roles && ARRAY[r.code])
           {variant_entity_filter}),
       (SELECT count(*) FROM edu_ai.qa_variants v
          JOIN edu_ai.qa_entries e ON e.entry_id = v.entry_id
         WHERE v.is_active AND e.is_active
           AND v.visibility IN ('entity', 'organization')
           AND (v.audience_roles IS NULL OR v.audience_roles && ARRAY[r.code])
           AND cardinality(coalesce(v.domain_ids, '{{}}')) > 0
           {variant_entity_filter})
  FROM roles r ORDER BY 1;
"""


def _filters(entity: str | None) -> dict:
    """The one entity predicate, per alias. Inlined because psql gets SQL on
    stdin and cannot take a parameter; the value is validated by the caller."""
    if not entity:
        return {k: "" for k in ("entity_filter", "pack_entity_filter",
                                "entry_entity_filter", "chunk_entity_filter",
                                "meta_entity_filter", "variant_entity_filter")}
    e = entity.replace("'", "''")
    return {
        "entity_filter": f"AND e.legal_entity_code = '{e}'",
        "pack_entity_filter": f"AND m.legal_entity_code = '{e}'",
        "entry_entity_filter": f"AND e.legal_entity_code = '{e}'",
        "chunk_entity_filter": f"AND c.legal_entity_code = '{e}'",
        "meta_entity_filter": f"AND m.legal_entity_code = '{e}'",
        "variant_entity_filter": f"AND v.legal_entity_code = '{e}'",
    }


def main() -> int:
    global _remote
    ap = argparse.ArgumentParser(
        description="READ-ONLY area-tagging inventory per school.")
    ap.add_argument("--remote", metavar="SSH_HOST",
                    help="run against that host's /opt/edu_ai stack (read-only)")
    ap.add_argument("--entity", help="one legal_entity_code instead of all")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output for a release note")
    args = ap.parse_args()
    _remote = args.remote

    if args.entity and not args.entity.replace("_", "").replace("-", "").isalnum():
        sys.exit("--entity must be a plain legal_entity_code")
    f = _filters(args.entity)

    where = args.remote or "local dev stack"
    print("=" * 74)
    print(f"AREA-TAGGING INVENTORY — {where}"
          + (f", entity={args.entity}" if args.entity else ""))
    print("READ-ONLY. Codes, counts and identifiers only — no questions, no "
          "answers, no document text.")
    print("=" * 74)
    stamp = psql("SELECT now()::text, current_database(), version();")
    print(f"   time={stamp[0][0]}  db={stamp[0][1]}")
    print(f"   server={stamp[0][2].split(' on ')[0]}")

    out: dict = {"target": where, "entity": args.entity, "at": stamp[0][0]}

    rows = run(Q_ENTITIES, f)
    table("entities", ["entity", "kind", "level", "parent"], rows)
    out["entities"] = rows

    rows = run(Q_CATALOG, f)
    table("DOMAIN catalog per school", ["entity", "resolved", "own", "codes"], rows,
          "own=0 means the school INHERITS; nearest-owner-exclusive, so a school "
          "with own>0 cannot use a global code that is not also its own")
    out["catalog"] = rows

    rows = run(Q_PACKS, f)
    table("active QA packs", ["entity", "pack", "vis", "roles", "n_dom", "areas",
                              "entries", "untagged", "differing"], rows,
          "untagged = active answers with no area (invisible under any explicit "
          "filter); differing = answers whose own areas are NOT the pack's "
          "(the mixed pack a curator has to split or label per entry)")
    out["packs"] = rows

    rows = run(Q_ENTRY_IDS, f)
    table("QA answers", ["entity", "active", "with area", "unresolvable id"], rows,
          "unresolvable id = an area id the answer's own school cannot resolve; "
          "it matches nothing and the application never says so")
    out["entries"] = rows

    rows = run(Q_DOCS, f)
    table("documents — ACTIVE versions (file_uuid + processing_uuid)",
          ["entity", "docs", "chunks", "chunks w/ area", "docs w/o area"], rows)
    out["documents"] = rows

    rows = run(Q_DOC_DECL, f)
    table("document declarations (the ingest's precedence)",
          ["entity", "user-declared", "LLM only", "neither"], rows,
          "user > LLM is what the ingest propagates to the chunks; 'neither' is "
          "the set a curator has to decide about")
    out["declarations"] = rows

    rows = run(Q_DRIFT, f)
    table("DRIFT — denormalised copies that disagree",
          ["check", "differing", "order only", "MISSING label", "SET differs"], rows,
          "'order only' is harmless — the filter is array OVERLAP, so order "
          "cannot change a result. MISSING = the scanned row has no label while "
          "its source has one, so it is unreachable in EVERY area. SET differs = "
          "reachable in the WRONG areas. Only those two block the picker.")
    out["drift"] = rows

    rows = run(Q_DRIFT_WHERE, f)
    table("…and WHICH schools carry the blocking kinds",
          ["entity", "chunks MISSING label", "chunks with a DIFFERENT set"], rows,
          "these are the schools whose chunk labels must be re-propagated from "
          "their documents before their picker is switched on")
    out["drift_by_entity"] = rows

    rows = run(Q_REACHABLE, f)
    table("REACHABLE through role + validity (not merely tagged)",
          ["role", "chunks", "of which tagged", "QA variants", "of which tagged"],
          rows,
          "private rows are excluded (they belong to one owner); this is the "
          "corpus a caller of that role actually sees")
    out["reachable"] = rows

    print("\n" + "=" * 74)
    print("Untagged content is NOT a defect this report can fix: which area a "
          "source belongs to")
    print("is a curatorial decision (spec.search-domain-scope §6). Write it down, "
          "then apply it")
    print("with scripts/domain_migration.py --plan <file> (dry-run by default).")
    if args.json:
        print("\n--- JSON ---")
        print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
