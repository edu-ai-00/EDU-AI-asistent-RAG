# edu_ai — Onboarding: cloning and bringing up the whole stack

> **Scope:** this page is the **local development** onboarding (clone, bring the stack up, understand each step).
> To install on a fresh Ubuntu host use [docs/CLEAN_INSTALL.md](docs/CLEAN_INSTALL.md) (`scripts/setup.py`); operating checks and
> requirements are in [docs/OPERATIONS_LINUX.md](docs/OPERATIONS_LINUX.md). Neither is a supported production upgrade procedure.
> Tests and the definition of done: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md). Release and packaging rules: [docs/RELEASING.md](docs/RELEASING.md).

> **Status: development walk-through, last revised 2026-07-25, partly behind the code.** Known differences (audit of
> c8fe0543): `scripts/bootstrap.py` alone does **not** finish on a clean clone, because `docker-compose.yml` requires
> `EDU_AI_TUNNEL_SUBNET` (set by `host_preflight.py pin-env --allocate-tunnel-subnet`, which the `setup.py` wizard runs)
> and `.env.example` leaves `CHAT_LEGAL_ENTITY` as `CHANGE_ME…`, so the smoke test reports the chat as not configured.
> The LLM provider and its key are configured in the **Bifrost** UI, not through `OPENAI_API_KEY` in `.env`
> ([CLEAN_INSTALL.md](docs/CLEAN_INSTALL.md)). The stack has 9 services (plus optional `cloudflared`); numbers such as
> "7 containers", "24 tables" and "seven paths" below are older snapshots. Section 6 describes three different flow-import
> behaviours; read it before re-running the import.

Goal: from a clean clone to a fully functional local environment — 7 Docker containers (at the time of writing; today 9 services:
nginx, FastAPI, Langflow, Bifrost, Postgres+pgvector, pgAdmin, search, web chat, QA portal) with imported
Langflow flows and correctly configured keys and IDs.

Estimated time: 30–45 minutes (most of it = docker build).

---

## 0) Prerequisites

- **Docker Desktop** (macOS/Windows) or Docker Engine + compose plugin (Linux)
- **git**, **python3**
- An LLM provider key for the **paid** features (embeddings, generated answers), configured in the Bifrost UI. Curated QA
  exact matches work without one.
- The repository: `github.com/edu-ai-00/EDU-AI-asistent-RAG` (public source)

---

# Optional Cloudflare Tunnel

Cloudflare is optional. The default Compose selection starts the local stack without
`cloudflared`; no Cloudflare account or token is required. To enable the connector,
set `COMPOSE_PROFILES=cloudflare` and `CLOUDFLARE_TUNNEL_TOKEN` in the installation's
`.env`, then use the same bootstrap/Compose project and configuration paths. A token
alone does not enable it. Bootstrap refuses an enabled connector with a missing or
placeholder token before starting services. Smoke checks every enabled service;
an absent or stopped enabled connector is a failure.

Configure the tunnel routes and Cloudflare Access separately before publishing the
Bifrost UI: Bifrost inference has no authentication by design (its UI is behind the
admin login from `BIFROST_ADMIN_USERNAME` / `BIFROST_ADMIN_PASSWORD`), and the tunnel
itself does not authenticate users.
The local R2 subnet/trust preflight remains required even when the connector is off;
its private network protects the nginx trust boundary and is allocated locally.

For an existing installation that uses Cloudflare, preserve its enabled state by
setting the profile explicitly before adopting this Compose change. Removing the
profile does not stop an already running connector. To deliberately disable it,
stop and remove only its `cloudflared` container in the same verified project;
smoke still flags leftover broken containers. Preserve all other services and data.

# Current entry point

For a fresh isolated Ubuntu host use [CLEAN_INSTALL.md](docs/CLEAN_INSTALL.md):
prepare the host, re-login for Docker access, then run `scripts/setup.py install`
from its clean pinned checkout. The wizard supplies the tunnel subnet and school
picker configuration before bootstrap. No paid provider is needed for its smoke checks.

This does not claim a validated one-command Windows/macOS development setup.
The detailed development walkthrough below is historical and needs its stated
configuration prerequisites. `bootstrap.py` alone is not the current clean-install entry point.

# The long way, step by step

Useful when bootstrap fails at some step, or when you want to understand what it did.
Sections follow the order of the original bootstrap; the current `bootstrap.py` steps differ in numbering and add the Langflow connectivity variables.

## 1) Clone

```bash
git clone https://github.com/edu-ai-00/EDU-AI-asistent-RAG.git
cd EDU-AI-asistent-RAG
```

## 2) Environment configuration — `.env` (phase 1)

```bash
cp .env.example .env
```

One file: `docker-compose.yml` uses it both as `env_file` (values passed into the
containers) and for `${POSTGRES_*}` interpolation in compose itself. It's gitignored.

For now fill in **only these** in `.env` (the rest comes in steps 5–6):

| Variable | Value |
|---|---|
| `COMPOSE_PROJECT_NAME` | leave as `edu_ai` — see the warning below |
| `POSTGRES_PASSWORD` | choose a password (the password of the database role `edu_ai_ingest`) |
| `POSTGRES_ADMIN_PASSWORD`, `POSTGRES_SEARCH_PASSWORD`, `POSTGRES_LANGFLOW_PASSWORD`, `POSTGRES_MIGRATOR_PASSWORD` | four more database role passwords, all different (`install.py` and `bootstrap.py` generate them; see `docs/plans/b06-c2-role-contract.md`) |
| `PGADMIN_DEFAULT_EMAIL` / `_PASSWORD` | pgAdmin login |
| `BIFROST_ADMIN_USERNAME` / `_PASSWORD` | Bifrost UI login (default user `admin@edu-ai.eu`); bootstrap generates the password and `BIFROST_SETUP_TOKEN` |
| `LANGFLOW_SUPERUSER` / `_PASSWORD` | Langflow admin account — created automatically on first start |
| `LANGFLOW_SECRET_KEY` | **must be a Fernet key**, see below |
| `OPENAI_API_KEY` | your OpenAI key |

Leave `LANGFLOW_API_KEY` at `CHANGE_ME…` for now. `LANGFLOW_FLOW_ID*` are not in
`.env` at all — they're fixed endpoint aliases in `docker-compose.yml`.

Passwords must not contain `$` — Compose reads it inside `.env` as the start of a
variable reference and silently truncates the value. `python3 -c "import secrets;
print(secrets.token_urlsafe(24))"` gives a safe one.

`LANGFLOW_SECRET_KEY` is **not** an ordinary password: Langflow passes it straight to
Fernet, which requires exactly 32 random bytes in url-safe base64 (44 characters ending
in `=`). Anything else fails with `Fernet key must be 32 url-safe base64-encoded bytes`.

```bash
python3 -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Set it **before the first start** and never change it afterwards — it encrypts the
Langflow Global Variables, and changing it makes the existing ones undecryptable.

> ⚠️ **`COMPOSE_PROJECT_NAME`** — do not remove it. Without it Compose derives the
> project name from the *directory* name, so a clone into a differently named folder
> silently addresses a different project: `docker compose down` then exits 0 having
> stopped nothing. It is also the prefix of the `postgres_data` volume, so changing
> the value points the stack at a different, empty database.
> Check what is actually running with `docker compose ls`, not just that a command
> exited without error.

> 📌 **How env is loaded:** values from `.env` are propagated into the containers
> at container **CREATION**, not on restart. So after each env change
> run `docker compose up -d` (it recreates the changed services) — a mere
> `docker restart` is not enough.

## 3) The perimeter — nothing here authenticates a caller

**nginx checks no credentials at all.** There is nothing to create in this step;
it exists so that nobody mistakes that for an oversight. Until 2026-09-02 `/api/`
had Basic auth and `/search/` an API key, both mounted from gitignored files.
Both are gone: the key lived inside a shipped mobile binary, where it was
extractable and therefore not a secret, and it cost months of `401`s to a
`Bearer ` prefix drifting between the map and the docs.

Two things hold the perimeter instead, and **neither is in this repository**:

| Door | Held by | Where it is configured |
|---|---|---|
| `rag.edu-ai.eu` → nginx `:81` (tunnel) | Cloudflare Access policy | Cloudflare Zero Trust dashboard |
| `:80`, `:8443`, `:8444` on the public IP | AWS security group | AWS console |

Both are one click away from being wide open and nothing in the stack would
notice, so:

```bash
# From OFF-SITE — not on the VPN, or it proves nothing (the script checks).
python3 scripts/perimeter_check.py --strict --ip <public IPv4>
```

Run it on a schedule from somewhere outside. It verifies that the tunnel refuses
an unauthenticated caller, that Cloudflare Access is what refuses it, and that
the admin ports do not answer from the internet. **That schedule is now a
security control, not a nicety** — treat a red run as an incident.

What nginx still does decide is *which endpoints exist*: the `$search_path_allowed`
map in `nginx/conf/nginx.conf` publishes seven paths and answers `404` for the rest.
It is not authentication, and it is the only thing keeping `/v1/query-log`
(every question ever asked) and `/v1/staff-users` off the edge. Add a line there
and you publish that endpoint to everyone past the perimeter.

Inside the VPN everything on `:80` is open, `/api/` included — and `/api/`
**writes**: it ingests documents and has no login of its own, unlike pgAdmin and
Langflow, which keep theirs. Any WireGuard peer can post to it.

## 4) Build and first start

```bash
docker compose build      # ~10–20 min the first time (the Langflow image is large)
docker compose up -d
docker compose ps
```

> **`build` before `up`, and not only on a first clone.** The postgres image carries
> the ParadeDB `pg_search` extension; compose starts postgres with
> `shared_preload_libraries=pg_search` and `init_clean` does `CREATE EXTENSION`, so an
> image built before `Dockerfile.postgres` gained that `.deb` fails on both counts.
> `up` alone reuses whatever image is already present, which is how a machine that
> once ran an older commit gets a broken first start. Preflight in `bootstrap.py`
> blocks this, because the failure is sticky: init runs only on an empty volume.

What happens automatically:
- **Postgres** on first start (empty volume) runs `postgres/init_clean/*.sql`, the
  consolidated baseline: the `langflow` database, then the schema (24 tables + the
  `qa_conversation_v` view, `vector` + `pg_trgm` + `unaccent`, Czech locale
  `cs_CZ.UTF-8`), then the config seed — **four active languages (cs, en, de, es)**,
  one `search_setting`, and the code lists. No documents and no Q&A data; content
  arrives through the ingestion API.
  > Runs **only on an empty volume**. `postgres/init/` holds the numbered migration
  > history and is no longer executed — on a database that already has data, apply
  > the relevant `NN_*.sql` by hand.
  > ⚠️ The German and Spanish stopwords, blocklist and self-harm helplines in the
  > seed are **generated**, not reviewed by a native speaker. See the header of
  > `02_seed_config.sql` before relying on them in production.
- **Langflow** builds its schema in the `langflow` database itself via automatic
  Alembic migrations (official behavior) and creates the superuser account
  from `LANGFLOW_SUPERUSER*`. Auth is enforced by `LANGFLOW_AUTO_LOGIN=false`
  in compose.
- **`edu_ai_api` starts and stays up even without `LANGFLOW_API_KEY`.** It no longer
  crash-loops; it reports the gap instead:
  `curl -s localhost:8080/health` → `{"status":"degraded","missing_config":["LANGFLOW_API_KEY"]}`.
  Step 5 fills it in.

Because every service now has a healthcheck and `depends_on: condition: service_healthy`,
`docker compose up -d --wait` is a real readiness gate — it returns when the stack is
actually usable, so nothing downstream needs a `sleep`.

> A *healthy* container is not the same as a *configured* one. The healthcheck asks
> "does this process answer?", which a degraded instance does — deliberately, so it
> stays inspectable. Use `scripts/smoke.py` to ask the other question.

## 5) Langflow API key

```bash
python3 scripts/create_api_key.py
```

Asks Langflow about the key already in `.env` first; the answer decides:

- **accepted** → it says so and changes nothing;
- **rejected** (HTTP 401) or **no key yet** → logs in as the superuser from `.env` over
  REST, creates a new key, checks that Langflow accepts it and writes it into `.env`
  (atomically). If a step before the write fails, `.env` and the existing keys are
  untouched, the new key is deleted again, and it falls back to asking you to paste one.
  The previous key is **never deleted**: a masked listing cannot identify it reliably.
  Remove it in Langflow (Settings → API Keys) if it is unused;
- **cannot be verified** (timeout, 5xx, a redirect, an odd answer, **HTTP 403**) → changes
  nothing, asks nothing and exits with status `3`. A key that cannot be asked about is not
  shown to be dead, and a 403 can be a proxy or a lack of rights; run it again when Langflow
  answers normally, or use `--force` if you know the key is dead. **Langflow 1.10.1 answers 403
  (not 401) to an unknown key** (verified in a lab), so a key that died with `docker compose
  down -v` shows up here too: run `python3 scripts/create_api_key.py --force`;
- the write of `.env` fails in a way that leaves it unclear whether `.env` holds the new key
  (readback failure, interrupt right after the rename) → exits with status `1`, deletes
  nothing and says what is known. Check `LANGFLOW_API_KEY` in `.env` and run it again.

`--force` is your explicit choice to skip the check and create a new key (the previous key
is kept). Calls that carry the key never follow an HTTP redirect.

> **Why not the CLI:** `docker compose exec edu_ai langflow api-key` is the documented
> route, and it does not work here — with `LANGFLOW_AUTO_LOGIN=false` (our real-auth
> switch) it answers *"Auto login is disabled. API keys cannot be created through the
> CLI."* The REST endpoints the UI itself uses have no such restriction.

Then `docker compose up -d` to recreate the API with the new value.

Manually, if you prefer the browser: open **http://127.0.0.1:7860**, log in with the
superuser from `.env`, **Settings → API Keys → Add New**, and put the `sk-…` value into
`.env` as `LANGFLOW_API_KEY`.

## 6) Import flows

> Each flow JSON carries a fixed `endpoint_name` alias (e.g. `edu-ai-load-pdf`).
> Callers — FastAPI and the extraction flows' loopback self-call into the
> markdown sub-flow — address flows by that alias (set as `LANGFLOW_FLOW_ID*`
> constants in `docker-compose.yml`), never by the UUID Langflow assigns on
> import. Run the script:

```bash
export LANGFLOW_API_KEY=sk-…        # key from step 5
python3 scripts/import_flows.py
```

The script imports the **6 ingestion flows** (MARKDOWN, PDF, OFFICE, WEB, QA,
EXPORT_QA_PACK). Re-running it creates no duplicates, but it is **not** harmless: a flow whose `endpoint_name`
already exists is PATCHed in place, i.e. overwritten with the repository version, and edits made in the Langflow UI are lost.
`bootstrap.py` itself calls `import_flows.py --missing-only` (creates only missing aliases, keeps existing flows), and
`scripts/flow_rollout.py` is the targeted A → B tool for an installed Langflow (refuses flows that drifted). Nothing to paste
into `.env`: run the script, done.

> Retrieval no longer runs through Langflow (the old search/agent flows are gone) —
> queries are handled by the `edu_ai_search` microservice.

## 7) Keys for flow components — Global Variables (automatic)

Flows take their credentials from **Langflow Global Variables** — they reference them
only by name (`load_from_db`), so no secret lives in any flow JSON.

**They are created automatically — there is nothing to do in this step.** Compose sets
`LANGFLOW_VARIABLES_TO_GET_FROM_ENVIRONMENT=OPENAI_API_KEY,POSTGRES_PASSWORD`, and on
start Langflow creates both variables from the values in `.env`. Verified on a fresh
instance (2026-07-25): both rows appear within a second of Langflow starting, before
any human could have opened the UI.

If you want to confirm, **Settings → Global Variables** should list `OPENAI_API_KEY`
and `POSTGRES_PASSWORD` as type **Credential**. (If they are absent — an older Langflow,
say — create them by hand: Add New, type Credential, values from `.env`.)

The components (`PostgresInsert*`, search components,
`TableMetadataBusinessValidator`) fetch the values themselves at runtime.

Nothing else needs configuring:
- **Extraction flows** (PDF/OFFICE/WEB) read `LANGFLOW_API_KEY` and the target
  `LANGFLOW_FLOW_ID` from the container environment; PDF/OFFICE/WEB call the markdown
  ingestion flow via `APIRequest` (header `x-api-key` from the env var `LANGFLOW_API_KEY`).

## 8) Verifying everything runs

| Service | URL |
|---|---|
| Langflow UI | http://127.0.0.1:7860 (direct) · http://localhost:8444 (via nginx) |
| API — test form | http://127.0.0.1:8080/ |
| API — Swagger | http://127.0.0.1:8080/docs |
| API — uploaded files | http://127.0.0.1:8080/files |
| pgAdmin | http://localhost:8443/pgadmin/ |
| Web chat | http://localhost/chat/ |

**Automated smoke test** — checks container health, every service's `/health`, that
Bifrost leaves inference open to the internal callers and has its admin login, all 6
flow aliases, the database schema, and a dry-run upload. No OpenAI call, so it costs
nothing and is safe to run from cron:

```bash
python3 scripts/smoke.py        # exit 0 = everything that ran passed
```

Checks it cannot run (an unpublished port, a missing key) are reported as `[skip]`, not
counted as passes — a green run never means "nothing was tested".

**Manual smoke test:**
1. At http://127.0.0.1:8080/ upload a small PDF, fill in `user_name`,
   leave **Dry run checked** → verifies the upload without Langflow.
2. Then again **without dry run** → the whole pipeline runs
   (extraction → markdown → ingestion → Postgres).
3. Check: a `.md` appeared under `/files`; in pgAdmin
   `SELECT count(*) FROM edu_ai.edu_ai_document_chunks;` returns > 0.

## 9) Overview: where each key / ID lives

| Key / ID | Where it is set | Who reads it |
|---|---|---|
| `POSTGRES_PASSWORD` | `.env` | role `edu_ai_ingest`: the Postgres components in flows (auto via Global Variables, step 7) |
| `POSTGRES_ADMIN_PASSWORD` | `.env` | the `postgres` container (maintenance superuser `edu_ai_admin`); no application receives it |
| `POSTGRES_SEARCH_PASSWORD` / `POSTGRES_LANGFLOW_PASSWORD` / `POSTGRES_MIGRATOR_PASSWORD` | `.env` | search service / Langflow state database / maintenance migrator |
| `LANGFLOW_SUPERUSER*` | `.env` | Langflow on first start (UI login) |
| `LANGFLOW_API_KEY` | `scripts/create_api_key.py` → `.env` (step 5) | FastAPI (calls flows), extraction flows (header component reads env) |
| `LANGFLOW_SECRET_KEY` | `.env` (Fernet key, set once) | Langflow — encrypts the Global Variables |
| `OPENAI_API_KEY` | `.env` | Langflow components via Global Variables (step 7) |
| `LANGFLOW_FLOW_ID_PDF/_OFFICE/_WEB/_QA` | constants in `docker-compose.yml` | FastAPI — which flow to run based on file type (`.pdf`/`.json`…) |
| `LANGFLOW_FLOW_ID` | constant in `docker-compose.yml` | extraction flows — the target markdown ingestion flow |
| Component IDs inside a flow (`APIPipelineInput-tfiG6`) | — | preserved on import, never change them manually |

**Golden rule:** the `LANGFLOW_FLOW_ID*` values are `endpoint_name` aliases
defined in the flow JSONs themselves (`langflow_flows/ingestion/*.json`), not
per-instance UUIDs — they don't change between imports or environments, so
they live as plain constants in `docker-compose.yml`.

## 10) Troubleshooting

| Problem | Cause / fix |
|---|---|
| something is wrong but I don't know what | `python3 scripts/smoke.py` — it names the failing check |
| `edu_ai_api` reports `degraded` | `curl localhost:8080/health` lists the missing variables → fill them in `.env`, `docker compose up -d` |
| `perimeter_check.py` says a port ANSWERED from the internet | the AWS security group has been widened — close it first, then ask why. nginx authenticates nobody, so that port is genuinely open (step 3) |
| `perimeter_check.py` exits 2 | it ran from inside the perimeter and refused to give a verdict — run it off-site, not on the VPN |
| `docker compose down` did nothing, exit 0 | wrong project — `COMPOSE_PROJECT_NAME` missing from `.env`, so the name came from the directory. Check `docker compose ls` |
| `Fernet key must be 32 url-safe base64-encoded bytes` | `LANGFLOW_SECRET_KEY` is an ordinary password → regenerate it as a Fernet key (step 2) |
| `langflow api-key` says *auto login is disabled* | expected with `LANGFLOW_AUTO_LOGIN=false` → `python3 scripts/create_api_key.py` |
| a change to `.env` didn't take effect | env is read at container CREATE → `docker compose up -d`, not restart |
| Postgres: `password authentication failed` | `.env` missing or empty `POSTGRES_PASSWORD`, or the password contains `$` → step 2 |
| a Langflow flow "reverted to an old version" | a stale browser tab overwrote the newer state — **close old tabs with the flow** |
| the browser forces HTTPS on `localhost:8443/8444` and nothing loads | a stale HSTS entry from before nginx served plain HTTP — clear it in the browser (Chrome: `chrome://net-internals/#hsts`, *Delete domain security policies* → `localhost`) |
| `ModuleNotFoundError: markitdown` | stale image → `docker compose build edu_ai && docker compose up -d` |
| Postgres won't start, or init stopped at `extension "pg_search" is not available` | the `edu_ai_postgres` image predates `Dockerfile.postgres` gaining the ParadeDB `.deb`, and `up` reuses an image it already has → `docker compose build postgres`. **The half-initialised volume must go too** (`docker compose down -v`): init runs only on an empty volume, so a retry otherwise skips the baseline and leaves a partial schema that looks fine. `bootstrap.py` refuses up front on this — `--build` is opt-in, which is exactly how the stale image survives |
| I can't reach the ports from another machine | intentional — the dev ports are bound to `127.0.0.1` (see `docker-compose.override.yml`) |
| I want a completely clean DB restart | `docker compose down -v` (⚠️ deletes data!) → the schema is recreated on the next start |

---

*Updated: 2026-07-25 · Questions → Martin (info@edu-ai.eu)*
