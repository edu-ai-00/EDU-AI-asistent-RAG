"""Runner commands: status, plan, init, apply, gen-baseline, gen-release."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import fingerprint, manifest, pg, registry, sqltok
from .manifest import Layout, ManifestError, MigFile

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_BUSY, EXIT_FAILED, EXIT_MISMATCH, EXIT_UNVERIFIED = 0, 1, 2, 3, 4, 5, 6
GUARD = "migration_tx guard"

NO_REGISTRY = ("database '{db}' has no migration registry (edu_ai.schema_migration). Nothing was "
               "changed or converted. A clean B216 installation is registered with `init`; an "
               "existing installation needs the separate takeover path (D11), which does not exist yet.")
DATA_CAVEAT = ("note: the schema fingerprint covers the schema only. It does not detect changes to "
               "rows in settings, prompt_registry or other configuration tables.")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# -- database facts ------------------------------------------------------------
def query_json(target: pg.Target, sql: str):
    return json.loads(pg.run_psql(target, sql, readonly=True))


def facts(target: pg.Target) -> dict:
    f = query_json(target, """SELECT json_build_object(
        'server_version', split_part(current_setting('server_version'), ' ', 1),
        'extensions', (SELECT json_object_agg(extname, extversion) FROM pg_extension),
        'schema', to_regnamespace('edu_ai') IS NOT NULL,
        'registry', to_regclass('edu_ai.schema_migration') IS NOT NULL);""")
    f["content_rows"] = None
    if f["schema"]:
        f["content_rows"] = int(pg.run_psql(target, "SELECT " + " + ".join(
            f"(SELECT count(*) FROM edu_ai.{t})" for t in fingerprint.CONTENT_TABLES) + ";", readonly=True))
    return f


def registry_lines(target: pg.Target) -> list[str]:
    return [l for l in pg.run_psql(target, registry.DESCRIBE_SQL, readonly=True).splitlines() if l]


def registry_rows(target: pg.Target) -> list[dict]:
    return query_json(target, """SELECT coalesce(json_agg(json_build_object(
        'id', migration_id, 'kind', kind, 'checksum', checksum, 'release', release,
        'applied_at', applied_at, 'applied_by', applied_by, 'notes', notes)
        ORDER BY applied_at, migration_id), '[]'::json) FROM edu_ai.schema_migration;""")


def try_lock(sess: pg.Session) -> bool:
    return sess.run(f"SELECT pg_try_advisory_lock({registry.LOCK_KEY});").strip() == "t"


def environment_problems(base: dict, f: dict) -> list[str]:
    pgs = base["postgres"]
    out = []
    if f["server_version"] != pgs["server_version"]:
        out.append(f"server version {f['server_version']} differs from the baseline image "
                   f"({pgs['server_version']}); the fingerprints are bound to that image")
    for name in ("vector", "pg_search"):
        if f["extensions"].get(name) != pgs["extensions"].get(name):
            out.append(f"extension {name} is {f['extensions'].get(name)}, baseline has {pgs['extensions'].get(name)}")
    return out


# -- analysis shared by status, plan and apply ---------------------------------
class NoRegistry(Exception):
    pass


def lint_problems(m: MigFile) -> list[str]:
    # Switching lint off exists only so a test can prove the migration_tx guard.
    if os.environ.get("EDU_AI_MIGRATE_TESTING") == "1" and os.environ.get("EDU_AI_MIGRATE_SKIP_LINT") == "1":
        return []
    return ["lint: " + f.format(m.path.name) for f in sqltok.lint_migration(m.text)]


@dataclass
class Analysis:
    base: dict
    rows: list[dict]
    release: dict | None = None
    pending: list[tuple[MigFile, dict]] = field(default_factory=list)
    expected_fp: str | None = None
    live_fp: str | None = None
    problems: list[str] = field(default_factory=list)


def analyze(layout: Layout, target: pg.Target, release_name: str | None, migrations_dir: Path | None,
            measure: bool = True) -> Analysis:
    base = manifest.load_baseline(layout)
    f = facts(target)
    if not f["schema"] or not f["registry"]:
        raise NoRegistry(NO_REGISTRY.format(db=target.db))
    a = Analysis(base, registry_rows(target))
    a.problems += [f"registry definition: {d}" for d in registry.drift(registry_lines(target))]
    a.problems += environment_problems(base, f)

    anchors = [r for r in a.rows if r["kind"] in ("baseline", "adopted")]
    if len(anchors) != 1:
        a.problems.append(f"registry holds {len(anchors)} baseline/adopted rows, expected exactly 1")
    elif anchors[0]["id"] != base["id"]:
        a.problems.append(f"registry baseline is {anchors[0]['id']}, this runner knows {base['id']}")
    elif anchors[0]["kind"] == "baseline" and anchors[0]["checksum"] != base["fingerprint"]["schema"]:
        a.problems.append("registry baseline checksum differs from the baseline manifest")
    applied = [r for r in a.rows if r["kind"] == "migration"]

    try:
        a.release = manifest.load_release(layout, release_name)
    except ManifestError as e:
        a.problems.append(str(e))
    entries = a.release["migrations"] if a.release else []
    if a.release:
        a.problems += manifest.validate_release(a.release, base)
    if applied and not a.release:
        a.problems.append("migrations are applied but no release manifest is available to verify them")

    history_ok = bool(a.release) or not applied
    if a.release and [r["id"] for r in applied] != [e["id"] for e in entries[:len(applied)]]:
        a.problems.append("registry history diverges from the release manifest (ids or order)")
        history_ok = False

    try:
        files = manifest.list_migrations(migrations_dir or layout.migrations_dir)
    except ManifestError as e:
        a.problems.append(str(e))
        files = []
    by_id = {m.id: m for m in files}

    if history_ok and a.release:
        for r, e in zip(applied, entries):
            if r["checksum"] != e["sha256"]:
                a.problems.append(f"{r['id']}: registry checksum differs from the release manifest")
            m = by_id.get(r["id"])
            if m is None:
                a.problems.append(f"{r['id']}: applied file is missing on disk")
            elif m.sha256 != r["checksum"]:
                a.problems.append(f"{r['id']}: file changed after it was applied (checksum drift)")
        for e in entries[len(applied):]:
            m = by_id.get(e["id"])
            if m is None:
                a.problems.append(f"{e['id']}: pending file is missing on disk")
                continue
            if m.sha256 != e["sha256"]:
                a.problems.append(f"{e['id']}: file differs from the release manifest")
                continue
            a.problems += lint_problems(m)
            a.pending.append((m, e))
        listed = {e["id"] for e in entries}
        a.problems += [f"{i}.sql is not in the release manifest" for i in by_id if i not in listed]
        last = entries[len(applied) - 1] if applied else None
        a.expected_fp = last["schema_after"] if last else base["fingerprint"]["schema"]
    elif not applied:
        a.expected_fp = base["fingerprint"]["schema"]

    if measure and a.expected_fp:
        a.live_fp = fingerprint.measure_schema(target)
        if a.live_fp != a.expected_fp:
            a.problems.append("schema drift: the live schema fingerprint differs from the expected one "
                              f"({'after ' + (applied[-1]['id'] if applied else base['id'])})")
    return a


def print_analysis(a: Analysis, target: pg.Target) -> None:
    say(f"database: {target.db}   baseline: {a.base['id']}")
    for r in a.rows:
        say(f"  {r['kind']:<9} {r['id']:<32} {str(r['applied_at'])[:19]}  {r['notes'] or ''}")
    if a.release:
        say(f"release manifest: {a.release['release']} ({len(a.release['migrations'])} migrations)")
    say(f"pending: {len(a.pending)}" + "".join(f"\n  {m.id}  sha256={m.sha256[:12]}" for m, _ in a.pending))
    say("schema fingerprint: " + ("not measured" if a.live_fp is None else
        "matches" if a.live_fp == a.expected_fp else "DIFFERS"))
    say("registry definition: " + ("v1 ok" if not any(p.startswith("registry definition") for p in a.problems)
                                   else "DRIFT"))
    if a.problems:
        say("PROBLEMS:")
        for p in a.problems:
            say(f"  - {p}")
    say(DATA_CAVEAT)


# -- status / plan -------------------------------------------------------------
def cmd_status(args, layout: Layout, target: pg.Target) -> int:
    a = analyze(layout, target, args.release, Path(args.migrations_dir) if args.migrations_dir else None)
    print_analysis(a, target)
    return EXIT_REFUSED if a.problems else EXIT_OK


def cmd_plan(args, layout: Layout, target: pg.Target) -> int:
    a = analyze(layout, target, args.release, Path(args.migrations_dir) if args.migrations_dir else None)
    print_analysis(a, target)
    if a.problems:
        say("apply would refuse.")
        return EXIT_REFUSED
    say("apply would run the pending migrations in this order, each in its own transaction." if a.pending
        else "nothing to apply.")
    return EXIT_OK


# -- fault injection (tests only) ----------------------------------------------
def _fault_mode(point: str) -> str | None:
    """Mode set for `point` by EDU_AI_MIGRATE_FAULT ('point=mode,point=mode'); tests only."""
    spec = os.environ.get("EDU_AI_MIGRATE_FAULT", "")
    if os.environ.get("EDU_AI_MIGRATE_TESTING") != "1" or not spec:
        return None
    for item in spec.split(","):
        name, _, mode = item.partition("=")
        if name == point:
            return mode
    return None


def _hold(point: str) -> None:
    """Test seam: stop at `point` until the test says go (mode 'hold:<dir>': writes <dir>/reached,
    waits for <dir>/release). Lets a test run something else at an exact moment."""
    mode = _fault_mode(point)
    if not mode or not mode.startswith("hold:"):
        return
    folder = Path(mode[5:])
    (folder / "reached").write_text("1")
    deadline = time.monotonic() + 180
    while not (folder / "release").exists() and time.monotonic() < deadline:
        time.sleep(0.2)


def _fault(point: str, sess: pg.Session) -> None:
    mode = _fault_mode(point)
    if mode is None:
        return
    if mode == "terminate":
        sess.run("SELECT pg_terminate_backend(pg_backend_pid());")
    if mode == "drift":  # a schema change that slips in after COMMIT
        sess.run("ALTER TABLE edu_ai.settings ADD COLUMN fault_injected int;")
        return
    sess.run("DO $$ BEGIN RAISE EXCEPTION 'injected fault'; END $$;")


# -- COMMIT outcome ------------------------------------------------------------
def resolve_outcome(target: pg.Target, present, wait: float = 30.0) -> str:
    """After the connection died around COMMIT: 'committed', 'not_committed' or 'unknown'.

    Waits until the dead session's backend (and its advisory lock) is gone, then
    asks the registry. If that cannot be established the answer is 'unknown'.
    """
    hi, lo = registry.LOCK_KEY >> 32, registry.LOCK_KEY & 0xFFFFFFFF
    held = ("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted AND objsubid = 1 "
            f"AND classid = {hi} AND objid = {lo} "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database());")
    end = time.monotonic() + wait
    try:
        while int(pg.run_psql(target, held, readonly=True)) > 0:
            if time.monotonic() > end:
                return "unknown"
            time.sleep(0.5)
        return "committed" if present() else "not_committed"
    except (pg.PgError, ValueError):
        return "unknown"


def _commit(sess: pg.Session, target: pg.Target, present, point: str) -> tuple[str, bool]:
    """Send COMMIT. Returns (outcome, session_alive); the outcome is never guessed."""
    mode = _fault_mode(point)
    try:
        if mode == "lose_before":
            sess.kill()
            raise pg.SessionDied("injected: connection lost before COMMIT was sent")
        sess.run("COMMIT;")
        if mode == "lose_ack":
            sess.kill()
            raise pg.SessionDied("injected: connection lost before the COMMIT acknowledgement")
        return "committed", True
    except pg.SessionDied:
        return resolve_outcome(target, present), False


# -- init ----------------------------------------------------------------------
def _registry_present(target: pg.Target) -> bool:
    return facts(target)["registry"]


def cmd_init(args, layout: Layout, target: pg.Target) -> int:
    base = manifest.load_baseline(layout)
    problems = manifest.verify_init_clean(layout, base)
    f = facts(target)
    if not f["schema"]:
        problems.append("schema edu_ai does not exist; install postgres/init_clean first")
    else:
        problems += environment_problems(base, f)
        if f["registry"]:
            problems.append("registry already exists; use `status` (init is for a clean install only)")
        if f["content_rows"]:
            problems.append(f"content tables hold {f['content_rows']} rows; init is for a clean install only")
    if problems:
        return _refuse("init refused", problems)

    sess = pg.Session(target)
    try:
        if not try_lock(sess):
            say("another update is running (advisory lock is held); nothing was changed.")
            return EXIT_BUSY
        sfp, cfp = fingerprint.measure_schema(target), fingerprint.measure_config(target)
        want = base["fingerprint"]
        problems = []
        if sfp != want["schema"]:
            problems.append("schema fingerprint differs from baseline B216 (this is not a clean B216 install)")
        if cfp != want["config"]:
            problems.append("configuration fingerprint differs from baseline B216")
        if problems:
            return _refuse("init refused", problems)
        return _init_transaction(sess, target, base, sfp, cfp)
    except pg.PgError as e:  # COMMIT and what follows it are handled inside _init_transaction
        say(f"init failed before COMMIT: {(getattr(e, 'output', '') or str(e)).strip()}")
        try:
            present = _registry_present(target)
        except pg.PgError:
            say("the registry could not be read afterwards; run `status` before anything else.")
            return EXIT_FAILED
        say("registry table present afterwards: YES (unexpected; run `status`)." if present else
            "registry table present afterwards: no. The transaction was rolled back, nothing was created.")
        return EXIT_FAILED
    finally:
        sess.close()


def _init_transaction(sess: pg.Session, target: pg.Target, base: dict, sfp: str, cfp: str) -> int:
    sess.run("BEGIN;\nSET LOCAL lock_timeout = '5s';")
    sess.run(registry.DDL)
    _fault("init:after_ddl", sess)
    drift = registry.drift([l for l in sess.run(registry.DESCRIBE_SQL).splitlines() if l])
    if drift:
        sess.run("ROLLBACK;")
        return _refuse("init refused (registry definition)", drift)
    sess.run("INSERT INTO edu_ai.schema_migration (migration_id, kind, checksum, applied_by, notes) VALUES "
             f"({lit(base['id'])}, 'baseline', {lit(sfp)}, {lit(manifest.TOOL)}, "
             f"{lit('baseline installed from init_clean; configuration fingerprint ' + cfp)});")
    _fault("init:after_insert", sess)
    # Second connection: cannot see the uncommitted registry (which is excluded anyway), but
    # proves the schema did not move while the transaction was open and that it is not blocked.
    if fingerprint.measure_schema(target) != base["fingerprint"]["schema"]:
        sess.run("ROLLBACK;")
        return _refuse("init refused", ["schema fingerprint changed while init was running"])
    outcome, alive = _commit(sess, target, lambda: _registry_present(target), "init:commit")
    if outcome == "not_committed":
        say("init failed: the connection was lost while COMMIT was in flight. The registry does not exist and "
            "no runner session is left, so the transaction did not commit; nothing was created.")
        return EXIT_FAILED
    if outcome == "unknown":
        say("init: the connection was lost while COMMIT was in flight and the outcome is UNKNOWN. "
            "Do not assume a rollback; run `status` to see whether the registry exists.")
        return EXIT_UNVERIFIED

    try:
        if alive:
            _fault("init:after_commit", sess)
        _hold("init:before_verify")
        after = fingerprint.measure_schema(target)
        drift = registry.drift(registry_lines(target))
        rows = registry_rows(target)
    except pg.PgError as e:
        say(f"init: the registry is COMMITTED, but the verification afterwards could not be completed "
            f"({(getattr(e, 'output', '') or str(e)).strip()}). Nothing was dropped; run `status`.")
        return EXIT_UNVERIFIED
    found = []
    if after != base["fingerprint"]["schema"]:
        found.append("schema fingerprint differs from baseline B216")
    found += [f"registry definition: {d}" for d in drift]
    if [r["id"] for r in rows] != [base["id"]]:
        found.append(f"registry rows are {[r['id'] for r in rows]}, expected only {[base['id']]}")
    if found:  # the registry stays: it is committed, and the changes may be someone else's
        say("init: the baseline registry is COMMITTED, but the final verification did not succeed. "
            "Nothing was dropped and no rollback was attempted; run `status` to judge the state "
            "(this is expected if another runner applied a migration in between).")
        for p in found:
            say(f"  - {p}")
        return EXIT_UNVERIFIED
    say(f"init ok: registry v{registry.VERSION} created, baseline {base['id']} registered.")
    if not alive:
        say("The connection was lost during COMMIT; the registry confirms that the commit happened.")
    say(f"schema fingerprint {after[:16]}..., registry definition v{registry.VERSION} ok.")
    return EXIT_OK


def _refuse(title: str, problems: list[str]) -> int:
    say(f"{title}:")
    for p in problems:
        say(f"  - {p}")
    return EXIT_REFUSED


# -- apply ---------------------------------------------------------------------
class MigrationFailed(Exception):
    """The migration did not commit: it failed before COMMIT, or COMMIT is proven not to have happened."""

    def __init__(self, mig: str, output: str, guard: bool, lost_commit: bool = False):
        super().__init__(mig)
        self.mig, self.output, self.guard, self.lost_commit = mig, output, guard, lost_commit


class AfterCommit(Exception):
    """COMMIT happened ('committed') or may have happened ('unknown'); what followed could not finish."""

    def __init__(self, mig: str, detail: str, state: str):
        super().__init__(mig)
        self.mig, self.detail, self.state = mig, detail, state


class SchemaMismatch(Exception):
    def __init__(self, mig: str, expected: str, actual: str):
        super().__init__(mig)
        self.mig, self.expected, self.actual = mig, expected, actual


def apply_one(sess: pg.Session, target: pg.Target, mig: MigFile, release: str, expected: str | None) -> str:
    """One migration and its registry row in one transaction; returns the schema fingerprint after COMMIT."""
    mid = lit(mig.id)
    try:
        sess.run(f"BEGIN;\nSET LOCAL lock_timeout = '5s';\nSELECT set_config('edu_ai.migration_tx', {mid}, true);")
        sess.run(mig.text)
        sess.run(f"""DO $edu_ai_guard$ BEGIN
  IF current_setting('edu_ai.migration_tx', true) IS DISTINCT FROM {mid} THEN
    RAISE EXCEPTION '{GUARD}: the file ended the transaction early; no registry row written for %', {mid};
  END IF;
END $edu_ai_guard$;""")
        sess.run("INSERT INTO edu_ai.schema_migration (migration_id, kind, checksum, release, applied_by) VALUES "
                 f"({mid}, 'migration', {lit(mig.sha256)}, {lit(release)}, {lit(manifest.TOOL)});")
    except pg.SessionDied as e:  # before COMMIT was sent
        raise MigrationFailed(mig.id, e.output, GUARD in e.output) from e

    def row_present() -> bool:
        return any(r["id"] == mig.id for r in registry_rows(target))

    outcome, alive = _commit(sess, target, row_present, "apply:commit")
    if outcome == "not_committed":
        raise MigrationFailed(mig.id, "connection lost while COMMIT was in flight", False, lost_commit=True)
    if outcome == "unknown":
        raise AfterCommit(mig.id, "the connection was lost while COMMIT was in flight and the registry could "
                                  "not be read", "unknown")
    try:  # from here on the migration is committed
        if alive:
            _fault("apply:after_commit", sess)
        actual = fingerprint.measure_schema(target)
    except pg.PgError as e:
        raise AfterCommit(mig.id, f"the check after COMMIT could not be completed "
                                  f"({(getattr(e, 'output', '') or str(e)).strip()})", "committed") from e
    if expected is not None and actual != expected:
        raise SchemaMismatch(mig.id, expected, actual)
    if not alive:
        raise AfterCommit(mig.id, "the connection was lost during COMMIT, the registry confirms the commit and "
                                  "schema_after matches; the lock is released", "committed")
    note = "schema_after verified: " + actual if expected is not None else "schema_after recorded on the reference"
    try:
        sess.run(f"UPDATE edu_ai.schema_migration SET notes = {lit(note)} WHERE migration_id = {mid};")
    except pg.PgError as e:
        raise AfterCommit(mig.id, f"the verification note could not be written ({str(e).strip()})",
                          "committed") from e
    return actual


def explain_failure(target: pg.Target, e: MigrationFailed, dump: Path | None) -> None:
    where = "while COMMIT was in flight" if e.lost_commit else "before COMMIT"
    say(f"migration {e.mig} FAILED {where}: {e.output.strip()}")
    try:
        present = any(r["id"] == e.mig for r in registry_rows(target))
    except pg.PgError:
        present = None
    say(f"registry row for {e.mig}: " + {True: "PRESENT", False: "absent", None: "unknown"}[present])
    if e.guard:
        say("The file ended the transaction itself (COMMIT/ROLLBACK/END). The guard detected it only "
            "afterwards and CANNOT undo what the file had already committed; the registry row was not written.")
    elif present is False and e.lost_commit:
        say("No runner session is left and the registry shows no row: the COMMIT did not happen and nothing of "
            "this migration remains. Following migrations were not started.")
    elif present is False:
        say("The transaction was rolled back (it never committed, and the registry confirms it): neither the "
            "file's changes nor the registry row remain. Following migrations were not started.")
    elif present is True:
        say("The registry unexpectedly shows a row for this migration. The state is unclear: run `status`.")
    else:
        say("The registry could not be read, so no rollback is claimed: run `status` before anything else.")
    if dump:
        say(f"If the database is in doubt, restore it from the local dump: {dump}")


def explain_after_commit(e: AfterCommit, dump: Path | None) -> None:
    if e.state == "committed":
        say(f"migration {e.mig} is COMMITTED. {e.detail[0].upper() + e.detail[1:]}. This is not a rollback; "
            "later migrations were not started. Run `status`, then `apply` again to continue.")
    else:
        say(f"migration {e.mig}: the outcome is UNKNOWN. {e.detail[0].upper() + e.detail[1:]}. Do not assume a "
            "rollback; run `status` to see whether the registry holds the row.")
    if dump:
        say(f"Local dump taken before the run: {dump}")


def take_dump(target: pg.Target, dump_dir: Path, first_id: str) -> Path:
    """Local custom-format dump, accepted only when pg_dump succeeded and pg_restore can read it."""
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = dump_dir / f"{target.db}_{stamp}_before_{first_id}.dump"
    try:
        pg.pg_dump_file(target, str(path))
        toc = pg.pg_restore_list(target, str(path))
        if not path.stat().st_size or not any("schema_migration" in line for line in toc):
            raise pg.PgError("dump archive is readable but does not contain the registry table")
    except pg.PgError:
        path.unlink(missing_ok=True)
        raise
    return path


def cmd_apply(args, layout: Layout, target: pg.Target) -> int:
    mdir = Path(args.migrations_dir) if args.migrations_dir else None
    sess = pg.Session(target)
    dump: Path | None = None
    try:
        if not try_lock(sess):
            say("another update is running (advisory lock is held); nothing was changed.")
            return EXIT_BUSY
        a = analyze(layout, target, args.release, mdir)
        if not a.release:
            a.problems.append("no release manifest found; give --release NAME")
        if a.problems:
            return _refuse("apply refused; nothing was changed", a.problems)
        if not a.pending:
            say("nothing to apply: every migration of the release is already registered.")
            return EXIT_OK
        try:
            dump = take_dump(target, Path(args.dump_dir), a.pending[0][0].id)
        except pg.PgError as e:
            return _refuse("apply refused: the local pre-apply dump is not usable; nothing was changed", [str(e)])
        say(f"local dump written and verified (pg_dump rc=0, archive readable): {dump}")
        for mig, entry in a.pending:
            say(f"applying {mig.id} ...")
            apply_one(sess, target, mig, a.release["release"], entry["schema_after"])
            say(f"  {mig.id}: committed, schema_after matches the release manifest")
        say(f"apply ok: {len(a.pending)} migration(s) applied.")
        say(DATA_CAVEAT)
        return EXIT_OK
    except MigrationFailed as e:
        explain_failure(target, e, dump)
        return EXIT_FAILED
    except AfterCommit as e:
        explain_after_commit(e, dump)
        return EXIT_UNVERIFIED
    except SchemaMismatch as e:
        say(f"migration {e.mig} is COMMITTED, but the schema does not match the release manifest.")
        say(f"  expected {e.expected[:16]}...  actual {e.actual[:16]}...")
        say("Further migrations were not started. The committed change was NOT rolled back automatically; "
            "the way back is restoring the local dump" + (f": {dump}" if dump else "."))
        return EXIT_MISMATCH
    except pg.SessionDied as e:  # only reachable before the first migration (lock, analysis, dump)
        say(f"the psql session ended unexpectedly before any migration started: {e.output.strip()}")
        return EXIT_FAILED
    finally:
        sess.close()


# -- generators (reference databases only) -------------------------------------
def cmd_gen_baseline(args, layout: Layout, target: pg.Target) -> int:
    out = Path(args.out) if args.out else layout.baseline_manifest
    problems = []
    try:
        base_image = manifest.dockerfile_base_image(layout)
        if manifest.git(layout, "status", "--porcelain", "--", "postgres/init_clean").strip():
            problems.append("postgres/init_clean has uncommitted changes; commit them first")
        hashes = manifest.init_clean_hashes(layout)
        for rel, sha in hashes.items():
            blob = manifest.git_blob(layout, "HEAD", rel)
            if fingerprint.file_sha256(blob) != sha:
                problems.append(f"{rel}: differs from HEAD")
        commit = manifest.git(layout, "rev-parse", "HEAD").strip()
    except ManifestError as e:
        return _refuse("gen-baseline refused", [str(e)])
    f = facts(target)
    if not f["schema"]:
        problems.append("schema edu_ai does not exist on the reference database")
    elif f["content_rows"]:
        problems.append(f"content tables hold {f['content_rows']} rows; the reference must be synthetic")
    if problems:
        return _refuse("gen-baseline refused", problems)

    doc = {
        "format": 1, "id": manifest.BASELINE_ID, "kind": "baseline",
        "description": "Logical baseline B216: a clean install of postgres/init_clean 00-03.",
        "source": {"base_commit": commit,
                   "note": "Commit the inputs were generated from, i.e. the parent of the commit that adds "
                           "this manifest (a manifest cannot name its own commit). Informational: identity "
                           "is carried by the file hashes below."},
        "files": [{"path": p, "sha256": s} for p, s in hashes.items()],
        "fingerprint": {
            "algorithm": fingerprint.ALGORITHM,
            "schema": fingerprint.measure_schema(target),
            "config": fingerprint.measure_config(target),
            "schema_dump_args": fingerprint.SCHEMA_DUMP_ARGS,
            "config_dump_args": fingerprint.CONFIG_DUMP_ARGS,
            "config_masked_columns": list(fingerprint.MASK_COLUMNS),
        },
        "postgres": {"base_image": base_image, "server_version": f["server_version"],
                     "pg_dump_version": pg.tool_version(target, "pg_dump"), "extensions": f["extensions"]},
        "generated_by": manifest.TOOL,
    }
    manifest.write_json(out, doc)
    say(f"wrote {out} (schema {doc['fingerprint']['schema'][:16]}..., config {doc['fingerprint']['config'][:16]}...)")
    return EXIT_OK


def cmd_gen_release(args, layout: Layout, target: pg.Target) -> int:
    base = manifest.load_baseline(layout)
    out = Path(args.out) if args.out else layout.releases_dir / f"{args.release}.json"
    if out.exists() and not args.force:
        return _refuse("gen-release refused", [f"{out} exists; pass --force to regenerate it"])
    try:
        files = manifest.list_migrations(Path(args.migrations_dir) if args.migrations_dir else layout.migrations_dir)
    except ManifestError as e:
        return _refuse("gen-release refused", [str(e)])
    problems = [p for m in files for p in lint_problems(m)]
    f = facts(target)
    if not f["registry"]:
        problems.append(NO_REGISTRY.format(db=target.db))
    elif f["content_rows"]:
        problems.append("the reference database holds content rows; use a synthetic one")
    if problems:
        return _refuse("gen-release refused", problems)
    rows = registry_rows(target)
    if [r["id"] for r in rows] != [base["id"]]:
        return _refuse("gen-release refused", ["the reference must be a freshly initialised B216 "
                                                f"(registry rows: {[r['id'] for r in rows]})"])
    sess = pg.Session(target)
    try:
        if not try_lock(sess):
            say("another update is running (advisory lock is held); nothing was changed.")
            return EXIT_BUSY
        problems = []
        if fingerprint.measure_schema(target) != base["fingerprint"]["schema"]:
            problems.append("schema fingerprint differs from B216")
        if fingerprint.measure_config(target) != base["fingerprint"]["config"]:
            problems.append("configuration fingerprint differs from B216")
        if problems:
            return _refuse("gen-release refused", problems)
        entries = []
        for m in files:
            say(f"reference: applying {m.id} ...")
            entries.append({"id": m.id, "sha256": m.sha256,
                            "schema_after": apply_one(sess, target, m, args.release, None)})
    except MigrationFailed as e:
        explain_failure(target, e, None)
        say("The reference database is now in an undefined state; discard it.")
        return EXIT_FAILED
    except AfterCommit as e:
        explain_after_commit(e, None)
        say("The reference database is now in an undefined state; discard it.")
        return EXIT_UNVERIFIED
    finally:
        sess.close()
    manifest.write_json(out, {
        "format": 1, "release": args.release, "baseline": base["id"],
        "baseline_schema_fingerprint": base["fingerprint"]["schema"],
        "fingerprint_algorithm": fingerprint.ALGORITHM,
        "postgres": base["postgres"], "migrations": entries, "generated_by": manifest.TOOL})
    say(f"wrote {out} ({len(entries)} migrations)")
    return EXIT_OK


# -- command line --------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--container", help="docker container running PostgreSQL (psql/pg_dump via docker exec); "
                                            "without it the local binaries and PG* variables are used")
    common.add_argument("--db")
    common.add_argument("--user")
    common.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    common.add_argument("--release", help="release manifest name (postgres/releases/<name>.json)")
    common.add_argument("--migrations-dir")
    common.add_argument("--confirm-isolated", action="store_true",
                        help="required for commands that write: B03e-1 is approved for isolated synthetic "
                             "databases only")

    ap = argparse.ArgumentParser(prog="migrate.py", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "plan", "init", "apply", "gen-baseline", "gen-release"):
        p = sub.add_parser(name, parents=[common])
        if name == "apply":
            p.add_argument("--dump-dir", required=True, help="where the pre-apply pg_dump goes")
        if name in ("gen-baseline", "gen-release"):
            p.add_argument("--out")
        if name == "gen-release":
            p.add_argument("--force", action="store_true")
    return ap


COMMANDS = {"status": cmd_status, "plan": cmd_plan, "init": cmd_init, "apply": cmd_apply,
            "gen-baseline": cmd_gen_baseline, "gen-release": cmd_gen_release}
WRITES = {"init", "apply", "gen-baseline", "gen-release"}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd in WRITES and not args.confirm_isolated:
        say(f"`{args.cmd}` writes to the database. B03e-1 is approved for isolated synthetic databases only; "
            "pass --confirm-isolated to confirm this is one.")
        return EXIT_USAGE
    if args.cmd == "gen-release" and not args.release:
        say("gen-release needs --release NAME")
        return EXIT_USAGE
    if args.release and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.release):
        say("--release may contain only letters, digits, '.', '_' and '-'")
        return EXIT_USAGE
    if args.container:
        user = args.user or pg.container_env(args.container, "POSTGRES_USER")
        db = args.db or pg.container_env(args.container, "POSTGRES_DB")
    else:
        user, db = args.user or os.environ.get("PGUSER"), args.db or os.environ.get("PGDATABASE")
    if not user or not db:
        say("cannot tell the database user/name: give --user and --db (or a reachable --container)")
        return EXIT_USAGE
    layout = Layout(Path(args.repo_root).resolve())
    target = pg.Target(db=db, user=user, container=args.container)
    try:
        return COMMANDS[args.cmd](args, layout, target)
    except NoRegistry as e:
        say(str(e))
        return EXIT_REFUSED
    except ManifestError as e:
        say(f"refused: {e}")
        return EXIT_REFUSED
    except pg.PgError as e:
        say(f"database error: {e}")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
