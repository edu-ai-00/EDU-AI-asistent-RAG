# EDU-AI — operations on Linux

A new installation is prepared by the [wizard](CLEAN_INSTALL.md). The source, the
configuration and the state have separate directories; operational secrets and data
are not added to Git. Neither this guide nor the lab tools introduce a supported
production upgrade.

## Checking the running stack

For the default layout:

```bash
SRC=$HOME/edu-ai/src
CFG=$HOME/edu-ai/config
STATE=$HOME/edu-ai/state
PROJECT=edu_ai
cd "$SRC"
python3 scripts/smoke.py --source-root "$SRC" --config-dir "$CFG" --state-dir "$STATE" --project "$PROJECT"
python3 scripts/db_roles.py verify --container edu_ai_postgres
docker compose --project-directory "$SRC" --env-file "$CFG/.env" -f "$SRC/docker-compose.yml" -p "$PROJECT" ps
```

Require roles `MATCH`, a smoke without FAIL and the expected running services. A
disabled optional Cloudflare or a chat waiting for a school have an explicit
status. The smoke is a free functional check; it does not replace an ingest, a
regression on your own corpus or a restore test.

## School and provider

A new configuration allows selecting an active school in `/chat/`. The user selects
it in the browser; no additional SQL or terminal command is needed. A school change
clears the conversation and the domains. The selection is neither authentication
nor a role assignment. A fixed installation may have `CHAT_LEGAL_ENTITY`; the
wizard keeps an existing configuration.

The provider and its key are managed by Bifrost. Its UI and admin API need the
admin login (`BIFROST_ADMIN_USERNAME` / `BIFROST_ADMIN_PASSWORD` in the config
`.env`); bootstrap places `bifrost/config.json` (the login from those variables,
inference without auth for the internal callers) into `bifrost_data` and restarts
Bifrost when it changes. Change the login in the `.env` and recreate the container
(`… up -d bifrost`, see [CLEAN_INSTALL.md](CLEAN_INSTALL.md); a restart keeps the old
values); keep
"Enforce Virtual Keys on Inference" and "Allow Direct API Keys" off (see
[CLEAN_INSTALL.md](CLEAN_INSTALL.md)). A switch saved in the UI is kept over
`config.json`; reinstalling or restarting does not reset it. An in-place upgrade of an installation
running Bifrost v1.x is not supported (the v2 database migration is one-way): install
cleanly and enter the provider key again. Restrict administration access;
do not put keys into Git or into public reports. A paid verification needs its own
scope, test inputs and a recorded result.

## QA export

The exporter is the Langflow flow `langflow_flows/ingestion/EDU_AI_EXPORT_QA_PACK.json` (alias `edu-ai-export-qa-pack`).
The tools and interfaces are in [scripts/README.md](../scripts/README.md),
[tools/README.md](../tools/README.md) and the [test index](../tests/README.md).
The SQLite export contains QA data; it is a private output of the specific
installation. The [QA audit](integrations/offline-qa-rules.md) describes the
transferred rules and the gaps for mobile offline operation. The FTS index alone
does not document parity.

## Moving the application data to a new installation

Rehearsed on a lab VM on 2026-10-13 (production data into a clean installation of
this branch); a production cutover remains a separate, explicitly approved step.
It applies when both databases have the same `edu_ai` schema; compare
`pg_dump --schema-only --no-owner --no-privileges -n edu_ai` of both first (the new
installation's objects belong to the role contract's `edu_ai_owner`). If they differ,
stop: a migration is a separate decision. Only the `edu_ai` database moves; Langflow's own database is
not transferred (the flows come from the repository) and Bifrost's provider key
is entered again in its UI. The new installation keeps its own roles, grants and
secrets.

1. On the source host, a data-only dump (read-only there):
   `docker exec edu_ai_postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d edu_ai --data-only -n edu_ai -Fc' > edu_ai_data.dump`.
   The warning about a foreign-key cycle on `legal_entity` is expected.
2. On the target, stop the services that write: `… stop edu_ai_api edu_ai_search edu_ai edu_ai_web_chat edu_ai_qa_admin`.
3. In ONE transaction, as the database superuser of the target container: `TRUNCATE`
   every table of schema `edu_ai` (`RESTART IDENTITY CASCADE`), then load the SQL that
   `pg_restore --data-only --disable-triggers` produces from the dump. A failure leaves
   the target unchanged.
4. Compare the row counts of every table with the source.
5. `python3 scripts/setup.py install` brings the services back and verifies them
   (bootstrap, smoke, roles).

The dump holds real personal data (including logged questions): keep it off Git and
shared storage, and delete every copy when the transfer is verified.

## Backup, restore and source change

A backup must include both databases, the roles, the external config including
keys, the necessary bind mounts and named volumes, the source and the image
identity. It contains secrets; protect it and verify the SHA-256 as well as an
actual restore on your own isolated copy. A file change or a large object added
after the backup must disappear after the restore. Start the applications only
after checking the roles, data, keys, files and the correct images.

[`tools/lab/`](../tools/lab/README.md) contains older lab helpers.
[`tools/lab/cycle/`](../tools/lab/cycle/README.md) is a driver with strict owner
gates for a single layout; it must not be presented as a general updater. Before
using it, verify all the prerequisites from its README. The B03e migration runner
is approved only for isolated synthetic databases; do not use it on production or a
school installation. Roles contract: [B06 C2](plans/b06-c2-role-contract.md).

After `git pull` in the source checkout, `python3 scripts/setup.py install`
continues the same installation, and bootstrap rebuilds only the images whose
inputs changed: each built image carries a second tag `<repository>:src-<fingerprint>`
of its Dockerfile and the files it copies, as committed in git; a missing or
different one means a rebuild of that service, the previous image stays as
`<image>-prev`. An uncommitted change or a checkout without git rebuilds that image
every time (or, outside git, never: use `bootstrap.py --build`). Base-image
updates without a source change still need `--build`. Not covered by this:
database migrations, flows that already exist in Langflow (only missing ones are
imported) and a Compose change of a service's mounts, which the wizard refuses
(`RESUME_STACK_OTHER_LAYOUT`).

A change of `Dockerfile.postgres` within one PostgreSQL major version (e.g. 16.3 → 16.15,
pgvector 0.7.4 → 0.8.7, pg_search 0.25.1 → 0.25.11) rebuilds the postgres image and recreates
the container on the same data volume; a minor version needs no dump. The extensions
keep their old catalog version until `ALTER EXTENSION … UPDATE`; bootstrap runs it for
`vector` and `pg_search` (step 8b) only after a passing smoke test, and only when the
image has a newer version. The same transaction sets the role contract's privileges
on the extension schemas again: the functions an update adds would otherwise be
executable by PUBLIC, and the roles check would report it. It is one-way: from then
on, a return to the previous image (`<image>-prev`) needs a backup restore. With
`--skip-smoke` or a failed run it is skipped; run the installation again rather than
a bare `ALTER EXTENSION` by hand. Until step 8b has run, `db_roles.py reapply` and
`adopt` refuse with "baseline mismatch: extensions" (they expect the versions in
`postgres/role_contract/matrix.json`), so finish the installation before a reapply.
The smoke test and step 8b run a BM25 and vector query as `edu_ai_search`. Search
works before and after (lab, 2026-10-18).

### A new PostgreSQL major version

The wizard (`POSTGRES_MAJOR_CHANGED`) and bootstrap's preflight refuse when the
postgres data volume holds another major version than `Dockerfile.postgres` (e.g. 16
data, an 18 image): postgres cannot start on it, and nothing is changed. The data
moves through a dump and a clean database:

1. With the previous commit still checked out and running, make a full backup (see
   below) and the data-only dump of step 1 in
   [Moving the application data](#moving-the-application-data-to-a-new-installation).
2. Stop the stack with `… stop` (not `down`: the wizard continues an installation
   only while its containers exist), then remove only the postgres container and the
   data volume the refusal names: `docker rm edu_ai_postgres` and
   `docker volume rm <project>_postgres_data`. The other containers and volumes
   (Bifrost, pgAdmin, uploads) stay.
3. Check out the new commit and run `python3 scripts/setup.py install`: a clean
   database on the new version, with the roles and the flows from the repository.
   Langflow's own database is not carried over, so the API key in the `.env` belongs
   to the old one: bootstrap stops at "could not be verified (HTTP 403)" without
   changing anything. Replace it with
   `python3 scripts/create_api_key.py --url <the Langflow URL bootstrap printed> --force --no-input`
   and run the installation again.
4. Load the data with steps 2–5 of the same section. When comparing the schemas,
   `pg_dump` 18 adds `SET transaction_timeout = 0;`; that line is not a difference.

PostgreSQL 18 images declare `VOLUME /var/lib/postgresql`. The data stay in the named
volume at `/var/lib/postgresql/data` (`PGDATA` in `Dockerfile.postgres`); the
container also gets an empty anonymous volume, which is not the data.

A release records the exact commit, images, tests and limitations. An old PASS for
a different commit does not replace a new clean run. See [RELEASING.md](RELEASING.md).

Actual VM evidence and the boundary of the final source: [public-source.md](releases/public-source.md).
QA exact can work without a provider key; generated answers and embeddings need Bifrost.
