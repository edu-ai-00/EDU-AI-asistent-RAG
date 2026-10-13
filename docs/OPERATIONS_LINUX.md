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
`pg_dump --schema-only` of both first. If they differ, stop: a migration is a
separate decision. Only the `edu_ai` database moves; Langflow's own database is
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

A release records the exact commit, images, tests and limitations. An old PASS for
a different commit does not replace a new clean run. See [RELEASING.md](RELEASING.md).

Actual VM evidence and the boundary of the final source: [public-source.md](releases/public-source.md).
QA exact can work without a provider key; generated answers and embeddings need Bifrost.
