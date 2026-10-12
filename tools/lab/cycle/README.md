# tools/lab/cycle — backup, update and restore of one verified lab layout

> **Status: lab driver for a single layout, with no support for a general production upgrade.**
> Do not use on production, on a school installation or on someone else's stack. The B03e migration runner is not used here.
> The public checkout does not contain the private evidence of an actual restore.

Unlike the older [`tools/lab/acc_*.sh`](../README.md), before every access to the target stack it verifies the hostname, the default Docker engine
(root dir), the Compose project and the labels of all containers, the working directory, mounts (only inside `src`/`config`/`state`),
symlinks and absolute canonical paths. A foreign container on the engine, a different directory or a different hostname = refusal.

## Commands (`cycle.py`)

Layout: `--src --cfg --state --project --expect-host --backup-root` or `EDU_AI_SOURCE_ROOT`, `EDU_AI_CONFIG_DIR`,
`EDU_AI_STATE_DIR`, `EDU_AI_COMPOSE_PROJECT`, `CYCLE_EXPECT_HOST`, `CYCLE_BACKUP_ROOT`; further `--evidence-dir` and
`--running-from` (the directory the running containers were created from, default `--src`).

| Command | What it does |
|---|---|
| `gate` | only verifies ownership of the stack |
| `egress ensure|check|probe` | DROP rules for the `edge` and `bifrost_egress` networks in `DOCKER-USER`, verification by probes from inside the containers and a positive control |
| `backup NAME` | saves the images (`docker save`), stops all services except `postgres`, takes the raw baseline, dumps, globals, roles, files, named volumes and verifies everything; starts the services again |
| `verify-backup NAME` | full check of the backup (SHA-256, completeness, archive safety, dump TOC) |
| `update --from-src A --backup NAME --sha SHA` | build and `up --force-recreate` from the target checkout over the same config/state/project |
| `markers put|check ID` | synthetic markers after the backup (files in every mount, a large object in both DBs) |
| `compare-live NAME` | difference of the live state against the backup (counts, not content) |
| `restore NAME --confirm-overwrite-own-stack` | stops and removes the stack's containers, sets the original state aside (`pre-restore-*`), loads the images, restores the files and a clean PostgreSQL cluster, runs `db_roles.py reapply` and `verify`, compares the raw state; **does not start the applications** |
| `verify-restore NAME` | repeats the comparison of the completed restore against the whole backup, only when the applications are not running; on a match it creates a new marker |
| `start-apps NAME` | verifies the marker of the same backup and the host/project/paths, performs the full comparison again and a check of the original images; it consumes the marker before starting the applications |

`cycle_checks.py` (unpaid functional checks, optionally `--smoke --export`) and `cycle_export.py` (an actual SQLite export).
The standalone `cycle_export.py` and the read-only `cycle_flowdiff.py` now also require the full layout
`--src --cfg --state --project --expect-host --backup-root`, optionally `--running-from`.
In addition, the flow diff uses the `.env` from `--cfg` and rejects a Langflow URL outside the loopback nginx `http://127.0.0.1:8444` of this lab layout.
`cycle_checks.py --export` passes the same verified layout to the exporter.

## Guarantees and limits

- A backup cannot be used until it passes `verify_bundle`: the file list exactly matches `SHA256SUMS`, all hashes match,
  no symlink, no archive with an absolute path, `..`, a hardlink, a device or a write through a symlink.
- An error before the point of no return destroys nothing. A later restore error (images, files, `pg_restore`, roles, comparison)
  ends the command without starting the applications; `restore_ok.json` is deleted at the start and without it `start-apps` refuses.
- The marker is single-use, bound to the whole layout. `start-apps` re-checks the backup's SHA-256,
  the live data, the configuration, the files, the source and the original images/tags; an old marker alone is not evidence of the current state.
  An update and the writing of synthetic markers invalidate it. A marker from an older version without a layout is refused;
  a new one can be obtained only through `verify-restore` with the applications off and a full match.
- The cache (`reranker_models`) is not backed up; `postgres_data` is covered by the logical dumps (the physical PGDATA is only a copy set aside during a restore).
- Backups contain secrets (`.env`, keys, password hashes): directory 0700, files 0600, on the VM only; the tools' output carries only names,
  counts and fingerprints.
- Subprocess errors disclose only the return code or the type of failure. Arguments, stderr/stdout,
  third-party exception texts and unknown fields of probe responses are not copied into the console or the JSON evidence.
  Differing schema items are given in the JSON by fingerprint; the private bundle retains the material for diagnostics.
- Names of backups and synthetic markers are a single safe ASCII segment of at most 64 characters.

Tests: `python3 tests/test_cycle_driver.py` (offline, no Docker; bundle, paths, refusals, restore order).

## Verification

The offline tests check the bundle, paths, refusals, restore order and the CLI gates.
A specific deployment requires its own actual cycle with a recorded SHA, layout,
images and checksums. An offline PASS does not prove a live restore or a general production upgrade.
