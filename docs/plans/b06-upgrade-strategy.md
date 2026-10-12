# B06-U: version management and supported updates

The public copy omits internal acceptance reports and links to them; this proposal is not evidence of the current deployment.

Date: 2026-10-06. **Status: the owner's requirement is recorded, the strategy is a proposal. The update tool is not implemented.**
Builds on the [B06 installer](b06-clean-install-proposal.md), B03e and the overall plan.
This note was produced by reading the configuration and documentation. No version, image, database, `.env`, key or production service is changed by it.

## 1. Goal and the current gap

An administrator is to later update a specific installation to a specific tested EDU-AI release while preserving data, configuration and keys.
A release defines a jointly verified set of component versions. Checking for new available versions and introducing them into production are two separate steps.
Automatic deployment of floating `latest` tags is not the proposed way of updating.

B06-1 prepares a new configuration; it is not an updater of an existing school. B03e-1 has a migration runner only for isolated synthetic databases.
Supported school and production updates remain open; this boundary is set by [AGENTS.md](../../AGENTS.md).
The S2/R2 scripts are specialised transitions of nginx and the network wiring, not a general updater of all services.

## 2. What is pinned today

The table describes declarations in the local source tree, not versions found live on production. The new target versions are not chosen here.

| Area | Declaration and gap | Source checked |
|---|---|---|
| Langflow | base `1.10.1`, local image `edu_ai:1.1.0`; base tag without a digest; the added packages partly have loose versions, constraints are produced from `pip freeze` of the base image during the build | [Dockerfile](../../Dockerfile), [requirements-langflow.txt](../../requirements-langflow.txt), [Compose](../../docker-compose.yml) |
| PostgreSQL and extensions | base `pgvector:0.7.4-pg16` with a digest, `PG_SEARCH_VERSION=0.25.1`; the `.deb` is tied to PostgreSQL 16, downloaded without a checksum verification in the Dockerfile | [Dockerfile.postgres](../../Dockerfile.postgres) |
| Proxy and tunnel | `nginx:1.27-alpine` and `cloudflared:2026.8.1`; tags alone do not guarantee immutable content | [Compose](../../docker-compose.yml) |
| Bifrost and pgAdmin | `bifrost:v1.6.7`, `pgadmin4:9.11.0`; persistent state and compatibility must be verified separately | [Compose](../../docker-compose.yml) |
| Ingest API | Python base `3.11.16` with a digest and exact Python dependencies; apt system packages are resolved at build time | [API Dockerfile](../../api/Dockerfile), [API requirements](../../api/requirements.txt) |
| Search | Python base `3.12-slim` with a digest; a number of dependencies use a `>=` lower bound, the lemma engine `simplemma==2.0.0` is pinned | [Search Dockerfile](../../search/Dockerfile), [Search requirements](../../search/requirements.txt) |
| Chat and QA portal | the frontend has `package-lock.json`; base Node `24.18.0-alpine`, runtime Python `3.12-slim` without a digest; the QA backend also has `>=` ranges | [chat Dockerfile](../../web_chat/Dockerfile), [chat lock](../../web_chat/frontend/package-lock.json), [QA Dockerfile](../../qa_portal/Dockerfile), [QA lock](../../qa_portal/frontend/package-lock.json), [QA requirements](../../qa_portal/backend/requirements.txt) |
| Host | Linux or a future Windows/WSL path, Docker Engine/Desktop, Compose, Git and Python have their own compatibility; upgrading them may also affect other stacks on the host | [B06 proposal](b06-clean-install-proposal.md) |

Conclusion from the sources: for some components even a rebuild of the same commit may resolve different packages. Before issuing updates we need a record of the actual built artifacts and dependencies.

## 3. Indicative effort

This is a rough planning estimate of the preparation and verification of one bundle, not a measurement or a downtime. The size of the data, the number of skipped versions and breaking changes will change the estimate.

| Kind of change | Indicative work | Main reason |
|---|---|---|
| Minor update of nginx or cloudflared | hours to 1–2 working days | configuration, routes, proxy trust, the real tunnel and rollback |
| Libraries of API, chat, QA portal or search | 1–3 working days per related bundle; more if the public interface changes | build, API contracts, UI and search behaviour |
| Langflow or Bifrost | several working days up to weeks for a large jump | flows, custom components, providers, API and possible migrations of their state DBs |
| PostgreSQL major version and database extensions | several working days up to weeks | extension compatibility, data/index migration, restore and a measured interruption |
| Embedding model, dimension, stemming or normalization | a separate estimate after a proposal | recomputing indexes/vectors, parity and a new measurement of retrieval results |

The first introduction of a supported update process is separate work on top of these estimates. Regular maintenance of smaller changes should reduce future large jumps.
PostgreSQL distinguishes minor updates that keep the data format from major upgrades that require a migration procedure; see the [official documentation](https://www.postgresql.org/docs/current/upgrading.html).

## 4. Proposed release rules

- Every release has an immutable Git SHA, registry image digests for the supported platform, the actual package versions, the schema version and compatible versions of the host tools. Generate the record from the build, tie it to B03; do not edit existing generated manifests by hand.
- For components with loose dependencies, add reproducible locking of the resulting versions. Complement digest pinning with a plan for renewing them regularly, so that they do not freeze security fixes.
- Prepare component updates in a separate branch or a copy from `clean_init`, with small related changes. Release only a jointly tested result.
- Define precisely the supported release transition A → B. Skipping other releases is not automatically considered supported.
- Preserve the supplied configuration, passwords, API keys and `LANGFLOW_SECRET_KEY`. An update must not silently regenerate them.
- Changes to models, dimensions, the database schema/indexes and pipeline precedence require separate approval under AGENTS.md. A package change must not introduce them covertly.
- Checking for new versions and security fixes may later be automated. Deployment takes place in a controlled window; this step sets up no automation.

## 5. Procedure for one upgrade

1. **Inventory:** determine the exact current artifacts and the state of the data, read the release notes of the components and of all skipped major versions. Determine the target release and the supported pair A → B.
2. **Plan:** list the affected services, configuration changes, required migrations, disk space, backup, the expected interruption and rollback. Show which checks cannot be verified yet.
3. **Rehearsal:** on a dedicated isolated installation with synthetic data, verify the actual transition, preservation of configuration/keys, repeated runs and failure in every irreversible phase.
4. **Acceptance:** perform the relevant builds, API/UI smoke, scope and retrieval checks, flows and decryption of credential variables. On a normalization change, parity before/after; on a retrieval behaviour change, a regression with `--repeat`. A paid call only after an explicit instruction with a limit.
5. **Backup and restore:** keep the corresponding DB/data, configuration and keys, images and metadata of the old release. Store the keys protected; keep the escrow copy separate from the DB backup. Actually rehearse the restore in the lab.
6. **Deployment:** after checking the current state, perform only the approved plan and only the affected services. Preserve other stacks, the network layout and the Cloudflare configuration; changes to these areas belong in a specific plan, not among the side effects of an upgrade.
7. **Check and record:** functional verification and monitoring, a record of versions/hashes and results, an open rollback window. Delete old artifacts only after the window is closed separately.

Rolling back the application image is not automatically a rollback of the data: a new Langflow, Bifrost or DB may change the schema or format of its state.
Where backward compatibility is not proven, rollback means restoring the corresponding backup; the plan must determine how writes made after the upgrade are handled.
No general `down -v`, `prune` or regeneration of `.env` belongs in the update procedure. The current migration runner stays only for isolated synthetic DBs.

## 6. Future tool and division of work

Proposed functions: showing versions, a read-only transition plan, preflight, backup/record, execution of a specific transition, verify and rollback by data type.
These functions are not implemented today; no runnable school update command is given. The Windows entry is later to use the same proven procedure via WSL, with its own verification of Docker Desktop and the file system.

- **B06-U1:** inventory and release compatibility proposal; can be prepared in parallel with completing the first installation, from code and public sources only.
- **B06-U2:** generated release description and dependency locking, tied to B03; prototype/tests in isolation.
- **B06-U3:** the first specific transition A → B, lab migration and restore; before school/production use, resolve the open migration support B03e-2/D11.
- **B06-U4:** a common update entry point and an operations runbook, including Windows/WSL; done only after a rehearsal of a real transition and rollback.

Acceptance of the updater requires a proven transition and rollback, preserved keys and data, safe refusal of an unsupported starting version,
truthful results of unverified checks and documentation of the exact supported environment. A successful clean install does not replace this acceptance.

## 7. Public sources

- [PostgreSQL: upgrading a cluster](https://www.postgresql.org/docs/current/upgrading.html) — the minor/major difference and migration methods.
- [Docker: Compose in production](https://docs.docker.com/compose/how-tos/production/) — build and replacement of the affected services.
- [Langflow: deployment on Docker](https://docs.langflow.org/deployment-docker) — the official path to versioning, upgrade and rollback guidance; the current public page is not a contract of our image 1.10.1.

Before executing a specific upgrade, re-check the official sources for the exact starting and target versions. This proposal does not determine the latest version of every component and chooses none for deployment.
