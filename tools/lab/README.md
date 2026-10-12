# tools/lab — lab backup and restore (isolated synthetic stack only)

> **Status: lab tools, not a production backup system.** The scripts are carried over unchanged from the `edu_ai_acc`
> acceptance runs on the VM. They are written for a single fixed lab layout (below) and **must not be run on
> production, on a school installation or against someone else's stack**. The restore is destructive (it deletes the project, config and state).
> The verification applies only to the exact bytes listed in section 1; every modification is new, unverified behaviour.

Operational context: [docs/OPERATIONS_LINUX.md](../../docs/OPERATIONS_LINUX.md) (the section on backup and restore).
For the `edu_ai_final` layout (an isolated lab) with verification of its own stack see [`cycle/`](cycle/README.md); the scripts below remain unchanged.

## 1. Files, origin and SHA-256

| File | SHA-256 | Role |
|---|---|---|
| `acc_backup.sh` | `96676ac1356f711c190c1d076881d14b5b8dd455e21d0646ebcaa5b805ba5a20` | backup |
| `acc_restore.sh` | `0e9b7fb890c5d39adc704c935df7cf2be6261ca8b0469346efefba5829555770` | full restore |
| `acc_snap.sh` | `145f831fd29e168a1882cfa760034551ddf2e8fdd5a191f855d9462bde3d2784` | state snapshot (hashes, counts, IDs; no secret values); the restore reads the expected state from it |
| `acc_lib.sh` | `84798145554e93b112866ec3a172519c084bcbea24ef570ba4e02d5ed5b62825` | shared variables (`source`d by all three) |

The files are LF, with no change of content; `sha256sum tools/lab/*.sh` must give the values above.

**Origin (byte-identical in all three acceptances, except one line in `acc_lib.sh`):**

| Acceptance (`tests/eval_out/…/tools/`) | What passed with them |
|---|---|
| `acc_closeout_20261009` (source `7ef6047`) | backup `A1`, full restore `A1`: raw baseline identical, roles `MATCH` before the applications |
| `sqlite_export_acceptance_20261010` (source `fbadbf0`) | backup and restore `CI_B3` (`rec/G4_candidate_backup_restore.txt`: `pg_restore` rc=0 for both DBs, raw baseline identical, `DB_CONTRACT: MATCH`, `snap_CI_POST == snap_CI_PRE`, smoke 26/0/1); restore `UPD_B2` |
| `field_order_acceptance_20261010` (source `b78b2e0`) | restore `UPD_B2` (`rec/S1_restore_UPD_B2.txt`: raw baseline identical, roles `MATCH`, RPO markers 0/0) |

The selection was not made by file name or date. `acc_backup.sh`, `acc_restore.sh` and `acc_snap.sh` have the same SHA-256 in all three
directories, so this is not a decision between versions. The only difference is in `acc_lib.sh`: the older
`acc_closeout` (SHA-256 `8265cb92b4bc5e93e2fa5c49988de9c756329367b77f97ce85c7def898325275`) has `SRC=$A/src`, the versioned
form has `SRC=${ACC_SRC:-$A/src}` and is in the copies from both 2026-10-10 runs. Without `ACC_SRC` set the behaviour is identical.
The evidence does not explicitly show which form the VM loaded during the `CI_B3` run; what is verified is that both give
the same value with the default `SRC`.

**Not carried over, and why:** `acc_verify.sh` and `acc_identity.sh` (depend on the unversioned `probe.py`, `ask.py`,
`api_tools.py`, `b06_qa_fixture_cases.py`), `sbx.sh` and `sb_*.sh` (lab sandbox: a network namespace and its own
Docker daemon on a specific VM). They remain only in the ignored evidence in `tests/eval_out/`.

## 2. Prerequisites (lab, hard-wired)

- **Own isolated synthetic stack** of the Compose project `edu_ai_acc` on the VM, inside a sandbox (own netns, own
  `dockerd` and containerd; `sbx.sh` slips them the socket as the default daemon). Without the sandbox the scripts talk to the host's default Docker daemon.
- Directories: `$HOME/acc/{src,config,state,backup,rec,tools}`. `src` is a clean Git checkout (see OPERATIONS_LINUX, section 2),
  `config` contains `.env` and `install-record.json`, `state` contains `uploads`, `langflow_data`, `sqllite_export`, `bifrost_data`.
  `src` contains symlinks to these four directories and to `.env`. `ACC_SRC` overrides only `src`.
- **The scripts are run from `$HOME/acc/tools/`**: `acc_lib.sh` is loaded by the fixed path `$HOME/acc/tools/acc_lib.sh`. Copy
  `tools/lab/*.sh` there (keep `+x`) and verify the SHA-256 after the copy.
- Fixed names: project `edu_ai_acc`, DB user `edu_ai_admin`, containers `edu_ai_postgres` and `edu_ai`
  (`container_name` in `docker-compose.yml`), DBs `edu_ai` and `langflow`.
- The source contains `scripts/db_roles.py` with the commands `verify`, `globals`, `reapply --confirm-isolated`. `acc_snap.sh` calls bifrost
  at `127.0.0.1:8099`.
- `bash`, `python3`, `docker compose` (≥ 2.24), `git`, `tar`, `sha256sum`, **passwordless `sudo`** (state belongs to the container users).
- The scripts have no `set -e` and no parameter checking: a missing argument or environment is not detected in advance.

## 3. Usage

Everything inside the sandbox, as a regular user (`sbx.sh bash $HOME/acc/tools/<script> …`). The stack must be running.

```bash
# 1) snapshot of the state right before the backup (into ~/acc/rec/snap_<LABEL>.txt)
bash $HOME/acc/tools/acc_snap.sh BEFORE_X

# 2) backup NAME into ~/acc/backup/NAME (writers are stopped for the backup window and then started again; ~65–80 s)
bash $HOME/acc/tools/acc_backup.sh X

# 3) FULL restore (DESTRUCTIVE), expected state = the snapshot from step 1
bash $HOME/acc/tools/acc_restore.sh X BEFORE_X
```

The snapshot from step 1 must be taken right before the backup with no writes in between, otherwise the raw baseline will not match after the restore.
Anything created after the backup is lost, according to the RPO.

**Backup** `~/acc/backup/NAME` (directory 0700, files 0600): `edu_ai.dump`, `langflow.dump` (custom format), `globals.sql`
(roles and grants; contains password hashes, hence 0600), `role_verify_at_backup.txt`, `config.tgz` (`.env`, `install-record.json`),
`state.tgz` (uploads, `langflow_data` including `secret_key`, `sqllite_export`, `bifrost_data`), `META.txt`, `SHA256SUMS`.
It writes only names and hashes, not secret values.

**Restore** in this order: check of SHA256SUMS → `down -v` of the project → deletion of `config` and `state` → unpacking → `postgres` only →
`pg_restore` of both DBs → **`db_roles.py reapply` and `verify` before any application service** → comparison of the raw baseline
(counts, alembic head, md5 of every flow, drafts, variables) against the snapshot → only then the other services.

## 4. Limitations and findings (check of safeguards, 2026-10-10)

The scripts were not changed because of them (verified logic, see section 1). Protection in the script code is missing at these points; in the lab it is
currently replaced by the sandbox and an attentive operator.

1. **No check that it runs against its own stack.** Containers are addressed by fixed name (`docker exec/start edu_ai_postgres`,
   `docker inspect edu_ai`) and nothing verifies that they belong to the project `edu_ai_acc` or that the daemon is the sandbox. Outside the sandbox, on a name
   collision `up -d postgres` would probably fail (untested), the script would continue and `pg_restore --clean --if-exists` would go into **someone else's** database. The only thing
   that limits `down -v` is `-p edu_ai_acc`. **Run only through `sbx.sh`.** (`reapply` requires `--confirm-isolated`, but
   does not verify isolation.)
2. **The `SHA256SUMS` check is not enforced on restore.** Its result is printed and the restore continues; deletion of the project, `config` and `state`
   happens even on a mismatch or a missing archive. Before a restore verify manually with `sha256sum -c` that all seven files are OK.
3. **Step results are not enforced.** The `pg_restore` rc, the role `reapply`/`verify` and the raw baseline diff are printed, but the services
   are started even after a failure. The order (roles before applications) is correct, but keeping to it on error depends on reading the output.
4. **Scope of the restore's deletion:** `find $SRC -maxdepth 1 -type l -exec rm -f` deletes **all** symlinks in the root of `src`, not just the five of its own;
   `ACC_SRC` can point elsewhere. `rm -rf $CFG` and `sudo rm -rf $ST` precede any check.
5. **State not covered:** the Docker volumes `pgadmin_data`, `qa_admin_state` and `reranker_models` are not backed up and `down -v` deletes them.
   Postgres data is restored from the dumps, not from the volume. The source checkout, images and Cloudflare are not backed up
   (the image identity is only in `META.txt`).
6. **Minor points:** `META.txt` and `SHA256SUMS` are created after `chmod 600` and stay 0644 (inside a 0700 directory, no secrets);
   if `pg_dump` fails the script ends with `exit 1` and the writers stay stopped (no `trap`); the snapshots in `rec/` carry 16-character
   SHA-256 prefixes of `.env`, `LANGFLOW_SECRET_KEY` and API keys (a hash, not a value), and `rec/` is created with the default umask.

They do not touch someone else's stack or the global Docker state: the scripts use no `prune`, and no `rm`/`stop` outside the project
`edu_ai_acc` and their own directories (with the reservations from points 1 and 4).

## 5. What this directory does not promise

Backup of production or of a school installation, a supported upgrade, automation (cron, rotation, off-site copies), backup encryption,
verification of a restore outside the lab. A change to the scripts requires a new restore acceptance on an isolated synthetic stack.
