# edu_ai — Onboarding: cloning and bringing up the whole stack

Goal: from a clean clone to a fully functional local environment — 7 Docker containers
(nginx, FastAPI, Langflow, Postgres+pgvector, pgAdmin, search, web chat) with imported
Langflow flows and correctly configured keys and IDs.

Estimated time: 30–45 minutes (most of it = docker build).

---

## 0) Prerequisites

- **Docker Desktop** (macOS/Windows) or Docker Engine + compose plugin (Linux)
- **git**, **python3**
- **Your own OpenAI API key** (https://platform.openai.com/api-keys)
- Access to the `github.com/<ORG>/edu_ai` repo (collaborator invitation)

---

# The short way

```bash
git clone https://github.com/<ORG>/edu_ai.git
cd edu_ai
python3 scripts/bootstrap.py
```

That is the whole first deployment. It generates `.env` with strong passwords, creates
the nginx auth files, brings the stack up waiting for each service
to report *healthy*, creates the Langflow API key over REST, imports the 6 flows, runs
a smoke test, and prints the URLs and credentials at the end.

Every step is idempotent and prints `[ok]` / `[skip]` / `[fail]`; an existing file is
never overwritten. Re-run it whenever something is missing — after a `git pull` that
adds a variable to `.env.example`, for example.

Then do the one thing it cannot do for you:

```bash
# put your real OpenAI key in .env — it cannot be generated
#   OPENAI_API_KEY=sk-...
docker compose up -d          # recreate, so the containers see the new value
curl -s localhost:8080/health # -> {"status":"ok", "missing_config":[]}
```

Useful flags: `--build` (rebuild images after a local code change), `--no-input`
(never prompt — for CI), `--skip-smoke`, `--langflow-url` (when port 7860 is not
published on the host).

To check a running stack at any time:

```bash
python3 scripts/smoke.py      # no OpenAI cost — safe to run from cron
```

---

# The long way, step by step

Useful when bootstrap fails at some step, or when you want to understand what it did.
Each section matches one bootstrap step.

## 1) Clone

```bash
git clone https://github.com/<ORG>/edu_ai.git
cd edu_ai
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
| `POSTGRES_PASSWORD` | choose a password |
| `PGADMIN_DEFAULT_EMAIL` / `_PASSWORD` | pgAdmin login |
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

## 3) Auth for the public API (nginx, outside git)

nginx protects `/api/` (Basic auth) and `/search/` (API-key). Both files are
**gitignored** — create them from the `.example` templates:

> ⚠️ **If they are missing, nginx crash-loops with a misleading error.** A bind mount
> whose source does not exist on the host is created by Docker as an empty *directory*,
> and nginx then dies with `pread() "/etc/nginx/auth_keys.conf" failed (21: Is a
> directory)` — which reads like a bug in `nginx.conf`. The nginx service now runs a
> preflight check that says what is actually wrong instead.
>
> Creating the file afterwards is not enough on its own: a container keeps the mount it
> was created with, so a plain restart changes nothing. You need
> `docker compose up -d --force-recreate nginx`. `scripts/bootstrap.py` detects the
> stray directory, replaces it with a real file and recreates the container for you.

```bash
# /api/ — Basic auth (admin/content ops): create a user + password
printf 'admin:%s\n' "$(openssl passwd -apr1 'ZVOL_SI_HESLO')" > nginx/.htpasswd

# /search/ — API key for the mobile app: pick a strong key
cp nginx/auth_keys.conf.example nginx/auth_keys.conf
# and in the file replace CHANGE_ME_search_api_key with the real key
```

The app then calls `/search/` with the header `Authorization: Bearer <key>`.
`/api/` (upload form) requires Basic auth (`curl -u admin:password …`).
You rotate the key by adding a new line to the `map` in `auth_keys.conf` (then delete the old one).

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
  one `search_setting`, and the číselníky. No documents and no Q&A data; content
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

Logs in as the superuser from `.env` over REST, creates the key, and writes it into
`.env`. If the key already there works, it says so and changes nothing; if anything
fails it falls back to asking you to paste one.

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
EXPORT_QA_PACK). Re-running it is idempotent — a flow whose `endpoint_name`
already exists gets PATCHed in place instead of duplicated. Nothing to paste
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

**Automated smoke test** — checks container health, every service's `/health`, all 6
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
| `POSTGRES_PASSWORD` | `.env` | Postgres, pgAdmin; the Postgres components in flows (auto via Global Variables, step 7) |
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
| nginx: `pread() … (21: Is a directory)` | `nginx/.htpasswd` or `auth_keys.conf` missing → `python3 scripts/bootstrap.py`, then `docker compose up -d --force-recreate nginx` (a plain restart keeps the old mount) |
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
