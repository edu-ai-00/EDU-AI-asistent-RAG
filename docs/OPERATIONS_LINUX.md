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
setup token (`BIFROST_SETUP_TOKEN` in the config `.env`) until an admin account
exists; bootstrap places `bifrost/config.json` (inference without auth for the
internal callers, the token reference) into `bifrost_data` and restarts Bifrost when
it changes. When the admin account is created, keep "enforce auth on inference" off
(see [CLEAN_INSTALL.md](CLEAN_INSTALL.md)). An in-place upgrade of an installation
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

A release records the exact commit, images, tests and limitations. An old PASS for
a different commit does not replace a new clean run. See [RELEASING.md](RELEASING.md).

Actual VM evidence and the boundary of the final source: [public-source.md](releases/public-source.md).
QA exact can work without a provider key; generated answers and embeddings need Bifrost.
