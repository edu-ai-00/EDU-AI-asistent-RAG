---
id: spec.migration-registry
status: active
components: [db, tooling]
sources:
  - scripts/migrate.py
  - scripts/migrate.sh
  - scripts/eduai_migrate/sqltok.py
  - scripts/eduai_migrate/fingerprint.py
  - scripts/eduai_migrate/pg.py
  - scripts/eduai_migrate/registry.py
  - scripts/eduai_migrate/manifest.py
  - scripts/eduai_migrate/commands.py
  - postgres/baseline/B216.json
  - postgres/migrations/README.md
  - postgres/init_clean/01_schema.sql
  - Dockerfile.postgres
  - tests/migrate_test_env.py
  - tests/test_migrate_lint.py
  - tests/test_migrate_fingerprint.py
  - tests/test_migrate_runner.py
last_verified: 2026-10-01
---

# Migration registry and runner (B03e-1)

Scope: **isolated, synthetic databases only.** The owner approved D07–D10 for
this scope on 2026-09-30. Production, school installations and the takeover of
old installations (D11) are **not** covered, and no school upgrade is supported
by this spec. Backups (B02) are deferred; the local dump below is a precondition
of a run, not a backup policy. This page is self-contained; it does not depend
on planning notes outside the tracked tree.

## Model

A new installation is one logical baseline, **B216** (`postgres/init_clean/00–04`).
Migrations before 217 are never run on it and never listed in the registry.
Migrations from 217 on live in `postgres/migrations/` and are recorded one by
one in `edu_ai.schema_migration`. The table is **not** part of B216: the runner
creates it (`init`), so the baseline stays unchanged.

## Fingerprints

One shared implementation (`fingerprint.py`) serves the manifest generator and
the runner.

- **Schema fingerprint** — sha256 of the normalized `pg_dump --schema-only
  --schema=edu_ai --no-owner --no-privileges` output, with
  `edu_ai.langchain_pg_*` **and `edu_ai.schema_migration`** excluded. The
  registry exclusion is fixed and identical everywhere. Normalization drops only
  pg_dump's own banner comments, version lines and `\restrict` lines **that start
  outside** a string, quoted identifier or dollar-quoted body. A line inside a body
  that merely looks like a banner (`-- Name: …`) is object content and is kept, so
  two functions that differ only there get different fingerprints.
- **Configuration fingerprint** — sha256 of the sorted, normalized
  `--data-only --column-inserts` output of the seed tables, with `created_at` /
  `updated_at` values masked. It holds only right after installation, before
  school data exists.
- **Registry definition** is verified separately from the catalog (columns,
  types, constraints, indexes, triggers, table comment) against the definition
  of registry v1 and reports `ok` or the drift lines.

A fingerprint proves schema identity. It does **not** detect changes to rows in
`settings`, `prompt_registry` or other configuration tables, and migration
checksums prove file identity, not the current effect of a file on data. A
migration that changes data must carry its own targeted self-check
(`RAISE EXCEPTION`) and test.

## Manifests (generated, never hand-edited)

- `postgres/baseline/B216.json`: sha256 of `00–04` (line endings normalized to
  LF before hashing, as git stores them), schema and configuration fingerprints,
  the fingerprint algorithm and its pg_dump arguments, server and pg_dump
  version, extension versions and the **digest-pinned** base image from
  `Dockerfile.postgres`. It holds no timestamp, so two independent generations
  are byte-identical.
- **Source commit.** The manifest cannot name the commit that adds it. It
  records `base_commit`, the commit the inputs were generated from (the parent of
  the commit that adds the manifest). It is informational. Identity is carried by
  the file hashes, and the generator refuses to run when `00–04` differ from that
  commit.
- `postgres/releases/<release>.json`: cumulative from B216; per migration the id,
  file sha256 and `schema_after`, measured on an isolated reference after each
  committed migration.

## Registry

```
edu_ai.schema_migration(migration_id PK, kind baseline|migration|adopted,
  checksum, release, applied_at, applied_by, notes)
+ unique partial index: at most one baseline/adopted row
```

## Runner commands

Common rules: no default container (always explicit); every write command also
needs `--confirm-isolated`, which exists only until D11 is decided.

- `status`, `plan` — **read-only** (session is `default_transaction_read_only`).
  A database without registry gets a clear error and nothing is converted.
- `init` — only a clean install. Verifies file hashes, extension versions,
  empty content tables and both fingerprints, then creates the registry, checks
  its definition and inserts the single `B216` row in **one transaction**. Any
  failure before commit leaves no table. After a confirmed commit the schema
  fingerprint, the registry definition and the registry rows are verified again.
  A discrepancy there **never removes the registry**: it is committed, and the
  difference may be legitimate (another runner may have applied a migration in
  between). `init` keeps the registry, says that the initialisation is committed
  but the final verification did not succeed, and exits 6; `status` judges the state.
- `apply` — see below.
- `gen-baseline`, `gen-release` — generators; reference databases only.

`adopt --check` is **not implemented** (D11 open).

## Migration files (≥ 217)

`NNN_name.sql`, ascending, unique numbers, listed in the release manifest.

- **A file never controls the transaction.** Lint (stdlib tokenizer over
  top-level statements; comments, strings, `E''`, quoted identifiers,
  dollar-quoting, function/`DO` bodies, `CASE … END` and `BEGIN ATOMIC` are not
  judged) rejects `BEGIN`, `START TRANSACTION`, `COMMIT`, `END`, `ROLLBACK`,
  `ABORT`, `PREPARE TRANSACTION`, `COMMIT/ROLLBACK PREPARED`, `… CONCURRENTLY`,
  `VACUUM`, `CREATE/DROP DATABASE`, `CREATE/DROP TABLESPACE`, `ALTER SYSTEM`,
  psql meta-commands and unterminated quoting, with file name and line.
- **The runner controls it.** One psql session holds a session advisory lock for
  the whole `apply` (a second `apply` exits with "another update is running").
  Each migration and its registry row share one transaction; the first error
  stops all further files.
- **`migration_tx` guard.** After `BEGIN` the runner sets a transaction-local
  setting and checks it before writing the registry row. An early `COMMIT` or
  `ROLLBACK` in the file clears it, the check fails and the row is not written.
  The guard **cannot undo** what the file already committed; the error says so
  and points to the dump.
- **Before changes:** the registry, applied checksums and pending files must
  match the release manifest, and the live schema fingerprint must equal the
  `schema_after` of the last applied migration (B216 when none). Otherwise
  nothing runs.
- **Dump.** `apply` takes a local `pg_dump -Fc`, accepts it only when `pg_dump`
  exits 0 and `pg_restore --list` reads the archive and finds the registry.
- **Three outcomes are never confused** (`init` and `apply`):
  1. *Failure before COMMIT* (SQL error, lost connection): the transaction is
     rolled back. "Rolled back" is printed only when the registry confirms there
     is no row.
  2. *Connection lost while COMMIT is in flight*: the runner waits until the dead
     session's backend and advisory lock are gone, then reads the registry. Row
     present means committed; row absent means not committed; if that cannot be
     established the outcome is reported as **unknown** (exit 6) and no rollback
     is claimed. After a confirmed commit on a lost session the run stops (the
     lock is gone); a re-run continues.
  3. *Error after COMMIT* (post-commit fingerprint, note write): reported as
     committed and unverified (exit 6), never as rolled back.
- **After each COMMIT** the schema fingerprint is measured from a second
  connection and compared with `schema_after`. A mismatch stops further
  migrations, exits non-zero and does **not** claim the committed change was
  rolled back; the way back is restoring the dump.
- A data-only migration may leave `schema_after` unchanged; its own self-check is
  what verifies the result.

## Not covered

Takeover of existing installations, production use, backup policy (B02), service
compatibility during a migration (B06), distribution of the 193 historical files
(B05), down migrations (return = restore the dump and the image).
