# Clean deployment on a new Ubuntu VM

The procedure is for a new isolated Linux host with no other stack. The source is a
Git checkout of the exact approved public commit; the installer does not accept a
ZIP without `.git`. Verification of a specific release must state its SHA. Offline
tests are not a VM acceptance.

## Quick path from a clean VM

Log in over SSH as a regular user with `sudo`. The following procedure uses a
public HTTPS clone, a new source directory and a separate config/state. Run the
blocks one at a time; on an error, resolve its cause first.

1. **Git and the exact source.**

   ```bash
   sudo apt-get update
   sudo apt-get install -y git ca-certificates openssh-client
   umask 022
   mkdir -p "$HOME/edu-ai"
   git clone --single-branch --branch main https://github.com/edu-ai-00/EDU-AI-asistent-RAG.git "$HOME/edu-ai/src"
   cd "$HOME/edu-ai/src"
   CANDIDATE_SHA=$(git rev-parse HEAD)
   git checkout --detach "$CANDIDATE_SHA"
   git rev-parse HEAD
   git status --short
   ```

   The source directory must be new and the tree clean. Record the full SHA for
   tests and handover. `umask 022` creates a new parent directory with permissions
   0755; an existing parent is not changed by it, and the preflight rejects its
   unsuitable permissions. Do not create `.env` or the config in advance, and do
   not copy secrets and runtime from another installation.
2. **Prepare the host and log in again.**

   ```bash
   bash scripts/prepare_ubuntu.sh --check
   bash scripts/prepare_ubuntu.sh --apply
   ```

   The script adds Python, Docker Engine, Compose, Buildx and other tools. After
   being added to the `docker` group, log out and log in over SSH again; Docker
   must work without `sudo`. If it stops at `apt-get` (for example "Release file
   ... is not valid yet": a wrong clock, typical right after restoring a VM
   snapshot), fix the cause and run it again; it reuses the Docker repository
   files it wrote itself and refuses any other.
3. **Run the wizard.** After logging in again:

   ```bash
   cd "$HOME/edu-ai/src"
   python3 scripts/setup.py install
   ```

   Confirm the plan with Enter. The wizard prepares the configuration
   `~/edu-ai/config`, the state `~/edu-ai/state`, the project `edu_ai`, the
   bootstrap with smoke and the roles check. `NETWORK_POOLS` may be accepted with
   the word `yes` only as the single unverified finding on an isolated VM. Any
   other problem stops the installation. Optional read-only overview:
   `python3 scripts/setup.py plan`.
4. **Open the chat and verify the result.** Require `BACKEND_READY: yes`, roles
   `MATCH` and a smoke without FAIL. Open the printed address
   `http://<VM address>/chat/` and select a school; sending is disabled until a
   selection is made. Verify a reload and a school change with the conversation
   cleared. Eight services should be healthy, pgAdmin running without a
   healthcheck. The clean seed has no QA or document corpus.

QA exact can answer without a paid LLM after a QA import. Embeddings and generated
answers require a provider and a key in Bifrost. The installation and the answer
quality are separate checks. [Scope of the documented runs](releases/public-source.md).

## Network and configuration

The default Compose publishes ports 80, 8443 and 8444. Restrict access at the
network perimeter before starting. The wizard does not check the firewall or the
isolation and does not enable Cloudflare. `NETWORK_POOLS` remains unverified: the
automatic Docker networks are not measured against the LAN/VPN. On an isolated VM,
only this single finding may be accepted by answering `yes`. Any other rejection
or unverified finding must stop the installation.

Non-interactive equivalent for an operator who explicitly accepts this limitation:

```bash
python3 scripts/setup.py install --no-input --accept-unmeasured-network-pools
```

A new configuration gets `CHAT_ENTITY_PICKER=true`; `CHAT_LEGAL_ENTITY` stays
unset. The school selection is not authentication. The backend still checks the
entity, roles and visibility. The wizard does not override a fixed school or an
existing picker choice. An unset or invalid school registry has its own state; the
first school is not chosen automatically.

## Failure and retry

A non-zero return of the bootstrap is an error. Do not bypass the smoke. The
wizard deletes and reverts nothing; it allows a retry only for its own
configuration (the same checkout, keys, project and Compose model). Another
commit of the same checkout is accepted (the tool does not compare history, so a
later and an earlier commit are treated alike); a configuration created from
another clone, or by a checkout that has since been moved or renamed (a release
directory switched through a symlink counts as moved), is refused
(`CONFIG_FROM_ANOTHER_CHECKOUT`): the checkout path is part of the installation.
A configuration created before the record named its checkout (`source_root`)
keeps the older rule and continues only on the same commit
(`CONFIG_FROM_ANOTHER_SOURCE`); install such a host cleanly. Keys the commit's
`.env.example` has and the `.env` lacks are appended (`CONFIG_KEYS_ADDED`; secrets
the installer generates are generated, others are copied from the template line)
and existing values are never changed. It verifies the local Docker
engine and the ownership of containers, mounts, networks and volumes. It rejects a
foreign configuration, a HEAD that changes during the run, a dirty tree or Compose overrides. The keys
are not generated again on a retry.

For diagnostics after a successful run:

```bash
python3 scripts/smoke.py --source-root "$HOME/edu-ai/src" --config-dir "$HOME/edu-ai/config" --state-dir "$HOME/edu-ai/state" --project edu_ai
python3 scripts/db_roles.py verify --container edu_ai_postgres
docker compose --project-directory "$HOME/edu-ai/src" --env-file "$HOME/edu-ai/config/.env" -f "$HOME/edu-ai/src/docker-compose.yml" -p edu_ai ps
git rev-parse HEAD
```

The LLM provider is configured separately in Bifrost; a key in the client `.env`
does not configure the provider by itself. The Bifrost admin login exists from the
first start: `bifrost/config.json` creates it from `BIFROST_ADMIN_USERNAME`
(default `admin@edu-ai.eu`, the same as pgAdmin) and `BIFROST_ADMIN_PASSWORD` (generated) in `~/edu-ai/config/.env`.
Open the UI over an SSH tunnel to `127.0.0.1:8099` and log in; add the provider
and its key there. To change the login, edit `BIFROST_ADMIN_USERNAME` or
`BIFROST_ADMIN_PASSWORD` in the `.env` and recreate the container (a plain
`restart` keeps the values it was created with; without a password Bifrost does
not start):

```bash
docker compose --project-directory "$HOME/edu-ai/src" --env-file "$HOME/edu-ai/config/.env" -f "$HOME/edu-ai/src/docker-compose.yml" -p edu_ai up -d bifrost
```

In the UI's Security Settings
keep **"Enforce Virtual Keys on Inference" off**: with it on, Langflow, search and
the API get `401 virtual key is required`. The red warning below it is expected:
inference is open on purpose and reachable only from the internal network, the
loopback port and an Access-protected tunnel. "Allow Direct API Keys" stays off
(`bifrost/config.json` sets both on a new installation). A setting saved in the UI
takes precedence: Bifrost does not overwrite it from `config.json` at the next
start, so a switch turned on by hand stays on until it is turned off by hand. The smoke test fails in that state
(`bifrost: inference requires a credential`) and when the admin login is missing;
an installation that wants inference auth on is outside this design and does not
pass the smoke. Inference stays unauthenticated for the internal callers by design.
The seeded settings also use the Jina reranker as a prefilter; it is called by
search directly, not through Bifrost, and needs `JINA_API_KEY` in the `.env` (empty
by default). Without it the prefilter is skipped and the answers come without that
step; the smoke prints a `[note]`. After setting it, recreate the search container
(`… up -d edu_ai_search`).
`BIFROST_SETUP_TOKEN` is only a fallback for `/api` if the dashboard login is ever
switched off. Expose the administration UI only to an authorised administrator. Healthy containers and a smoke do not document a paid ingestion or
the RAG quality.

## Cloudflare tunnel (optional)

The tunnel is prepared but off: `cloudflared` runs only under the Compose profile
`cloudflare`. To bring it up on an installed host:

1. In the Cloudflare Zero Trust dashboard, use the existing tunnel (or create one;
   a recreated tunnel may keep its ID) and copy its token. Its public hostnames
   point at `http://edu_ai_nginx:81` (the `/search/` allowlist) and, for the
   gateway UI, at `http://bifrost:8080`. Put a **Cloudflare Access** policy on every
   hostname: the tunnel authenticates nothing and Bifrost inference has no auth.
2. Check that the Bifrost admin login works (see above).
3. Add to `~/edu-ai/config/.env`:

   ```bash
   COMPOSE_PROFILES=cloudflare
   CLOUDFLARE_TUNNEL_TOKEN=<token>
   ```

4. Start the tunnel and run the smoke test; the wizard itself never enables a
   Cloudflare profile:

   ```bash
   docker compose --project-directory "$HOME/edu-ai/src" --env-file "$HOME/edu-ai/config/.env" -f "$HOME/edu-ai/src/docker-compose.yml" -p edu_ai up -d cloudflared
   ```

   ```bash
   python3 "$HOME/edu-ai/src/scripts/smoke.py" --source-root "$HOME/edu-ai/src" --config-dir "$HOME/edu-ai/config" --state-dir "$HOME/edu-ai/state" --project edu_ai
   ```

   With the profile active the smoke also checks that `cloudflared` runs. Whether
   the hostnames answer through Cloudflare is checked from outside, in a browser.

`EDU_AI_TUNNEL_SUBNET` (pinned at installation) is the range nginx trusts for
`CF-Connecting-IP`; it is internal and need not match the old host. This procedure
has not been run against a real tunnel token in the documented VM runs.

Operations and backups: [OPERATIONS_LINUX.md](OPERATIONS_LINUX.md). Development and
tests: [DEVELOPMENT.md](DEVELOPMENT.md). Packaging: [RELEASING.md](RELEASING.md).

Repeated wizard installation was actually verified on upstream 06de66d. Later public commits have separate offline evidence, not automatically a new live VM PASS.
