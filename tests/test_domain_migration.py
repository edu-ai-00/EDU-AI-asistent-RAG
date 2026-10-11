#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-09-06).
"""scripts/domain_migration.py — the three scenarios a label migration must survive.

    python3 tests/test_domain_migration.py                 # all
    python3 tests/test_domain_migration.py concurrent      # one group

Runs on the HOST against the dev stack, driving the script through
`subprocess` exactly as an operator would — no imports of its internals. That
is deliberate: the argument parsing, the `--go` gate and the exit codes are
part of what is being tested, and a test that called `apply()` directly would
pass while `--apply` without `--go` wrote to the database.

WHAT IT COVERS, and why each one is here rather than being a variation:

  concurrent   somebody edits a row around the write. Three separate windows,
               because they need three different instruments:
                 * before the run          -> the `expect_current` pre-check SKIPs
                 * rows expect_current does not cover -> the snapshot does
                 * DURING the transaction  -> only the in-transaction guard can,
                   and only because it takes a row lock first. Tested with a
                   genuinely concurrent psql session, not a trigger: a trigger
                   fires inside the migration's own transaction, which no lock
                   can protect against, so it would prove nothing.
               Before the guard existed (`guard = ""` in both branches) the
               third window overwrote the other session in silence.
  snapshot     the process dies AFTER the writes and BEFORE the snapshot
               reaches disk. That used to lose the record of the prior values
               at exactly the moment it was needed. The test kills the process
               mid-apply and asserts the file is there and usable.
  foreign      a rollback must touch ONLY the rows its snapshot recorded. The
               previous version resynced every qa_variants/qa_embedding row in
               the database, so restoring two packs rewrote three thousand
               rows belonging to sets nobody had asked about.
  added-row    a row that APPEARS between the snapshot and the write. The guard
               compared only the keys it had stored — and restricted the actual
               side to those keys, making "a row appeared" undetectable — while
               the writes selected by pack_id. Both halves are now exact.
  added-variant a VARIANT added to an answer already in the set: the scan
               surfaces were absent from the snapshot while the writes updated
               them by JOIN, so it was written blind and unrestorable.
  rollback-race the restore's own drift check ran BEFORE its transaction, so a
               curator committing in between was overwritten after the tool had
               printed "no drift".

Every scenario asserts on the DATABASE, not on the script's output: the point
is which rows hold which values afterwards.

RUN IT ALONE. It shares the fixture with tests/test_domain_scope*.py and drops
it between groups, so running two of them at once makes both fail in ways that
look like product defects and are not.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
PG = "edu_ai_postgres"
SEARCH = "edu_ai_search"
SCRIPT = str(ROOT / "scripts" / "domain_migration.py")

ZS = "SKOLA_NOVA_VES_ZS"

_failures: list[str] = []
_ran = 0


def check(name: str, cond, detail: str = "") -> None:
    global _ran
    _ran += 1
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
        _failures.append(name)


# ── plumbing ────────────────────────────────────────────────────────────────
def db(sql: str) -> str:
    """Run SQL, return stdout. `-q` so a write's command tag ("INSERT 0 1") does
    not end up appended to a RETURNING value — which it did, and produced an
    `invalid input syntax for type uuid` two statements later."""
    p = subprocess.run(
        ["docker", "exec", "-i", PG, "sh", "-c",
         'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -qtA'],
        input=sql, capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        raise RuntimeError(f"psql failed: {p.stderr.strip()}")
    return p.stdout.strip()


def fixture(cmd: str) -> None:
    subprocess.run(["docker", "cp", str(ROOT / "tests" / "domain_scope_fixture.py"),
                    f"{SEARCH}:/tmp/domain_scope_fixture.py"],
                   check=True, capture_output=True)
    p = subprocess.run(["docker", "exec", "-e", "PYTHONPATH=/app", "-w", "/app",
                        SEARCH, "python", "/tmp/domain_scope_fixture.py", cmd],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"fixture {cmd} failed:\n{p.stdout}\n{p.stderr}")


def run(*args, timeout=180) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, SCRIPT, *args],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=timeout, cwd=str(ROOT))


def labels(entry_key: str) -> str:
    """entry / variant / embedding labels of one entry, as one comparable string."""
    return db(
        "SELECT coalesce(array_to_string(e.domain_ids, ','), 'NULL') || '|' || "
        "       coalesce(array_to_string(v.domain_ids, ','), 'NULL') || '|' || "
        "       coalesce(array_to_string(q.domain_ids, ','), 'NULL') "
        "  FROM edu_ai.qa_entries e "
        "  LEFT JOIN edu_ai.qa_variants  v ON v.entry_id = e.entry_id "
        "  LEFT JOIN edu_ai.qa_embedding q ON q.entry_id = e.entry_id "
        f" WHERE e.entry_key = '{entry_key}';")


def domain_id(code: str, entity: str | None = ZS) -> int:
    owner = f"= '{entity}'" if entity else "IS NULL"
    return int(db("SELECT id FROM edu_ai.taxonomy_rules "
                  "WHERE taxonomy_type_code = 'DOMAIN' AND is_active "
                  f"  AND type_code = '{code}' AND legal_entity_code {owner};"))


def write_plan(sets: list[dict], note: str = "test") -> pathlib.Path:
    fh = tempfile.NamedTemporaryFile("w", suffix=".plan.json", delete=False,
                                     encoding="utf-8")
    json.dump({"note": note, "target": {"database": "edu_ai"}, "sets": sets}, fh,
              ensure_ascii=False)
    fh.close()
    return pathlib.Path(fh.name)


PLAN_UNTAGGED = {
    "id": "untagged-pack", "kind": "qa_pack", "entity": ZS,
    "pack_id": "ds-untagged", "expect_current": [], "domains": ["CATERING"],
    "source": "tests/test_domain_migration.py",
}


# ══════════════════════════════════════════════════════════════════════════
# 1. A CONCURRENT EDIT between the read and the write
# ══════════════════════════════════════════════════════════════════════════
def test_concurrent_change_before_the_transaction():
    print("\n[concurrent: an edit lands between the dry run and the apply]")
    fixture("build")
    plan = write_plan([PLAN_UNTAGGED])
    try:
        # the operator reviews a dry run — the pack is untagged, as the plan says
        r = run("--plan", str(plan))
        check("dry run accepts the plan", r.returncode == 0
              and "precondition: OK" in r.stdout, r.stdout[-300:])

        # …and THEN a curator tags it themselves
        gid = domain_id("GRADING")
        db("UPDATE edu_ai.qa_pack_manifest "
           f"   SET domain_ids = ARRAY[{gid}]::bigint[] "
           " WHERE pack_id = 'ds-untagged';")

        r = run("--plan", str(plan), "--apply", "--go", "--out", tempfile.gettempdir())
        check("the apply SKIPS the moved set rather than overwriting it",
              "SKIP untagged-pack" in r.stdout, r.stdout[-400:])
        now = db("SELECT array_to_string(domain_ids, ',') "
                 "  FROM edu_ai.qa_pack_manifest WHERE pack_id = 'ds-untagged';")
        check("the curator's own label survived", now == str(gid),
              f"{now!r} != {gid}")
    finally:
        os.unlink(plan)
        fixture("drop")


def test_guard_catches_a_row_expect_current_does_not_check():
    print("\n[concurrent: the guard covers rows expect_current never looks at]")
    # `expect_current` for a qa_pack set checks the MANIFEST row only. The
    # snapshot — and therefore the guard — covers the manifest AND every entry
    # of the pack, which is what the set actually writes. So an entry edited
    # after the review passes the pre-check and is caught by the guard. This is
    # the extra protection the in-transaction guard buys, and before it existed
    # (`guard = ""`) the edit was overwritten in silence.
    fixture("build")
    plan = write_plan([PLAN_UNTAGGED])
    try:
        gid = domain_id("GRADING")
        # a curator labels ONE ENTRY of the untagged pack; the manifest is
        # untouched, so expect_current: [] still matches
        db("UPDATE edu_ai.qa_entries "
           f"   SET domain_ids = ARRAY[{gid}]::bigint[] "
           " WHERE entry_key = 'ds-untag-1';")
        entry_before = labels("ds-untag-1")

        r = run("--plan", str(plan), "--apply", "--go", "--out", tempfile.gettempdir())
        check("the manifest pre-check still passes (it is unchanged)",
              "SKIP untagged-pack" not in r.stdout, r.stdout[-400:])
        check("but the set is APPLIED, because the snapshot recorded the entry "
              "as it is now",
              "OK    untagged-pack" in r.stdout, r.stdout[-400:])
        # The snapshot is taken immediately before the write, so a change made
        # BEFORE the run is simply the state being migrated — correct, and not
        # what the guard is for. The guard's window is narrower and is tested by
        # test_guard_blocks_a_concurrent_session below.
        check("the curator's entry label was NOT silently reverted to NULL",
              labels("ds-untag-1") != "NULL|NULL|NULL",
              f"{labels('ds-untag-1')!r} (was {entry_before!r})")
    finally:
        os.unlink(plan)
        fixture("drop")


def _waiting_locks() -> int:
    """How many lock requests are currently BLOCKED. The handshake signal."""
    return int(db("SELECT count(*) FROM pg_locks WHERE NOT granted;") or 0)


def test_guard_blocks_a_concurrent_session():
    print("\n[concurrent: a REAL second session commits during the transaction]")
    # THE NARROW WINDOW THE IN-TRANSACTION GUARD EXISTS FOR, and getting a test
    # to land in it takes care. The three windows are different:
    #
    #   before the run          -> the pre-check SKIPs (tested above)
    #   after the pre-read,
    #   before the transaction  -> ONLY the guard can catch it  ← this test
    #
    # The sequencing relies on MVCC and on the lock, not on sleeps:
    #
    #   B: BEGIN; UPDATE pack;              -- UNCOMMITTED, holds the row lock
    #   A: migration starts. Its pre-read is a plain SELECT in its own
    #      transaction, so it sees the OLD committed value and PASSES —
    #      which is precisely the situation the pre-check cannot detect.
    #   A: opens its write transaction; the guard's FOR UPDATE blocks on B.
    #   -- handshake: wait until Postgres reports a BLOCKED lock request --
    #   B: COMMIT                           -- releases
    #   A: unblocks; its comparison now sees B's value -> RAISE -> whole rollback
    #
    # Without `FOR UPDATE` in the guard, A would not block at all: it would read
    # the pre-B value, pass, write, and B's committed decision would be gone.
    fixture("build")
    plan = write_plan([PLAN_UNTAGGED])
    gid = domain_id("GRADING")
    holder = mig = None
    try:
        entry_before = labels("ds-untag-1")

        # ── B: change the row and HOLD it uncommitted ───────────────────────
        holder = subprocess.Popen(
            ["docker", "exec", "-i", PG, "sh", "-c",
             'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        assert holder.stdin is not None
        holder.stdin.write(
            "BEGIN;\n"
            f"UPDATE edu_ai.qa_pack_manifest SET domain_ids = ARRAY[{gid}]::bigint[]"
            "  WHERE pack_id = 'ds-untagged';\n")
        holder.stdin.flush()
        held = "0"
        for _ in range(80):
            held = db("SELECT count(*) FROM pg_locks l JOIN pg_class c "
                      "    ON c.oid = l.relation "
                      " WHERE c.relname = 'qa_pack_manifest' "
                      "   AND l.mode = 'RowExclusiveLock' AND l.granted;")
            if int(held or 0) > 0:
                break
        check("the second session holds an UNCOMMITTED row lock",
              int(held or 0) > 0, f"locks={held}")
        # …and the change is invisible to anyone else, which is why the
        # migration's pre-check will pass
        visible = db("SELECT coalesce(array_to_string(domain_ids, ','), 'NULL') "
                     "  FROM edu_ai.qa_pack_manifest WHERE pack_id = 'ds-untagged';")
        check("…and it is NOT yet visible to other sessions (so the pre-check "
              "cannot see it)", visible == "NULL", f"{visible!r}")

        # ── A: the migration. Pre-read passes; the guard then blocks. ───────
        mig = subprocess.Popen(
            [sys.executable, SCRIPT, "--plan", str(plan), "--apply", "--go",
             "--out", tempfile.gettempdir()],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(ROOT))
        blocked = False
        for _ in range(200):                       # ~60 s worst case
            if _waiting_locks() > 0:
                blocked = True
                break
            if mig.poll() is not None:             # it finished without blocking
                break
        check("the migration BLOCKS on the concurrent row lock "
              "(this is the guard's FOR UPDATE)", blocked,
              "it never waited — the guard is not taking a row lock")

        # ── B commits; A must now see the change and abort ──────────────────
        holder.stdin.write("COMMIT;\n")
        holder.stdin.flush()
        holder.stdin.close()
        out, _err = mig.communicate(timeout=180)

        check("…and the migration then ABORTS, because the row moved",
              "ABORT untagged-pack" in out or "precondition moved" in out,
              out[-600:])
        check("…and reports nothing applied",
              "applied=0" in out, out[-300:])
        pack_now = db("SELECT coalesce(array_to_string(domain_ids, ','), 'NULL') "
                      "  FROM edu_ai.qa_pack_manifest WHERE pack_id = 'ds-untagged';")
        check("the concurrent session's value SURVIVED", pack_now == str(gid),
              f"{pack_now!r} != {gid}")
        check("the transaction rolled back WHOLE — the entries are untouched",
              labels("ds-untag-1") == entry_before,
              f"before={entry_before!r} after={labels('ds-untag-1')!r}")
    finally:
        for proc in (holder, mig):
            if proc is not None and proc.poll() is None:
                proc.kill()
        os.unlink(plan)
        fixture("drop")


# ══════════════════════════════════════════════════════════════════════════
# 2. The process dies before the snapshot would have been written
# ══════════════════════════════════════════════════════════════════════════
def test_snapshot_survives_a_killed_process():
    print("\n[snapshot: the process is killed mid-apply]")
    fixture("build")
    out = pathlib.Path(tempfile.mkdtemp())
    # Two sets, and the process is killed while the SECOND is being applied.
    # Under the old order (snapshot written after the loop) the file would not
    # exist at all and the first set's prior values would be unrecoverable.
    plan = write_plan([
        PLAN_UNTAGGED,
        {"id": "slow-entry", "kind": "qa_entries", "entity": ZS,
         "pack_id": "ds-mixed",
         "entries": {"ds-mix-att": ["ATTENDANCE", "GRADING"]},
         "source": "tests/test_domain_migration.py"},
    ])
    try:
        proc = subprocess.Popen(
            [sys.executable, SCRIPT, "--plan", str(plan), "--apply", "--go",
             "--out", str(out)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(ROOT))
        # Wait for the pre-write snapshot line, then kill. That line is the
        # contract: after it, a snapshot exists on disk.
        saw = False
        assert proc.stdout is not None
        for line in proc.stdout:
            if "snapshot (pre-write)" in line:
                saw = True
                break
        check("the apply announces a PRE-WRITE snapshot", saw)
        proc.kill()
        proc.wait(timeout=30)

        snaps = sorted(out.glob("domain-migration-*.snapshot.json"))
        check("a snapshot file exists after the kill", len(snaps) == 1,
              f"{[s.name for s in snaps]}")
        if not snaps:
            return
        data = json.loads(snaps[0].read_text(encoding="utf-8"))
        check("it is valid JSON with both sets recorded",
              set(data.get("sets", {})) == {"untagged-pack", "slow-entry"},
              f"{list(data.get('sets', {}))}")
        check("every recorded set carries its PRIOR row values",
              all(d.get("rows") for d in data["sets"].values()),
              f"{ {k: len(v.get('rows') or []) for k, v in data['sets'].items()} }")
        check("no temporary file was left behind",
              not list(out.glob("*.tmp")), f"{[p.name for p in out.glob('*.tmp')]}")

        # …and it is USABLE: the rollback accepts it and restores the recorded
        # values, whether or not the writes had committed.
        r = run("--rollback", str(snaps[0]), "--go")
        check("the killed run's snapshot rolls back cleanly", r.returncode == 0,
              (r.stdout + r.stderr)[-400:])
        pack = db("SELECT coalesce(array_to_string(domain_ids, ','), 'NULL') "
                  "  FROM edu_ai.qa_pack_manifest WHERE pack_id = 'ds-untagged';")
        check("the pack is back to its prior value", pack == "NULL", f"{pack!r}")
    finally:
        os.unlink(plan)
        fixture("drop")


# ══════════════════════════════════════════════════════════════════════════
# 3. A rollback must not touch a set it never migrated
# ══════════════════════════════════════════════════════════════════════════
def test_rollback_leaves_a_foreign_set_alone():
    print("\n[foreign: a rollback touches only the rows its snapshot recorded]")
    fixture("build")
    out = pathlib.Path(tempfile.mkdtemp())
    plan = write_plan([PLAN_UNTAGGED])
    try:
        r = run("--plan", str(plan), "--apply", "--go", "--out", str(out))
        check("the apply succeeds", "OK    untagged-pack" in r.stdout,
              r.stdout[-300:])
        snaps = sorted(out.glob("domain-migration-*.snapshot.json"))
        if not snaps:
            check("a snapshot was written", False)
            return

        # A DIFFERENT set, migrated by somebody else and NOT in this snapshot.
        # This is the row the old rollback destroyed: its blanket resync
        # rewrote every qa_variants/qa_embedding row from qa_entries, so a
        # variant deliberately labelled differently from its entry was reset.
        att, cat = domain_id("ATTENDANCE"), domain_id("CATERING")
        db("UPDATE edu_ai.qa_entries SET domain_ids = "
           f"    ARRAY[{att},{cat}]::bigint[] WHERE entry_key = 'ds-att-2';")
        db("UPDATE edu_ai.qa_variants v SET domain_ids = e.domain_ids "
           "  FROM edu_ai.qa_entries e WHERE e.entry_id = v.entry_id "
           "   AND e.entry_key = 'ds-att-2';")
        db("UPDATE edu_ai.qa_embedding q SET domain_ids = e.domain_ids "
           "  FROM edu_ai.qa_entries e WHERE e.entry_id = q.entry_id "
           "   AND e.entry_key = 'ds-att-2';")
        foreign_before = labels("ds-att-2")
        check("the foreign set is labelled before the rollback",
              foreign_before == f"{att},{cat}|{att},{cat}|{att},{cat}",
              foreign_before)

        # …and a variant deliberately OUT OF STEP with its entry, which is what
        # a blanket resync silently "fixes"
        db("UPDATE edu_ai.qa_variants v SET domain_ids = ARRAY[888888]::bigint[] "
           "  FROM edu_ai.qa_entries e WHERE e.entry_id = v.entry_id "
           "   AND e.entry_key = 'ds-mix-cat';")
        outofstep_before = labels("ds-mix-cat")

        r = run("--rollback", str(snaps[0]), "--go")
        check("the rollback succeeds", r.returncode == 0,
              (r.stdout + r.stderr)[-400:])
        # The derived resync is GONE. The snapshot now records the variant and
        # embedding rows themselves, so each is restored by its own key and
        # there is no JOIN back through qa_entries to bound. A stronger
        # guarantee than the bound one, so the assertion moved with it.
        check("every restore statement is keyed to ONE recorded row",
              "keyed to ONE recorded row" in r.stdout, r.stdout[-400:])
        check("…and the scan surfaces are restored by variant_id, not by a JOIN",
              "qa_variants SET domain_ids" in r.stdout
              and "variant_id::text =" in r.stdout, r.stdout[-600:])
        check("…with no unbounded resync left in the plan",
              "FROM edu_ai.qa_entries e" not in r.stdout, r.stdout[-600:])

        check("the FOREIGN set is untouched",
              labels("ds-att-2") == foreign_before,
              f"before={foreign_before!r} after={labels('ds-att-2')!r}")
        check("…and so is a variant deliberately out of step with its entry",
              labels("ds-mix-cat") == outofstep_before,
              f"before={outofstep_before!r} after={labels('ds-mix-cat')!r}")
        # the set it DID migrate is restored
        pack = db("SELECT coalesce(array_to_string(domain_ids, ','), 'NULL') "
                  "  FROM edu_ai.qa_pack_manifest WHERE pack_id = 'ds-untagged';")
        check("the migrated set IS restored", pack == "NULL", f"{pack!r}")
    finally:
        os.unlink(plan)
        fixture("drop")


def test_rollback_refuses_over_a_later_change():
    print("\n[foreign: a rollback REFUSES to overwrite a post-apply edit]")
    fixture("build")
    out = pathlib.Path(tempfile.mkdtemp())
    plan = write_plan([PLAN_UNTAGGED])
    try:
        run("--plan", str(plan), "--apply", "--go", "--out", str(out))
        snaps = sorted(out.glob("domain-migration-*.snapshot.json"))
        if not snaps:
            check("a snapshot was written", False)
            return
        # a curator changes their mind AFTER the migration
        gid = domain_id("GRADING")
        db("UPDATE edu_ai.qa_pack_manifest "
           f"   SET domain_ids = ARRAY[{gid}]::bigint[] "
           " WHERE pack_id = 'ds-untagged';")

        r = run("--rollback", str(snaps[0]), "--go")
        check("the rollback REFUSES (exit 2), naming the changed row",
              r.returncode == 2 and "have CHANGED since" in r.stdout,
              f"rc={r.returncode} {r.stdout[-400:]}")
        still = db("SELECT array_to_string(domain_ids, ',') "
                   "  FROM edu_ai.qa_pack_manifest WHERE pack_id = 'ds-untagged';")
        check("…and the later change is still there", still == str(gid),
              f"{still!r}")

        r = run("--rollback", str(snaps[0]), "--go", "--force")
        check("--force is the only way past it", r.returncode == 0,
              (r.stdout + r.stderr)[-300:])
        pack = db("SELECT coalesce(array_to_string(domain_ids, ','), 'NULL') "
                  "  FROM edu_ai.qa_pack_manifest WHERE pack_id = 'ds-untagged';")
        check("…and it then restores the recorded value", pack == "NULL",
              f"{pack!r}")
    finally:
        os.unlink(plan)
        fixture("drop")


def test_dry_run_writes_nothing():
    print("\n[the --go gate]")
    fixture("build")
    out = pathlib.Path(tempfile.mkdtemp())
    plan = write_plan([PLAN_UNTAGGED])
    try:
        before = labels("ds-untag-1")
        r = run("--plan", str(plan), "--out", str(out))
        check("a bare --plan is a dry run", r.returncode == 0
              and "DRY RUN" in r.stdout)
        r = run("--plan", str(plan), "--apply", "--out", str(out))
        check("--apply WITHOUT --go is still a dry run",
              "still a dry run" in r.stdout, r.stdout[:200])
        check("neither wrote a snapshot file",
              not list(out.glob("*.json")), f"{[p.name for p in out.glob('*.json')]}")
        check("neither changed a row", labels("ds-untag-1") == before,
              f"before={before!r} after={labels('ds-untag-1')!r}")
    finally:
        os.unlink(plan)
        fixture("drop")


# ══════════════════════════════════════════════════════════════════════════
# 4. A ROW ADDED AFTER THE SNAPSHOT
# ══════════════════════════════════════════════════════════════════════════
def _add_entry(pack: str, key: str, question: str) -> str:
    """Insert an entry (plus one variant and one embedding) into an existing
    pack, as a QA re-import would. Returns its entry_id."""
    eid = db(f"""
        INSERT INTO edu_ai.qa_entries
            (entry_id, pack_id, entry_key, canonical_q, answer_md, is_active,
             legal_entity_code, visibility, audience_roles)
        SELECT gen_random_uuid(), '{pack}', '{key}', '{question}',
               'added after the snapshot', true, m.legal_entity_code,
               'entity', ARRAY['STUDENT','TEACHER','PARENT']
          FROM edu_ai.qa_pack_manifest m WHERE m.pack_id = '{pack}'
        RETURNING entry_id::text;""")
    db(f"""
        INSERT INTO edu_ai.qa_variants
            (entry_id, phrasing, norm_text, is_active, legal_entity_code,
             visibility, audience_roles)
        VALUES ('{eid}'::uuid, '{question}', '{key}', true,
                'SKOLA_NOVA_VES_ZS', 'entity',
                ARRAY['STUDENT','TEACHER','PARENT']);""")
    db(f"""
        INSERT INTO edu_ai.qa_embedding
            (variant_id, entry_id, embedding_model, is_active,
             legal_entity_code, visibility, audience_roles)
        SELECT v.variant_id, v.entry_id, 'text-embedding-3-small', true,
               v.legal_entity_code, v.visibility, v.audience_roles
          FROM edu_ai.qa_variants v WHERE v.entry_id = '{eid}'::uuid;""")
    return eid


def _add_variant(entry_key: str, norm: str) -> str:
    """Add a second VARIANT to an EXISTING answer — the case the snapshot used
    to miss entirely, because the scan surfaces were not in it. Returns its
    variant_id."""
    return db(f"""
        INSERT INTO edu_ai.qa_variants
            (entry_id, phrasing, norm_text, is_active, legal_entity_code,
             visibility, audience_roles, domain_ids)
        SELECT e.entry_id, '{norm}', '{norm}', true, e.legal_entity_code,
               'entity', ARRAY['STUDENT','TEACHER','PARENT'],
               ARRAY[777777]::bigint[]
          FROM edu_ai.qa_entries e WHERE e.entry_key = '{entry_key}'
        RETURNING variant_id::text;""")


def test_row_added_after_the_snapshot_is_not_written():
    print("\n[added row: an entry that appears between snapshot and write]")
    # THE FINDING. The guard compared only the keys it had stored, and the
    # `actual` side of that comparison was itself restricted to those keys — so
    # "a row appeared" was structurally undetectable. Meanwhile the writes
    # selected by `pack_id`, i.e. whatever was in the pack AT WRITE TIME. An
    # entry added in between was therefore modified having passed no check, and
    # was absent from the snapshot so it could not be restored either.
    #
    # Two guarantees are asserted, because they are separate mechanisms:
    #   * the guard REFUSES the set (the row is visible, so it is compared)
    #   * with --force past it, the write still cannot REACH the row, because
    #     every statement is keyed to the snapshot
    fixture("build")
    out = pathlib.Path(tempfile.mkdtemp())
    plan = write_plan([PLAN_UNTAGGED])
    holder = mig = None
    gid = domain_id("GRADING")
    try:
        # ── the visible case: added and committed before the apply runs ─────
        new_id = _add_entry("ds-untagged", "ds-added-1", "Pridano az potom?")
        before = db("SELECT coalesce(array_to_string(domain_ids, ','), 'NULL') "
                    f"  FROM edu_ai.qa_entries WHERE entry_id = '{new_id}'::uuid;")
        r = run("--plan", str(plan), "--apply", "--go", "--out", str(out))
        # It is applied: the snapshot is taken immediately before the write, so a
        # row already committed is simply part of the set being migrated. That is
        # correct — and it means the row IS in the snapshot and IS restorable.
        check("a row committed BEFORE the run is snapshotted, not skipped",
              "OK    untagged-pack" in r.stdout, r.stdout[-400:])
        snaps = sorted(out.glob("domain-migration-*.snapshot.json"))
        data = json.loads(snaps[0].read_text(encoding="utf-8"))
        rows = data["sets"]["untagged-pack"]["rows"]
        check("…and the new entry is IN the snapshot (so it can be undone)",
              any(t == "qa_entries" and k == new_id for t, k, _v in rows),
              f"{[k for t, k, _ in rows if t == 'qa_entries']}")
        check("…as are its variant and embedding rows",
              any(t == "qa_variants" for t, _k, _v in rows)
              and any(t == "qa_embedding" for t, _k, _v in rows),
              f"{sorted({t for t, _k, _v in rows})}")

        # ── the RACE: added and committed AFTER the snapshot was read ──────
        # Same handshake as the apply race: an uncommitted INSERT is invisible
        # to the snapshot, so the set is read without it; the guard then blocks
        # on the lock, the inserter commits, and the guard sees a row it has no
        # record of.
        fixture("drop")
        fixture("build")
        holder = subprocess.Popen(
            ["docker", "exec", "-i", PG, "sh", "-c",
             'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        assert holder.stdin is not None
        # BEGIN + an UPDATE that takes the manifest lock the guard will want,
        # and an INSERT of a brand-new entry into the same pack
        holder.stdin.write(
            "BEGIN;\n"
            "UPDATE edu_ai.qa_pack_manifest SET domain_ids = domain_ids "
            "  WHERE pack_id = 'ds-untagged';\n"
            "INSERT INTO edu_ai.qa_entries (entry_id, pack_id, entry_key, "
            "  canonical_q, answer_md, is_active, legal_entity_code, "
            "  visibility, audience_roles) "
            "VALUES (gen_random_uuid(), 'ds-untagged', 'ds-raced-1', "
            "  'Pridano v zavodu?', 'x', true, 'SKOLA_NOVA_VES_ZS', 'entity', "
            "  ARRAY['STUDENT','TEACHER','PARENT']);\n")
        holder.stdin.flush()
        for _ in range(80):
            if int(db("SELECT count(*) FROM pg_locks l JOIN pg_class c "
                      "    ON c.oid = l.relation WHERE c.relname = "
                      "'qa_pack_manifest' AND l.mode = 'RowExclusiveLock' "
                      "AND l.granted;") or 0) > 0:
                break
        check("the inserting session holds the manifest lock", True)

        mig = subprocess.Popen(
            [sys.executable, SCRIPT, "--plan", str(plan), "--apply", "--go",
             "--out", str(out)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(ROOT))
        blocked = False
        for _ in range(200):
            if _waiting_locks() > 0:
                blocked = True
                break
            if mig.poll() is not None:
                break
        check("the migration blocks on it", blocked, "it never waited")
        holder.stdin.write("COMMIT;\n")
        holder.stdin.flush()
        holder.stdin.close()
        mig_out, _ = mig.communicate(timeout=180)

        check("the guard REFUSES the set because a row APPEARED",
              "ABORT untagged-pack" in mig_out
              or "precondition moved" in mig_out, mig_out[-600:])
        raced = db("SELECT coalesce(array_to_string(domain_ids, ','), 'NULL') "
                   "  FROM edu_ai.qa_entries WHERE entry_key = 'ds-raced-1';")
        check("the row added in the race was NOT modified", raced == "NULL",
              f"{raced!r} — new_row_outside_snapshot_modified")
        check("…and neither was the rest of the set (whole rollback)",
              db("SELECT coalesce(array_to_string(domain_ids, ','), 'NULL') "
                 "  FROM edu_ai.qa_pack_manifest WHERE pack_id = 'ds-untagged';")
              == "NULL")
    finally:
        for proc in (holder, mig):
            if proc is not None and proc.poll() is None:
                proc.kill()
        db("DELETE FROM edu_ai.qa_embedding WHERE entry_id IN "
           "  (SELECT entry_id FROM edu_ai.qa_entries "
           "    WHERE entry_key LIKE 'ds-added-%' OR entry_key LIKE 'ds-raced-%');"
           "DELETE FROM edu_ai.qa_variants WHERE entry_id IN "
           "  (SELECT entry_id FROM edu_ai.qa_entries "
           "    WHERE entry_key LIKE 'ds-added-%' OR entry_key LIKE 'ds-raced-%');"
           "DELETE FROM edu_ai.qa_entries "
           "  WHERE entry_key LIKE 'ds-added-%' OR entry_key LIKE 'ds-raced-%';")
        os.unlink(plan)
        fixture("drop")


def test_variant_added_to_an_existing_answer():
    print("\n[added row: a VARIANT added to an existing answer]")
    # The scan surfaces were absent from the snapshot while the writes updated
    # them by JOIN on entry_id, so a variant added to an answer already in the
    # set was written blind AND had no rollback entry. Now each variant is a
    # snapshot row in its own right.
    #
    # WHAT THE GUARANTEE ACTUALLY IS, stated carefully because the first version
    # of this test asserted something stronger and wrong. It is NOT "an added
    # variant is never touched": a variant that is committed and visible when a
    # snapshot is taken is legitimately part of the set that snapshot describes,
    # exactly like an added entry, and writing it is correct BECAUSE it is
    # recorded and therefore restorable. The guarantee is per snapshot —
    #
    #   a row absent from snapshot S is not written, and not restored, by S
    #
    # — and it is the ROLLBACK half that the old code violated: its resync ran
    # `FROM qa_entries e WHERE e.entry_id IN (recorded entries)`, and the added
    # variant hangs off a recorded entry, so undoing the migration destroyed a
    # label the migration had never touched.
    fixture("build")
    out = pathlib.Path(tempfile.mkdtemp())
    plan = write_plan([PLAN_UNTAGGED])
    try:
        r = run("--plan", str(plan), "--apply", "--go", "--out", str(out))
        check("the set applies", "OK    untagged-pack" in r.stdout, r.stdout[-300:])
        snaps = sorted(out.glob("domain-migration-*.snapshot.json"))
        rows = json.loads(snaps[0].read_text(encoding="utf-8"))["sets"][
            "untagged-pack"]["rows"]
        snap_variants = {k for t, k, _v in rows if t == "qa_variants"}
        check("the snapshot records the answer's variant rows",
              bool(snap_variants), f"{sorted(snap_variants)}")
        check("…and its embedding rows",
              any(t == "qa_embedding" for t, _k, _v in rows),
              f"{sorted({t for t, _k, _v in rows})}")

        # a NEW variant on an answer the set already covers, carrying a label of
        # its own that nothing in this migration ever chose
        vid = _add_variant("ds-untag-1", "pridana varianta")
        check("the new variant is NOT in that snapshot", vid not in snap_variants,
              f"{vid} in {sorted(snap_variants)}")

        # THE ASSERTION THIS TEST EXISTS FOR: rolling back THAT snapshot must
        # not reach a row the snapshot does not name.
        r = run("--rollback", str(snaps[0]), "--go")
        check("the rollback runs", r.returncode == 0, (r.stdout + r.stderr)[-400:])
        after_rb = db("SELECT coalesce(array_to_string(domain_ids, ','), 'NULL') "
                      f"  FROM edu_ai.qa_variants WHERE variant_id = {vid};")
        check("…and the added variant keeps its own label",
              after_rb == "777777",
              f"{after_rb!r} — the restore reached a row outside its snapshot")

        # …and a LATER apply does include it, which is why writing it then is
        # safe: it is recorded, so it can be undone.
        plan2 = write_plan([{k: v for k, v in PLAN_UNTAGGED.items()
                             if k != "expect_current"}])
        try:
            r2 = run("--plan", str(plan2), "--apply", "--go", "--out", str(out))
            check("a later apply writes it — and RECORDS it",
                  "OK    untagged-pack" in r2.stdout, r2.stdout[-300:])
            newest = sorted(out.glob("domain-migration-*.snapshot.json"))[-1]
            rows2 = json.loads(newest.read_text(encoding="utf-8"))["sets"][
                "untagged-pack"]["rows"]
            check("…so the added variant is restorable from THAT snapshot",
                  any(t == "qa_variants" and k == vid for t, k, _v in rows2),
                  f"{sorted({k for t, k, _ in rows2 if t == 'qa_variants'})}")
            r3 = run("--rollback", str(newest), "--go")
            check("…and restoring it brings the label back",
                  r3.returncode == 0
                  and db("SELECT coalesce(array_to_string(domain_ids, ','), 'NULL') "
                         f"  FROM edu_ai.qa_variants WHERE variant_id = {vid};")
                  == "777777", (r3.stdout + r3.stderr)[-300:])
        finally:
            os.unlink(plan2)
    finally:
        db("DELETE FROM edu_ai.qa_variants WHERE norm_text = 'pridana varianta';")
        os.unlink(plan)
        fixture("drop")


# ══════════════════════════════════════════════════════════════════════════
# 5. A CONCURRENT WRITE DURING THE ROLLBACK ITSELF
# ══════════════════════════════════════════════════════════════════════════
def test_rollback_blocks_a_concurrent_session():
    print("\n[rollback: a curator commits DURING the restore transaction]")
    # The rollback's drift check ran in separate statements BEFORE the restore,
    # so a curator saving in between had their change overwritten while the tool
    # had already printed "no drift" — the apply's bug, in the other direction.
    # Same handshake, same conclusion: lock and verify inside the transaction.
    fixture("build")
    out = pathlib.Path(tempfile.mkdtemp())
    plan = write_plan([PLAN_UNTAGGED])
    gid = domain_id("GRADING")
    holder = rb = None
    try:
        run("--plan", str(plan), "--apply", "--go", "--out", str(out))
        snaps = sorted(out.glob("domain-migration-*.snapshot.json"))
        if not snaps:
            check("a snapshot was written", False)
            return

        # ── a curator changes the pack, UNCOMMITTED ────────────────────────
        holder = subprocess.Popen(
            ["docker", "exec", "-i", PG, "sh", "-c",
             'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        assert holder.stdin is not None
        holder.stdin.write(
            "BEGIN;\n"
            f"UPDATE edu_ai.qa_pack_manifest SET domain_ids = ARRAY[{gid}]::bigint[]"
            "  WHERE pack_id = 'ds-untagged';\n")
        holder.stdin.flush()
        for _ in range(80):
            if int(db("SELECT count(*) FROM pg_locks l JOIN pg_class c "
                      "    ON c.oid = l.relation WHERE c.relname = "
                      "'qa_pack_manifest' AND l.mode = 'RowExclusiveLock' "
                      "AND l.granted;") or 0) > 0:
                break
        # invisible to anyone else, so the rollback's pre-check will see no drift
        check("the change is not yet visible, so the pre-check finds no drift",
              db("SELECT coalesce(array_to_string(domain_ids, ','), 'NULL') "
                 "  FROM edu_ai.qa_pack_manifest WHERE pack_id = 'ds-untagged';")
              == str(domain_id("CATERING")))

        rb = subprocess.Popen(
            [sys.executable, SCRIPT, "--rollback", str(snaps[0]), "--go"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(ROOT))
        blocked = False
        for _ in range(200):
            if _waiting_locks() > 0:
                blocked = True
                break
            if rb.poll() is not None:
                break
        check("the RESTORE blocks on the concurrent lock", blocked,
              "it never waited — the restore is not locking")

        holder.stdin.write("COMMIT;\n")
        holder.stdin.flush()
        holder.stdin.close()
        rb_out, _ = rb.communicate(timeout=180)

        check("…and then ABORTS rather than overwriting the curator",
              rb.returncode == 2 and "changed since the migration" in rb_out,
              f"rc={rb.returncode} {rb_out[-500:]}")
        now = db("SELECT coalesce(array_to_string(domain_ids, ','), 'NULL') "
                 "  FROM edu_ai.qa_pack_manifest WHERE pack_id = 'ds-untagged';")
        check("the curator's value SURVIVED the rollback attempt",
              now == str(gid), f"{now!r} != {gid}")
    finally:
        for proc in (holder, rb):
            if proc is not None and proc.poll() is None:
                proc.kill()
        os.unlink(plan)
        fixture("drop")


GROUPS = {
    "gate": test_dry_run_writes_nothing,
    "concurrent": test_concurrent_change_before_the_transaction,
    "guard-scope": test_guard_catches_a_row_expect_current_does_not_check,
    "guard-race": test_guard_blocks_a_concurrent_session,
    "snapshot": test_snapshot_survives_a_killed_process,
    "foreign": test_rollback_leaves_a_foreign_set_alone,
    "refuse": test_rollback_refuses_over_a_later_change,
    "added-row": test_row_added_after_the_snapshot_is_not_written,
    "added-variant": test_variant_added_to_an_existing_answer,
    "rollback-race": test_rollback_blocks_a_concurrent_session,
}


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    print("=" * 74)
    print("domain_migration.py — concurrency, crash safety, blast radius")
    print("=" * 74)
    for name, fn in GROUPS.items():
        if only and name != only:
            continue
        try:
            fn()
        except Exception as e:                                   # noqa: BLE001
            _failures.append(f"{name} (raised)")
            print(f"  FAIL group {name} raised: {type(e).__name__}: {e}")
            try:
                fixture("drop")
            except Exception:                                    # noqa: BLE001
                pass
    print("\n" + "=" * 74)
    print(f"{_ran} checks, {len(_failures)} failed")
    for f in _failures:
        print(f"  - {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
