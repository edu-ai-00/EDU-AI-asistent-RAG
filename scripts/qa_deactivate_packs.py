#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-08-21).
"""List and retire curated QA packs. There is no API for it — /v1/ingest only adds.

    python3 scripts/qa_deactivate_packs.py                       # what is loaded
    python3 scripts/qa_deactivate_packs.py --all                 # dry run
    python3 scripts/qa_deactivate_packs.py --all --go            # retire everything
    python3 scripts/qa_deactivate_packs.py --all --keep provoz-zs --go
    python3 scripts/qa_deactivate_packs.py --pack cs-curated-zs --entity SKOLA_NOVA_VES_ZS --go

SOFT, never DELETE — the same invalidation the ingest itself uses (`is_active`),
for the same reasons: question_answer_log.matched_entry_id keeps pointing at a
real row, and a mistake is one UPDATE away from being undone.

Deactivation touches all three tables carrying the flag. qa_variants and
qa_embedding are not optional: the lexical indexes are partial on is_active
(migration 149) and the HNSW one always was, so an entry left active in either
table stays matchable after its entry row is gone.

Dry run by default. --go is the only way anything is written.
"""
from __future__ import annotations
import argparse, re, subprocess, sys

PG = ["docker", "exec", "-i", "edu_ai_postgres", "psql", "-U", "edu_ai", "-d", "edu_ai",
      "-v", "ON_ERROR_STOP=1"]
# The server runs the stack from /opt/edu_ai under compose, and SSH passes stdin
# straight through, so the remote form is the local one with a wrapper — the
# same pattern the deploy procedure uses to pipe migrations in.
REMOTE_PSQL = ("cd /opt/edu_ai && docker compose exec -T postgres "
               "psql -U edu_ai -d edu_ai -v ON_ERROR_STOP=1")
# Both are validated by the DB anyway; checked here because these values are
# INLINED into the SQL below and a shell-quoting bug is not an acceptable way to
# find that out. Same rule as migration 143 for pack_id.
IDENT_RX = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


REMOTE_HOST = None          # set from --remote


def psql(sql: str, tuples_only: bool = False) -> str:
    """SQL on stdin, not -c: `-c` sends the whole string as ONE query, which
    Postgres wraps in an implicit transaction, so the explicit BEGIN below would
    warn and ON COMMIT DROP would hang off a block this script did not open."""
    flags = " -t -A -F '\\t'" if tuples_only else ""
    if REMOTE_HOST:
        cmd = ["ssh", "-o", "BatchMode=yes", REMOTE_HOST, REMOTE_PSQL + flags + " -f -"]
    else:
        cmd = PG + (["-t", "-A", "-F", "\t"] if tuples_only else []) + ["-f", "-"]
    r = subprocess.run(cmd, input=sql, capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        sys.exit(f"  ! psql selhal:\n{r.stderr.strip()}")
    return r.stdout


def _lit(values) -> str:
    for v in values:
        if not IDENT_RX.match(v):
            sys.exit(f"  ! {v!r} není platný kód — povoleno jen [A-Za-z0-9_-], max 64")
    return "ARRAY[" + ",".join(f"'{v}'" for v in values) + "]"


def selector(a) -> str:
    """The WHERE fragment naming the packs to retire. Written against columns
    both qa_entries and qa_pack_manifest carry, so one string serves both."""
    where = ["is_active"]
    if a.pack:
        where.append(f"pack_id = ANY({_lit(a.pack)})")
    if a.entity:
        where.append(f"legal_entity_code = ANY({_lit(a.entity)})")
    if a.keep:
        where.append(f"NOT (pack_id = ANY({_lit(a.keep)}))")
    return " AND ".join(where)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="všechny aktivní balíčky")
    ap.add_argument("--pack", action="append", default=[], help="pack_id (lze opakovat)")
    ap.add_argument("--entity", action="append", default=[], help="legal_entity_code")
    ap.add_argument("--keep", action="append", default=[], help="pack_id, který zůstane")
    ap.add_argument("--go", action="store_true", help="skutečně zneaktivnit")
    ap.add_argument("--remote", metavar="HOST",
                    help="ssh alias serveru (např. edu-ai) místo lokálního kontejneru")
    a = ap.parse_args()

    global REMOTE_HOST
    REMOTE_HOST = a.remote

    print(f"\n  Aktivní QA balíčky — {a.remote or 'lokálně'}\n" + "=" * 66)
    listing = psql(
        "SELECT legal_entity_code, pack_id, count(*) AS entries "
        "FROM edu_ai.qa_entries WHERE is_active "
        "GROUP BY 1, 2 ORDER BY 1, 2")
    print(listing.rstrip() or "  (žádné)")

    if not (a.all or a.pack or a.entity):
        print("\n  Nic nevybráno — pro odstranění přidej --all nebo --pack/--entity.")
        return 0
    if a.all and (a.pack or a.entity):
        sys.exit("  ! --all a --pack/--entity se vylučují")

    where = selector(a)
    hit = psql(f"SELECT legal_entity_code || '  ' || pack_id || '  ' || count(*) "
               f"FROM edu_ai.qa_entries WHERE {where} "
               f"GROUP BY legal_entity_code, pack_id ORDER BY 1",
               tuples_only=True).strip()
    if not hit:
        print("\n  Výběru neodpovídá žádný aktivní balíček — nic k udělání.")
        return 0

    print(f"\n  {'DRY RUN — ' if not a.go else '*** ZNEAKTIVŇUJI *** '}"
          f"vybráno:\n" + "\n".join("    " + l for l in hit.splitlines()))
    if not a.go:
        print("\n  (nic nezměněno — pro provedení přidej --go)")
        return 0

    # One transaction: the three entry tables must not be able to disagree about
    # which entries are live, and the manifest must not outlive its entries.
    psql(f"""
        BEGIN;
        CREATE TEMP TABLE _doomed ON COMMIT DROP AS
            SELECT entry_id FROM edu_ai.qa_entries WHERE {where};
        UPDATE edu_ai.qa_entries   SET is_active = false
         WHERE is_active AND entry_id IN (SELECT entry_id FROM _doomed);
        UPDATE edu_ai.qa_variants  SET is_active = false
         WHERE is_active AND entry_id IN (SELECT entry_id FROM _doomed);
        UPDATE edu_ai.qa_embedding SET is_active = false
         WHERE is_active AND entry_id IN (SELECT entry_id FROM _doomed);
        UPDATE edu_ai.qa_pack_manifest SET is_active = false WHERE {where};
        COMMIT;
    """)

    print("\n  Hotovo. Zbývá aktivních:")
    print(psql("SELECT legal_entity_code, pack_id, count(*) AS entries "
               "FROM edu_ai.qa_entries WHERE is_active "
               "GROUP BY 1, 2 ORDER BY 1, 2").rstrip() or "  (žádné)")
    left = psql("SELECT count(*) FROM edu_ai.qa_variants v WHERE v.is_active "
                "AND NOT EXISTS (SELECT 1 FROM edu_ai.qa_entries e "
                "WHERE e.entry_id = v.entry_id AND e.is_active)",
                tuples_only=True).strip()
    print(f"\n  Osiřelých aktivních variant: {left}  (musí být 0)")
    return 0 if left == "0" else 1


if __name__ == "__main__":
    sys.exit(main())
