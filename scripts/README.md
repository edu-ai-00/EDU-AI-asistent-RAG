# scripts — index

Operational and development scripts. **This is only an overview**: the exact commands, options and prerequisites of each script are in its
docstring or opening comment, and those are authoritative; nothing is duplicated here. The paths stay fixed: documentation, tests, the `Makefile`
and operational procedures all refer to them, so the scripts are not split into subfolders by group.

Run from the repository root. Most scripts use only the standard library; where one does not, the docstring says so.
Before running, check the specific mode in the docstring: for example, `bootstrap.py` writes configuration and starts services on a normal run. Use a read-only mode or dry run wherever the particular script offers one.

## Installation and first run

| Script | Purpose | Related documentation |
|---|---|---|
| `setup.py` | clean-install wizard: `plan` (read-only) and `install`; combines `install.py`, `host_preflight.py`, `bootstrap.py` and the role check, the school is selected in the browser | [CLEAN_INSTALL.md](../docs/CLEAN_INSTALL.md) |
| `prepare_ubuntu.sh` | preparation of a new Ubuntu host (Docker, Compose, tools) | [CLEAN_INSTALL.md](../docs/CLEAN_INSTALL.md) |
| `host_preflight.py` | host checks before the first installation, hand-over to bootstrap, writing the subnet into `.env` | [OPERATIONS_LINUX.md](../docs/OPERATIONS_LINUX.md) |
| `install.py` | offline preparation of the external installation configuration from a specific commit | [OPERATIONS_LINUX.md](../docs/OPERATIONS_LINUX.md) |
| `bootstrap.py` | clean checkout → running stack with one idempotent command | [ONBOARDING.md](../ONBOARDING.md) |
| `create_api_key.py` | Langflow key via REST into `.env` (called by bootstrap) | [ONBOARDING.md](../ONBOARDING.md) |
| `import_flows.py` | import of the 6 flows from `langflow_flows/ingestion/`: without `--missing-only` it **overwrites** (PATCH) every existing flow, including manual edits; with `--missing-only` (how bootstrap calls it) it creates only the missing ones; a targeted replacement of changed nodes is `flow_rollout.py` | [ONBOARDING.md](../ONBOARDING.md) |
| `smoke.py` | verification of a running stack without paid calls | [ONBOARDING.md](../ONBOARDING.md) |
| `db_roles.py` | database role contract: SQL generator, check and takeover of an isolated synthetic installation | [b06-c2-role-contract.md](../docs/plans/b06-c2-role-contract.md) |
| `eduai_layout.py`, `envfile.py`, `http_safe.py` | shared modules (installation location, `.env` editing, HTTP without following redirects); not run on their own | docstrings |

## Databases, migrations and flows (isolated synthetic databases)

The migration runner is not used on production or on a school installation (see [AGENTS.md](../AGENTS.md)).

| Script | Purpose |
|---|---|
| `migrate.py`, `migrate.sh`, `eduai_migrate/` | migration registry and runner (`status`, `plan`, `apply`, generators); `migrate.sh` is a thin wrapper. Specification: [spec.migration-registry.md](../specs/active/spec.migration-registry.md) |
| `flow_rollout.py` | targeted replacement of changed flow nodes from release A to B in an installed Langflow; it rejects anything that has diverged |
| `gen_clean_install.sh` | regenerates `postgres/init_clean/` from a controlled reference database (generated files are not edited by hand) |
| `align_existing_db.sh` | **unsupported, irreversible historical script for one specific database**: it runs `postgres/init` from 31 (does not skip 201/202), inserts the fixed entities `SKOLA_NOVA_VES…` and bulk-fills `legal_entity_code`; do not use on production or on a school installation, no supported upgrade exists |

## Network and nginx (R2, S2)

This checkout does not contain the deployment record, [docs/plans](../docs/plans/README.md) has only proposals and historical records. The scripts are not run on production without approval.

| Script | Purpose |
|---|---|
| `nginx_s2_switch.sh` | switching only the nginx service between the pre-S2 state and S2, with checks and an automatic return |
| `nginx_s2_guard.sh` | operational entry point for the S2 return: lets the unchanged S2 script run only when it is verified that R2 is not active |
| `nginx_r2_switch.sh` | the three steps of wiring nginx/cloudflared (the `CF-Connecting-IP` trust boundary) and a step-by-step return |
| `tunnel_subnet.py` | allocation and check of the `tunnel` subnet and a check of the Compose model (read-only) |
| `perimeter_check.py` | check from the outside that only what should be exposed is exposed; must run outside the VPN |

## Content and data (QA, documents, domains)

| Script | Purpose |
|---|---|
| `reingest.py` | bulk re-upload of documents via `POST /v1/ingest` (after a chunking change) |
| `upload_qa_packs.py` | upload of generated QA packs via `/v1/ingest`; metadata from `_manifest.json` |
| `qa_deactivate_packs.py` | listing and deactivation of QA packs (there is no API for it) |
| `qa_set_categories.py` | setting `tags->>'category'` of one pack from a CSV |
| `restore_declared_metadata.py` | SQL that restores manually entered document metadata from the inventory export after a reingest |
| `domain_inventory.py`, `domain_migration.py` | overview of domains per school and a reproducible, reversible migration of the curator's decisions; historical record of the procedure: [2026-09-06_domain_scope.md](../docs/nasazeni/2026-09-06_domain_scope.md) |

Historical procedures mention the external skill `qa-pack-variants`, which this checkout does not distribute. The scripts above work with already prepared packs or fix their metadata.

## Measurement and quality checks

The current regression harness is **[tests/eval_rag.py](../tests/eval_rag.py)** (see [DEVELOPMENT.md](../docs/DEVELOPMENT.md), section 3, layer 5).
The scripts below are older and read input files from `eval/` or `QA/`. Those are ignored by Git and are not in the tracked
tree, so without your own inputs they will not run.

| Script | Purpose |
|---|---|
| `eval_search.py` | golden set of cases against `/search` (retrieval and outcome); a different file from `tests/eval_search.py` |
| `eval_snapshot.py` | snapshot of the chunks the answers were built from and their before/after diff for a change |
| `run_doc_questions.py` | questions derived from the corpus: whether the expected document was hit and whether the QA branch helped or hurt |
| `run_negative.py` | negative cases: each must end in `no_answer` (leak between schools) |
| `run_selfvariants.py` | every stored QA variant must return its own record (ingest check) |
| `qa_pack_probe.py` | which QA answered the caller; input `QA/probe_cases.json` |
| `param_liveness.py` | whether every `settings.config->pipeline` parameter has an observable effect; at runtime it **modifies the active `settings` row** (do not run on a shared environment or on production) |
| `chunk_analysis.sql`, `chunk_analysis_summary.sql` | chunk quality of one document and of the whole corpus (run in `psql`) |

## Dependency maintenance

| Script | Purpose |
|---|---|
| `dump_pkgs.py` | listing of installed packages (name, version, license) inside a container; basis for [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) |
