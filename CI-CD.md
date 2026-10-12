# CI/CD automation proposal

> **Status: future proposal, not a guide.** None of the proposed CI/CD automation
> is implemented, and this page is not a supported production procedure, an
> automatic upgrade or a documented restore. `.github/workflows/`,
> `deploy/services.yml`, `deploy/docker-compose.prod.yml` and `scripts/deploy.sh`
> do not exist. Only `scripts/migrate.py` (B03e-1) exists, and its scope is
> **limited to isolated synthetic databases**, not production or school installations
> ([AGENTS.md](AGENTS.md), [spec.migration-registry](specs/active/spec.migration-registry.md)).
> The actual installation procedure is [CLEAN_INSTALL.md](docs/CLEAN_INSTALL.md); the status
> of updates is in [b06-upgrade-strategy.md](docs/plans/b06-upgrade-strategy.md).

## Summary

The project's deployment mechanics can be automated almost completely. The code is
not overly complicated; the current risks arise mainly because production has no
unambiguous "desired state" and part of the deployment depends on manual file
selection, knowledge of the links between files and Docker images, and manual
tracking of migrations.

Recommended target process:

```text
merge to main
      ↓
create a prod-* tag / GitHub Release
      ↓
tests + build Docker images
      ↓
production deploy runner
      ↓
backup → migrations → deploy → healthcheck → smoke test
      ↓
success: record the release
failure: automatic rollback of images and configuration
```

Automation can reliably verify that an image was built, the migration ran, the
services are healthy and the smoke test passed. It can confirm the functional or
content correctness of a change only if an automatic test exists for it.

## Triggering a production deployment

Text in a commit message is not a suitable control mechanism. Recommended model:

1. A push or pull request to `main` triggers CI and the build.
2. A production deployment is triggered only by a tag of the form
   `prod-YYYY-MM-DD.N` or by creating a GitHub Release.
3. Once the pipeline is sufficiently proven, it is possible to move to an
   automatic deployment of every merge into the protected `main` branch.

A tag or Release is an unambiguous, auditable instruction "this is an update" and
determines exactly which commit is to be deployed.

## Production runner

The simplest option for the current infrastructure is a GitHub Actions
self-hosted runner on the production server or on a separate machine connected to
the same VPN.

The runner makes outbound connections to GitHub, so there is no need to expose
production SSH to the internet for CI. However, a production runner has
practically administrator privileges through Docker, and therefore the following
must hold:

- only the production deploy workflow may use the runner;
- code from an ordinary pull request is never run on the production runner;
- the `main` branch, the production tags and the GitHub Environment must be protected;
- secrets are stored outside Git, ideally directly on the server or as protected
  environment secrets;
- at most one production deployment may run at a time.

## Deployment flow

The deployer should perform the following steps:

1. Find the last successfully deployed release.
2. Determine the files changed relative to that release.
3. Map the changes to the affected services.
4. Run the relevant tests and `docker compose config --quiet`.
5. Build immutable Docker images tagged with the commit SHA and store them in a
   container registry.
6. Create a database backup.
7. Run the database migrations not yet applied.
8. Deploy only the affected services.
9. If Langflow flows changed, import their new version.
10. Run the healthchecks, `scripts/smoke.py` and a test of the function that
    actually changed.
11. Write the commit, time, image digests, migrations and result to the release record.
12. On failure, restore the previous images and configuration and mark the
    deployment as failed.

The build must come directly from the exact Git commit. This eliminates manual
SCP, CRLF removal and the error where a source file was copied to the server but
its Docker image was not rebuilt.

## Mapping changes to services

The mapping must be versioned, for example in `deploy/services.yml`. An unknown
path must stop the pipeline rather than be silently ignored.

| Changed path | Action |
|---|---|
| `api/**` | test and rebuild `edu_ai_api` |
| `search/app/**` | test and rebuild `edu_ai_search` |
| `search/requirements.txt` | rebuild `edu_ai_search` |
| `search/eduai_normalize/**` | rebuild both `edu_ai_search` and the Langflow image `edu_ai` |
| `web_chat/**` | pytest, frontend build and rebuild `edu_ai_web_chat` |
| `requirements-langflow.txt`, root `Dockerfile` | rebuild `edu_ai` |
| `Dockerfile.postgres` | rebuild the Postgres image; deploy with extra caution |
| `postgres/migrations/*.sql` (number ≥ 217) | migration runner; isolated synthetic DBs only, see [Database migrations](#database-migrations) |
| `postgres/init/*.sql`, `postgres/init_clean/**` | history and the generated B216 baseline only; the runner does not run them, a change requires approval and regeneration |
| `qa_portal/**` | test and rebuild `edu_ai_qa_admin` (the service was missing from the proposal) |
| `langflow_flows/**` | invariant tests and flow deployment per [Langflow flows](#langflow-flows) |
| `nginx/**` | `nginx -t` validation and deployment of the nginx configuration/image |
| Compose files | configuration validation and evaluation of the affected services |
| `docs/**`, `README.md` | no production deployment |

## Immutable Docker images

Production should not build an indeterminate local tag such as
`edu_ai_search:1.0`. CI should create the image and the production Compose should
use a concrete digest:

```yaml
services:
  edu_ai_search:
    image: ghcr.io/<ORG>/edu-ai-search@sha256:<digest>
```

The release manifest keeps the digest of every service. For unchanged services,
the digest is taken over from the previous release. Advantages:

- we know exactly what code runs in the container;
- a rollback only restores the previous manifest;
- it is not possible to accidentally run an old local image under the same tag;
- there is no need to copy source files into `/opt/edu_ai`;
- only the services that actually changed are deployed.

For development, local `build:` can still be used via a development Compose
override. The production Compose should use only finished images.

## Database migrations

The `postgres/init/` directory (history up to 216) is not run automatically on an
existing database. Migration tracking is already introduced by B03e-1: the table
`edu_ai.schema_migration` (`migration_id`, `kind`, `checksum`, `release`,
`applied_at`, `applied_by`, `notes`; the original proposal `schema_migrations`
with `release_sha` does not exist) and `scripts/migrate.py` (`status`, `plan`,
`init`, `apply`, generators). The runner works only with the B216 baseline
(`postgres/init_clean`) and the files `postgres/migrations/NNN_*.sql` with number
≥ 217 and **is approved only for isolated synthetic databases**; production,
school installations and adoption of an existing installation (`adopt`, D11) are
not covered, and `adopt` is not implemented. The release manifest
`postgres/releases/<release>.json`, which `apply` requires, is not published in
this public tree.

The runner already does the following today (according to the code in
`scripts/eduai_migrate/` and the specification; it is not run on production):

1. holds a PostgreSQL advisory lock, so a second `apply` terminates;
2. loads migrations in a deterministic order according to the release manifest;
3. compares the SHA-256 checksum with the manifest and the applied rows;
4. rejects a changed or unexpected state before running anything;
5. executes each migration and its registry row in a single transaction;
6. writes `migration_id`, `kind`, `checksum` and `release`.

What remains future from the proposal: a one-time adoption of an existing
production database (baseline/adopt) and any use on production. The original idea
of "marking already applied migrations" is therefore not a supported procedure.

The proposed rule "automatically allow forward-compatible changes" is only a
proposal, and stricter repository rules apply to the DB: any schema or index
change is approved in advance according to [AGENTS.md](AGENTS.md). Examples from
the original proposal:

- `CREATE TABLE IF NOT EXISTS`;
- a new nullable column;
- a new index "suitable for production operation" — but the runner rejects
  `CREATE INDEX CONCURRENTLY` and `VACUUM` in a migration from 217 on, because it
  controls the transaction;
- adding a new value or seed data using an idempotent `ON CONFLICT`.

Destructive changes, renames and immediate column removal must not pass through a
one-step automatic deployment. They should use the expand/contract procedure:

1. add a new structure compatible with the old code;
2. deploy code that can handle both the old and the new structure;
3. convert the data;
4. only in a later release, remove the old structure.

A rollback of the application does not automatically revert a database migration.
An additive change must be designed so that it does not harm an older image.

## Langflow flows

The current state, where the live Langflow DB is the source of truth and the JSON
in the repository is only a disaster-recovery export, is not compatible with a
fully automatic GitOps deployment.

The following needs to be done once:

1. Export the verified production flows to Git.
2. Run `tests/test_flow_invariants.py`.
3. From then on, treat Git as the source of the desired state.
4. A production change made through the UI is first exported, checked and
   committed.

There are three different tools with different behaviour, and the proposal must
choose the right one:

| Tool | Behaviour |
|---|---|
| `scripts/import_flows.py --missing-only` (used by bootstrap) | creates only the missing `endpoint_name`; an existing flow, even a user-modified one, is kept |
| `scripts/import_flows.py` (explicit import) | **overwrites** (PATCH) every flow with the same `endpoint_name` with the content from Git; it is idempotent, but discards manual changes made in the UI |
| `scripts/flow_rollout.py --from-dir A --to-dir B (--check \| --apply)` | targeted replacement of changed nodes from release A to B; it rejects a flow that has diverged from A; `--apply` requires `--confirm-maintenance-window` |

The proposal therefore should not give the deployer a full `import_flows.py` as a
routine update: once Git is taken as the source of truth, it is intended for
restoring a broken flow, and a small flow change is deployed by
`flow_rollout.py`. Before deployment, a backup of the affected DB rows or an
export of the flows is created, and afterwards a non-mutating end-to-end probe is
run.

On failure, the flows are restored from the previous release; no automated
procedure exists and the rollback has not been verified on production.

## Nginx and production drift

The production `nginx.conf` must not contain manual hotfixes that are not in Git.
Before automation is switched on, the production configuration must be compared
once with the repository and the legitimate changes committed.

The ideal is to bundle the versioned `nginx.conf` into a dedicated nginx image.
Since 2026-09-02 nginx mounts no secret (neither `.htpasswd` nor `auth_keys.conf`),
so the entire configuration can go into the image without exceptions. Before
deployment the pipeline runs `nginx -t`, and after deployment
`scripts/perimeter_check.py --strict` from a runner outside the VPN.

Today (`docker-compose.yml`) no nginx image is built: the directory `nginx/conf`
is mounted read-only into `/etc/nginx`, and the parent `nginx/` is not mounted.
If the configuration stays bind-mounted, the deployer must update only the
versioned configuration and must never overwrite the authentication files.

## Secrets and persistent data

The following must not enter the release artifact or the Docker images:

- `.env`;
- `LANGFLOW_SECRET_KEY`;
- OpenAI API keys;
- TLS private keys;
- nginx authentication files;
- `langflow_data/`;
- `uploads/`;
- `pgadmin_exports/`;
- database volumes;
- existing backups.

Persistent paths should be listed in the production Compose as absolute paths or
via a single variable, for example `DATA_ROOT=/opt/edu_ai/shared`. This separates
the data from the versioned release files and removes the risk of deleting them
when old release directories are cleaned up.

`LANGFLOW_SECRET_KEY` is never generated during deployment. A production
deployment also never runs `scripts/bootstrap.py` and never uses
`docker compose down -v`.

## Validation and rollback

The order of checks should go from the cheapest:

1. static tests and lint;
2. `tests/test_flow_invariants.py`, if flows change;
3. `web_chat` tests;
4. build of the affected Docker images;
5. validation of the resulting production Compose;
6. DB backup and migrations;
7. `docker compose up -d --wait` for the affected services;
8. verification of the actual `/health` endpoints;
9. `scripts/smoke.py`;
10. a test of the specific new function.

The Docker `(healthy)` status alone is not enough. The check must verify the
endpoint and the code path that changed in the given release.

On failure:

- restore the previous image digests and the Compose manifest;
- start the previous version of the service again;
- for flows, import the previous Git version;
- run the healthcheck and smoke test again;
- leave the additive DB migrations in place;
- keep the log and backup of the failed deployment.

A rollback does not solve a destructive migration. Such a migration must not be
accepted into the fully automatic branch at all.

## Proposed files

```text
.github/workflows/ci.yml                 # DOES NOT EXIST
.github/workflows/deploy-production.yml  # DOES NOT EXIST
deploy/services.yml                      # DOES NOT EXIST
deploy/docker-compose.prod.yml           # DOES NOT EXIST
scripts/deploy.sh                        # DOES NOT EXIST
scripts/migrate.py                       # exists (B03e-1), isolated synthetic DBs only
```

### `ci.yml`

- runs for pull requests and pushes to `main`;
- performs tests, Compose validation and the build;
- must not use the production runner or production secrets;
- may publish images only from the protected branch.

### `deploy-production.yml`

- runs when a `prod-*` tag or GitHub Release is created;
- uses the production environment and the production runner;
- has a concurrency lock;
- pulls or verifies the images for the specific commit;
- runs a single versioned deploy script;
- always publishes the deploy log and the release manifest.

### `deploy/services.yml`

- maps changed paths to services, tests and special steps;
- explicitly marks changes that require a migration or a flow import;
- fails safely on an unknown path.

### `deploy/docker-compose.prod.yml`

- contains no local `build:`;
- uses immutable image digests;
- uses the production VPN port bindings;
- mounts persistent data and secrets from separate paths;
- is the only versioned production Compose configuration.

### `scripts/deploy.sh`

- is idempotent;
- always uses explicit production Compose files;
- verifies absolute target paths;
- creates a backup;
- runs migrations, deployment, healthchecks and rollback;
- neither creates nor changes secrets.

## Recommended rollout phases

### Phase 1: Deterministic application deployment

- introduce CI;
- introduce an explicit production tag;
- introduce a production runner;
- build immutable images;
- automatically deploy only changes to `api`, `search` and `web_chat`;
- run the healthcheck and smoke test;
- automatically restore the previous images.

This phase removes most of the manual work and the most common error, a forgotten
rebuild.

### Phase 2: Migrations and configuration

- create a migration ledger and baseline;
- move the production VPN Compose configuration into Git;
- align the nginx configuration with Git;
- separate persistent data from release files;
- automate additive migrations and safe Compose changes.

### Phase 3: Langflow GitOps

- declare Git the source of truth for flows;
- automate backup, import, probe and rollback of flows;
- prevent unaudited changes made only in the production UI.

### Phase 4: Fully unattended routine releases

- add unit and integration tests, especially for the API and search logic;
- after a period of parallel verification, remove the approval for routine
  application changes;
- keep approval only for destructive DB changes, secrets and infrastructure.

## Conclusion

Routine application updates can be fully automatic. Migrations, flows and
configuration can also be automated once they have tracking, an unambiguous source
of truth and safe rollback behaviour.

A manual or approved gate should remain only for:

- destructive database changes;
- changes to secrets;
- network and cloud infrastructure;
- changes whose correctness is not yet covered by tests.

The manual procedure can then remain as a recovery runbook for an emergency manual
deployment. The standard path should be a versioned and repeatable deploy
workflow, not a manual SCP session driven by a human or an AI assistant.
