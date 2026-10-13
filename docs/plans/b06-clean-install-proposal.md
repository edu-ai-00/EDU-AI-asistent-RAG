# B06-A: proposal for a clean EDU-AI installation on a new Linux server

The public copy omits internal acceptance reports and links to them; this proposal is not evidence of the current deployment.

**Follow-up Codex review (2026-10-05):** record. According to it the proposal is now unified: B06-1 is only offline configuration preparation
over an explicit SHA and target directory (R1), the identity test is separated from QA retrieval (R2), publication of the file pair and the secrets rule are
refined (R3), and the factual refinements are incorporated. This is not an approval of a Docker installation, of Compose changes or of production operation.

**Implementation review (Codex, 2026-10-05):** record. Four findings (R1 swap of the reservation, R2 late partial-clone check,
R3 `git replace`, R4 signal after `rename`) were fixed in three rounds and are covered by regression tests (section 10). The second round (2026-10-06) fixed the gaps of the first;
the third round consisted of three independent reviews (record), of which review A proved that cleanup could delete a foreign directory (unmet R1). That is closed.

Audit note (SHA c8fe0543): the state below is historical. B06-2 exists (`scripts/host_preflight.py`, `scripts/setup.py`) and the wizard was demonstrated
on real VMs, see [public-source.md](../releases/public-source.md); the statements "B06-2 and B06-3 do not exist" and "the bootstrap has not run" no longer hold.
Links to non-public files (`production-readiness.md`, `b03*-progress.md`, `audit-04a`, `r2-lab-results-v3.md`) cannot be verified in the public tree.

Status (2026-10-06): **candidate B06-1 + bootstrap (section 10), published on `origin/clean_init` (code and tests in commit `d2cc0c70d6bc669cc5ebd64744d0327ba7e3992f`, documentation in the following commit; its SHA cannot be stated inside it, see `git log` of these files). B06-1 is implemented offline and passed three independent reviews and the final Codex review (five findings closed); the bootstrap is hardened (Langflow key, secrets in output, atomic `.env` write) and the API keys were verified in the lab against Langflow 1.10.1. It is not a finished installation or a supported school upgrade; the full bootstrap, PostgreSQL and B06-2 are the next stage, B06-3 does not exist and the rest is a proposal. Supported upgrades are addressed only by the proposal [B06-U](b06-upgrade-strategy.md), which implements nothing.** Older states in this document are history.
Date: 2026-10-06 (original proposal 2026-10-05). Baseline SHA of the proposal: `a19f9716dfb681e2cfb39a5ff6c74bd0dcc09250` (`clean_init`); fixes R1–R4 were made on top of the published `cfd6f7de5b23d37e61efbfba119d6c905bde147d`.
On 2026-10-06 the owner approved publication of this candidate and keeping the API key rules (section 10.7); nothing else was approved. D12–D20 are considered not approved; inputs from B04a are listed only as unapproved input.
The scope is the **first installation**. Supported upgrades of an existing school are not done and the B03e-1 runner may be used only on isolated synthetic DBs
([AGENTS.md](../../AGENTS.md), production-readiness.md lines 47 and 158), so this proposal promises no upgrade.

The inputs were only read. The B06-1 implementation used no SSH, AWS, Cloudflare, production, network or database and installed nothing; tests ran locally on Windows and in WSL2. Docker was used only in the lab verification of the API keys against Langflow 1.10.1 (2026-10-06, section 10.6: a separate container without network, synthetic configuration). It did not read `.env`, keys, dumps or backups.
It did not touch R2 (scripts, nginx, Compose, `r2-*` documents), `bootstrap.py`, `envfile.py`, the applications or the schema.

Legend: **[S]** current behaviour evidenced by reading the source (not run in this session), **[N]** proposed change, **[?]** unverified assumption.
"Carried over" is a result of an earlier session, not repeated here. Source abbreviations (`BS:147` = file and line) are explained at the end.

## 1. Summary

1. **[S]** `bootstrap.py` already covers steps 3–10 of the table in section 2. From an existing `.env` it keeps the values that are already set and are not placeholders, but it may change the file: it adds keys missing relative to the template and overwrites lines with `CHANGE_ME` directly (`set_env`, `EF:57-83`, `BS:238-262`). It has no check of data, volumes, other containers or most ports.
2. **[S], conclusion from code [?]:** at the baseline SHA the bootstrap does not finish on a clean clone. `DC:537` requires `EDU_AI_TUNNEL_SUBNET`, `EX` does not know it and the bootstrap does not add it.
   Without it Compose does not render the model (carried-over result of the R2 lab, `R2L:66`). The bootstrap then only warns and skips the pg_search check (`BS:195-198`) and fails on `up`. Not run.
3. **[S]** In this proposal the Langflow key is a source explicitly managed in `.env` (`DC:140-141`). Whether a backup file exists in `langflow_data/`, and with what priority, is unverified (section 4). The bootstrap cannot tell that the key was lost or changed relative to the existing data.
4. **[S]** `smoke.py` does not verify search. The search `/health` returns static JSON and smoke does not call `/v1/ask` (section 7). **[N]** Without an LLM and without writes, the published `/v1/explain` can verify identity and the router, but not the reading of QA records and their filters; that needs an isolated synthetic fixture (V3b).
5. **Implemented offline (section 10):** B06-1 is `scripts/install.py` with the commands `preflight`, `plan`, `init-config`. Each takes an explicit SHA and target directory, calls no Docker, SSH, network or database, defers the host checks (`HOST_NOT_CHECKED`) and a successful write is only `CONFIG_PREPARED`, not an installation (section 8).

## 2. Steps of the first installation and what the bootstrap can do

| # | Step | Bootstrap today [S] | Gap → proposal [N] |
|---|---|---|---|
| 0 | Host: Linux x86_64, Docker Engine + Compose plugin, git, python3, outbound internet | Only `docker compose version` (`BS:147-152`). `edu_ai` has `platform: linux/amd64` (`DC:129`). The build downloads from Docker Hub, GitHub releases (`DP:22-25`), apt and PyPI/npm; Bifrost without internet ends at start (`BS:601-603`) | Preflight: `uname -m` = x86_64 (otherwise refuse), reachable daemon (`docker info`), disk space in Docker root and state-dir, reachability of sources. Determine the minimum Compose version and the disk threshold by measurement **[?]**; the verified version is Compose 5.3.1 (`PR:129,138`) |
| 1 | Code at the exact SHA | Not addressed. `ON:22-25` is only `git clone`, the URL is `<ORG>` | `git clone` + `git checkout --detach <SHA>`, verify `rev-parse HEAD` and a clean tree. The SHA and URL are supplied by the administrator (Q1) |
| 2 | Preflight | `BS:144-217`: compose present, ports 80/8443/8444 via `connect_ex` on 127.0.0.1 (`BS:167-170`), pg_search in an already existing image. Runs **before** `.env` is loaded (`BS:587-591`) | See section 6. Today "is a stack already running?" is looked up in the project derived from the directory name, not from `COMPOSE_PROJECT_NAME` |
| 3 | `.env` | `BS:224-262`: copy of `EX` only when missing, adds keys missing relative to `EX`, generates 4 secrets only for `CHANGE_ME` (`BS:88-93`, `245-250`) | File into external configuration (section 5). In addition `COMPOSE_FILE`, `EDU_AI_TUNNEL_SUBNET`, `CHAT_LEGAL_ENTITY`, `QA_PORTAL_ALLOWED_ORIGINS` (missing from `EX`) |
| 4 | Data directories | `BS:55-61`, `269-313`: five directories in the checkout; ownership via an `alpine:3` container mounting the whole ROOT, Linux only (`BS:292`, `306-307`) | Data outside the checkout. Set permissions before the bootstrap: a symlink points outside the ROOT mount, so `chown` in the container would fail |
| 5 | Build and start of the core | `BS:604-606`: `up -d --wait --wait-timeout 900 postgres bifrost edu_ai nginx`. An empty volume runs `init_clean/00-03` (`DC:323`, `ON:176-188`) | Unchanged, but only after the safeguards. The behaviour of `--wait` without a cloudflared token was not verified **[?]**, so list the services without it |
| 6 | Langflow API key | `BS:396-424`, `AK:121-162` (bootstrap candidate, section 10.4): the key in `.env` is verified first and the response has three states. **Valid**: unchanged. **Rejected (401 only) or no key**: replacement in the order list → create → verify with the same request → atomic write; the old key is never deleted (a masked prefix does not identify it reliably). **Unverifiable** (**403**, timeout, 5xx, redirect, odd response): nothing is changed and exit code 3. Before: **any error** = "does not work" → delete the old `edu_ai-bootstrap` and create a new one (`AK:63-75`, `93-102`) | Done in the candidate; the real Langflow 1.10.1 not verified |
| 7 | Start of the rest | `BS:614`: `up -d --wait` of all services; `--force-recreate` only after a key or mount change | Unchanged |
| 8 | Langflow variables (Generic) | `BS:461-501` | Unchanged |
| 9 | Import of 6 flows | `BS:508-518`, `IF:60-67`; idempotent PATCH by `endpoint_name` (`IF:124-127`) | Unchanged |
| 10 | Smoke | `BS:525-533` → `SM` (section 7) | Add V0, V2–V5, V8 |
| 11 | Summary | It used to print the Langflow and pgAdmin passwords to stdout (`BS:556-560`). Candidate: path to `.env` and the names of the created values, values shown once and only in an interactive terminal after a successful run (`step_summary`) | Do not print secrets in the log or in errors; one-time overview only in an interactive terminal (K7), otherwise only the path to the file |
| 12 | Manually after installation | OpenAI key into `.env` (`BS:256-262`, but see R-3). Further the provider in the Bifrost UI, the Cloudflare token and Access policy (`EX:131-142`), the perimeter from outside (`PC`), the first school (`A3A:46`) | The installer lists them as **unfinished**, it does not pretend to be done |

Steps 0–2 and the host checks in section 6 belong to B06-2. B06-1 does not do them and does not report them as met.

Further: a clean DB from `init_clean` **has no migration registry** (`schema_migration` is not in `init_clean`; `B3E`). The installation therefore ends at B216 without a supported upgrade path.
The installer does not call `scripts/migrate.py` (`AG`, "Never"); a test checks that it does not import it.

## 3. Configuration

Origin: **gen** = generated once by the installer, **supplied** = by the administrator, **const** = constant from the template, **computed** = computed for the installation, **service** = created by a running service.

| Variable | Purpose | Requirement | Origin | Read by | Source |
|---|---|---|---|---|---|
| `COMPOSE_PROJECT_NAME` | project identity, prefix of volumes and networks | required | const `edu_ai` | Compose | `EX:17`, `DC` (fixed `container_name`) |
| `COMPOSE_FILE` | which files are loaded; without it the override is picked up on its own and publishes 6 more ports | required on the server | computed `docker-compose.yml` (commented out in `EX`) | Compose | `EX:22`, `DO:24-46`, `A4A:232` |
| `EDU_AI_TUNNEL_SUBNET` | subnet of the `tunnel` network and the nginx trust range for `CF-Connecting-IP` | required at the SHA with the R2 candidate; **missing from `EX`** | computed `tunnel_subnet.py allocate` (read-only, exit 0/2/3) | Compose → nginx, cloudflared | `DC:537,545`, `TS:11-13,222-252` |
| `POSTGRES_DB_SERVER`, `_SERVER_PORT`, `POSTGRES_DB`, `_DB_SCHEMA`, `_DB_USER` | DB connectivity and identity | required | const | postgres, edu_ai (flows), search (`PG_*`), scripts | `EX:48-63`, `DC:152,311-316,394-399` |
| `POSTGRES_PASSWORD` | password of the only DB role | required, secret | gen | postgres (only the first start of an empty volume), edu_ai (DB URL and Global Variable), search | `EX:68`, `DC:152,312-316,397` |
| `PGADMIN_DEFAULT_EMAIL` | pgAdmin login | required; default `admin@edu-ai.eu` | supplied (the bootstrap does not ask) | pgadmin | `EX:71`, `DC:343` |
| `PGADMIN_DEFAULT_PASSWORD` | pgAdmin password | required, secret | gen | pgadmin | `BS:90` |
| `LANGFLOW_SUPERUSER`, `_PASSWORD` | Langflow superuser, created at the first start | required | name const `admin`, password gen | edu_ai, `AK` (login) | `EX:78-79`, `DC:157-158` |
| `LANGFLOW_SECRET_KEY` | Fernet key, encrypts Global Variables | required, secret, **never change** | gen (`BS:73-80`) | edu_ai only via `env_file` | section 4, `DC:185-190` |
| `LANGFLOW_API_KEY` | Langflow REST key | required after step 6 | service (Langflow issues it), written by a script | api, edu_ai (extraction flows), scripts | `AK`, `DC:93` |
| `LANGFLOW_URL` | Langflow address for scripts on the host | optional; without override `http://127.0.0.1:8444` | supplied / installer | scripts, api; via `env_file` also Langflow | `EX:94`, `BS:365-393`, `A4A:237` |
| `LANGFLOW_PIPELINE_INPUT_NODE_{MARKDOWN,WEB,PDF,OFFICE,QA}`, `LANGFLOW_MARKDOWN_LOGGER_NODES` | node IDs in flows | required | const | api (WEB/PDF/OFFICE/QA), edu_ai (MARKDOWN, logger) | `EX:170-178`, `DC:104-107` |
| `OPENAI_API_KEY` | key for the SDK; the real provider key is held by Bifrost | required (non-empty) | supplied, or a formal value (Q4) | edu_ai (Global Variable), search | `EX:111`, `DC:182-183,379` |
| `OPENAI_BASE_URL` | direction to Bifrost | optional | default in Compose | edu_ai, search | `EX:117`, `DC:184,387` |
| LLM provider key | real LLM and embedding calls | required for LLM paths | supplied in the **Bifrost UI**, not in `.env` | bifrost (`bifrost_data/config.db`) | `EX:160-161` |
| `BIFROST_ADMIN_USERNAME`, `BIFROST_ADMIN_PASSWORD` | Bifrost admin login, created by `bifrost/config.json` | username default `admin@edu-ai.eu`; password required, secret | const / gen (install.py, bootstrap) | bifrost; password blanked for Langflow | `bifrost/config.json` |
| `BIFROST_SETUP_TOKEN` | Bifrost v2 fallback access to `/api` if the dashboard login is switched off | required; secret | gen (install.py, bootstrap) | bifrost; blanked for Langflow | `bifrost/config.json` |
| `BIFROST_BIND`, `_PORT`, `_LOG_LEVEL` | publication of the gateway (inference without authentication) | optional; default 127.0.0.1:8099 | const / supplied | bifrost | `EX:127-129`, `DC:234-238` |
| `CLOUDFLARE_TUNNEL_TOKEN` | tunnel | optional; empty = cloudflared starts and only logs an error | supplied | cloudflared; via `env_file` also Langflow | `EX:131-142`, `DC:267`, `A4A:229` |
| `CHAT_LEGAL_ENTITY` | webchat school; without it chat gets 422 | required for chat; **missing from `EX`** | supplied | web_chat | `DC:436-440`, `WC:40` |
| `CHAT_ENTITY_PICKER`, `CHAT_COOKIE_SECURE` | school choice (test only); Secure cookie | optional; picker missing from `EX` | const `false` | web_chat | `DC:444-448`, `EX:103` |
| `QA_PORTAL_ALLOWED_ORIGINS` | allow-list for the Origin check in the QA portal. Empty = the Origin check is **skipped** (`QS:34-35`) | required for safe portal operation; **missing from `EX`** | supplied (the address seen by the browser) | qa_admin | `DC:487`, R-4 |
| `CORS_ORIGINS` | empty = closed | optional | const empty | api, search | `EX:159`, `DC:112,409` |
| `JINA_API_KEY` | `jina` reranker and prefilter | optional; in `EX` empty (without it the prefilter is skipped) | supplied | search | `DC:382` |
| `LANGGRAPH_MODULE` | has no reader in Compose | residual | const | — | `EX:164`, `A4A:237` |

## 4. Secret values

| Class | Values | Rule on repeated runs |
|---|---|---|
| **Generated once** | `POSTGRES_PASSWORD`, `PGADMIN_DEFAULT_PASSWORD`, `LANGFLOW_SUPERUSER_PASSWORD`, `LANGFLOW_SECRET_KEY` | **[S]** generated everywhere there is `CHANGE_ME` (`BS:245-250`), regardless of existing data. **[N]** only in `init-config` into a not yet existing directory; never a second time |
| **Issued by a service** | `LANGFLOW_API_KEY` | rotation only after 401 (not after 403 or any other error) and indirectly: a new key is created, verified and stored; the old key is never deleted (bootstrap candidate, section 10.4) |
| **Supplied by the administrator** | provider key in Bifrost, `OPENAI_API_KEY`, `CLOUDFLARE_TUNNEL_TOKEN`, `JINA_API_KEY` | **[N]** never generated or overwritten; the installer only verifies "non-empty, not `CHANGE_ME`" and does not print the value |
| **Preserved** | everything above, plus `langflow_data/`, `bifrost_data/config.db`, the `postgres_data` volume | safeguards G1, G2 |

### Persistence of the Langflow encryption key

**[S]** The key reaches Langflow from `.env` via `env_file` (`DC:140-141`); this source is managed by the proposal. `langflow_data/` is mounted as the Langflow configuration directory and the audit designates it a "backup source" of the key (`DL:36`, `A2A:23,27`), but this work did not verify its existence or its priority relative to `.env`, so I do not claim it as fact.
The key encrypts Global Variables in the `langflow` DB; after a key change the existing variables cannot be decrypted and must be deleted and created again (`DM:728-733`, `DC:185-190`).
Risks today: (a) `.env` lies in the checkout, `.gitignore` only ignores it and `git clean -fdx` deletes it (`GI:2-10`, `A4A:234`); (b) when `.env` is missing a new key is created even though data exist (`BS:245-250`, no check);
(c) the `.env` write is not atomic (`EF:83` `write_text`), an interruption can truncate the file; (d) a DB backup without the key is not enough to restore the variables, while the DB and the key together are enough to decrypt, so they are kept separately.

**[N]** Rules (implemented and tested in B06-1, section 8):

- **K1** The key is created exactly once in `init-config` (same shape as `_fernet_key` in `BS:73-80`) and only into a newly created set. No command overwrites it later or generates it again. There is no `--force`, rotation or `reveal-key`; a test checks the list of options.
- **K2** `install-record.json` carries the **full SHA-256** of the key (64 hex). `preflight` and `plan` over an existing target report `EXISTING_KEY_FINGERPRINT: MATCH`, `MISMATCH` or `UNVERIFIABLE` without the value and always refuse the target (exit 2).
- **K3** The set is published whole or not at all: a private staging directory in the same parent, `fsync`, a read-back check and a **single** `renameat2(RENAME_NOREPLACE)` of the staging onto the target (`ctypes`, no fallback path). A target that exists in any form (empty directory, file, symlink or dangling symlink) is not replaced by the kernel, which returns `EEXIST`. The target is not reserved in advance, so there is nothing to swap or clean up: a foreign directory at the target path is never overwritten or deleted. The staging directory is created under umask 0077, immediately opened through a descriptor (`O_DIRECTORY|O_NOFOLLOW`) and only an empty directory of the same user with mode exactly 0700 is accepted (the name is opened, not the inode created by `mkdtemp`; anything else is not used, `fchmod` is not called); both files are written, verified and deleted through this descriptor, not by name. Just before publication the staging name is compared with the inode the script created: anything else under the name is `STAGING_REPLACED`, nothing is published and nothing is deleted. After `rename` it is verified that the target is our inode and that the content read through the descriptor is the whole set. The gap between the name check and the `renameat2` call cannot be closed (the kernel takes names): a foreign object that appears in it under the staging name is moved onto the target (nothing is deleted or overwritten), and this is detected only after the fact (K4). The identity of the parent (`st_dev`, `st_ino`) and its resolved path are verified before the staging is created and after publication; a change is `TARGET_PARENT_CHANGED` and the values are not shown. This does not prevent swapping the parent, it only admits it. The target is thus either absent or complete, never half-written (a test watches the target during the write). Where `RENAME_NOREPLACE` is unavailable (another OS, a filesystem without support) the result is `UNVERIFIED PUBLISH_NOREPLACE_UNSUPPORTED` and nothing is published; a plain `rename` is not used because it would replace an empty directory.
- **K4** Interruptions and errors: the outcome is determined by the disk, not by a variable. After every `rename` attempt, after an exception of any kind (`except Exception`), an I/O error or a signal (SIGINT, SIGTERM), the script compares the identity (`st_dev`, `st_ino`) of our staging directory with the target and with the staging name. Our set is at the target: after verifying both files and the key fingerprint, read through the descriptor, `CONFIG_PREPARED` with `INTERRUPTED_AFTER_PUBLICATION` (signal) or `ERROR_AFTER_PUBLICATION` (other exception); a failed `fsync` of the parent is `DURABILITY_UNCONFIRMED`. Our set stayed in the staging: nothing is published and only our own work is cleaned up: the script holds the descriptor of every file it created and **destroys the content through it** (`ftruncate`), so no step by name can touch a foreign item. **No name is deleted**, neither file nor directory (`unlink` and `rmdir` by name cannot be secured against an object that has taken over the name in the meantime; a test over the AST enforces the prohibition). A staging with two zero-length files remains and is reported as `STAGING_LEFT_EMPTIED` with the full path, the number of emptied files and the number of foreign items (the next run lists it as `STALE_STAGING`, the operator removes it after inspection); when `ftruncate` fails, it is `STAGING_LEFT_NOT_EMPTIED` with an explicit warning that the directory may hold a secret. Anything else (our set is neither at the target nor under the staging name, or the target after `rename` is not ours): `UNVERIFIED PUBLICATION_STATE_UNKNOWN` with the path where our set lies now (on Linux from `/proc`, otherwise "neznámé místo"); nothing is deleted or changed. The SIGTERM and SIGHUP handler (closed terminal, SSH) stays until the end of cleanup; an interruption while determining the outcome or during cleanup is `INTERRUPTED_WHILE_FINISHING` (the staging may remain; the first signal is already enough because the handler raises an exception there too). After SIGKILL before `rename` only the staging with unpublished secrets remains in the parent (the next run reports it as `STALE_STAGING`, neither deletes nor uses it); after SIGKILL right after `rename` the complete set is at the target and the next run refuses it as existing (`TARGET_EXISTS_CONFIG_SET`, fingerprint `MATCH`). After publication: a closed stdout pipe does not change the result (exit `0`, `2` or `3`, `emit` is guarded and `flush` lies inside the guard); SIGTERM and SIGINT after returning from `init_config`, during printing, do not change the result either, the set is on disk and the values in `.env`, only the report is missing.
- **K5** Concurrency: whoever completes `RENAME_NOREPLACE` first publishes. The others get `EEXIST`, end with exit 2 (`TARGET_EXISTS_*`), clean up their staging and their secrets are discarded. Test: a paused first process loses to a second; six concurrent processes produce exactly one set and no leftovers.
- **K6** An existing target of any kind (file, empty, foreign, partial or complete directory, symlink, dangling symlink) is not overwritten, not opened for writing and nothing is regenerated from it. A partial set is never regarded as usable and its key is not regenerated.
- **K7** Printing the secrets (owner's decision 2026-10-06, replaces the earlier "never any output"): the protected `.env` remains the permanent source. The four values created by this very `init-config` run are printed once in the **final summary of a successful first run**, and only in an interactive terminal: stdout and stdin are both tty and no CI environment is set (`CI`, `GITHUB_ACTIONS`, `GITLAB_CI`, `JENKINS_URL` and others; the value `0`, `false`, `no` or empty does not establish CI). The block `GENERATED_SECRETS:` and four lines `NÁZEV=hodnota` stand right before the lines `HOST_NOT_CHECKED` and `INSTALLATION_NOT_STARTED`, so every result still ends with these two lines. In CI, with redirected output or without a tty on stdin only the path (`CONFIG_FILE`) and the line `GENERATED_SECRETS_NOT_SHOWN` are printed. Never printed: an existing value on a repeated run (it refuses the target, K6), a supplied external token or provider key, `LANGFLOW_API_KEY`, a value from a failed, refused or unclear run, or from a run interrupted or ended with an error after publication (the set is then on disk and the values are read from the file). The values live separately from the output lines (`Report.reveal`), only `emit()` prints them, and only once; error messages, progress lines, `preflight` and `plan` must not contain them. Unchanged: the key fingerprint in `install-record.json` (without the value), no `--force`, rotation or `reveal-key` option. The user must store the values in their vault at that moment; a real custody copy of the key is a condition of the later operational start (Q6), not a reason to block the offline stage. A terminal that captures output (session recording, scrollback of a shared screen) captures the values too; the script cannot tell.
- **K8** After installation, the persistence test V8: re-create `edu_ai` and repeat a probe flow that decrypts a Credential variable (later stage).
- **K9** Reading from Git: before any object is read, only the configuration is read and a promisor/partial clone is refused (`PARTIAL_CLONE_UNSUPPORTED`): any occurrence of the key `extensions.partialclone` (even with an empty or whitespace value) or `remote.*.promisor` or `remote.*.partialclonefilter`, regardless of the value (conservatively even `false`), because looking up a missing object can trigger a lazy fetch and Git treats even `partialclonefilter` alone or an empty extension name as a promisor. Every git process receives `GIT_NO_REPLACE_OBJECTS=1`, `GIT_NO_LAZY_FETCH=1` and a ban on all built-in transports: `-c protocol.allow=never` and additionally `-c protocol.<p>.allow=never` for `file`, `git`, `ssh`, `http`, `https`, `ftp`, `ftps`, `ext` and `fd`. A setting of a single protocol (at any configuration level, also via `GIT_CONFIG_COUNT` and `GIT_CONFIG_PARAMETERS`) and `GIT_ALLOW_PROTOCOL` take precedence over `protocol.allow`, so the second variable is removed from the subprocess environment and every protocol is banned separately. An overlooked promisor therefore cannot use any built-in transport. A custom remote helper (`foo::`) with `protocol.foo.allow=always` in the user configuration is not banned by this; on Git ≥ 2.44 it is unreachable because of `GIT_NO_LAZY_FETCH` and on an older Git only the refusal of a promisor repository protects against it (review B showed that a hole in the detector, `partialclonefilter`, launched the helper while emulating Git < 2.44; it is closed). The content is bound to the full SHA by **recomputation**, not by trust in the object store: the commit, its root tree and both files are read as raw objects and each must have a SHA-1 equal to its name (a forged loose object, an object in `info/alternates` or a replacement that Git would use is thus detected even without `GIT_NO_REPLACE_OBJECTS`); both files must be regular files in the root tree (`100644` or `100755`), a symlink or gitlink is refused (`SOURCE_FILE_NOT_A_REGULAR_FILE`); a mismatch is `SOURCE_OBJECTS_FAILED_VERIFICATION` (exit 3). There are seven permitted forms of the `git` command: `rev-parse --verify --quiet`, `rev-parse --show-toplevel`, `cat-file blob`, `cat-file commit`, `cat-file tree`, `config --get` and `config --get-regexp`.

**[?]** Unverified: (i) which source wins when both a key in `.env` and a file in `langflow_data/` exist (`A2A:27` claims `.env`); (ii) whether Langflow 1.10.1 creates the file at all when the env is set;
(iii) what the key encrypts besides Global Variables. A test later in the lab: a clean Langflow with the key in env, determine only the **existence** of the file (do not read the content) and decryption after re-create.

## 5. External storage of configuration and operational data

```
/opt/edu_ai/                     detached checkout at the given SHA (release, replaceable)
  .env -> /srv/edu_ai/config/.env                       symlink
  uploads  langflow_data  bifrost_data  sqllite_export  -> /srv/edu_ai/data/<name>   symlinks
/srv/edu_ai/config/              set from init-config (example value of --target-dir; 0700): `.env` and `install-record.json`, both 0600
/srv/edu_ai/data/<name>/         owner uid:gid per BS:55-61
Docker volumes of project edu_ai:  postgres_data, pgadmin_data, reranker_models, qa_admin_state   unchanged (DC:550-554)
```

- **[S]** Today everything lies in the checkout (`BS:55-61`, `DC:113,191-194`) and is only ignored by git (`GI:12-19,66`). `CI:221-224` recommends data outside the release.
- **[N]** `--target-dir` is directly the set directory. `init-config` creates it, the parent must exist, must not be writable by others (except for the sticky bit) and the target must not lie in the repository (the parent is resolved through symlinks).
- **[N]** Without changing Compose this works via symlinks: Compose reads `.env` from the project directory and bind sources are relative to it. `env_file: .env` is fixed in Compose (`DC:140-141`) **[?]**,
  `--env-file` does not replace it. A symlink as a bind mount source must be confirmed by the lab **[?]**. `pgadmin_exports/` stays in the checkout: it contains a tracked `.gitkeep` (`GI:85-86`), a symlink would be reported by Git as a change and it contains only exports.
- **[N]** A clean solution (a variable for the data root and the env file in Compose) is the unapproved proposal B04a S5 (`A4A:293-309`) and touches the Compose that R2 is changing right now. It does not belong in B06-A (Q2).

| Data | Today → proposal | Loss means |
|---|---|---|
| `.env` | checkout → `/srv/edu_ai/config` | loss of passwords and the key: DB inaccessible without manual intervention, Langflow variables cannot be decrypted |
| DBs `edu_ai` and `langflow` (one cluster) | volume `postgres_data` in Docker root, unchanged | loss of documents, QA, query history, flows, users and Langflow keys |
| `langflow_data/` | → data | backup source of the key, cache |
| `bifrost_data/` | → data | provider keys in `config.db` unencrypted, hence 0750 (`BS:51-54`) |
| `uploads/` | → data | uploaded sources for re-ingest |
| `sqllite_export/` | → data | output of the QA pack builder, regenerable **[?]** |
| `pgadmin_data`, `qa_admin_state`, `reranker_models` | volumes unchanged | pgAdmin accounts; short-term portal state (`DC:495-498`); model cache |

Docker root: **[N]** preflight prints `DockerRootDir` and the free space. Moving it is a host decision (`daemon.json`), not the installer's.
Backups: the one-off backup before S2 is done (record). The automatic policy and the restore test are postponed (D01–D04 not approved). The proposal only determines that a future backup must cover the rows of the table and keep the key separate from the DB.

## 6. Safeguards

The rule for all reads: what cannot be read or parsed is a **refusal**, not "free" (exit 3), the same as in `TS:11-13`.

**Scope of B06-1:** files and the target directory (G2, G8, refusal of an existing target) and no subprocess other than local `git`. The host checks (G1, G3, G4, G5, G9) remain mandatory before a later start; B06-1 does not run them and does not report them as met, the result always carries `HOST_NOT_CHECKED`. The prepared configuration must not be attached to existing data until B06-2 passes them.

| # | Risk | Today [S] | Safeguard [N] |
|---|---|---|---|
| G1 | overwriting existing data | No check of volumes or data (`BS:144-217`). Init runs only on an empty volume, otherwise it is silently skipped and after an unsuccessful first start a half-initialised schema remains (`ON:335`) | **B06-2, host:** preflight refuses: a volume, network, container or image tag of the installation exists; a non-empty state-dir or `langflow_data`; `.env` anywhere; a state that cannot be initialised |
| G2 | overwriting passwords and keys | `.env` is not overwritten, but a lost `.env` with an existing volume gives new passwords (Postgres ignores the value for an existing volume, `DC:312-314`) and a new key (see section 4) | **B06-1:** K1–K7, full SHA-256 of the key in `install-record.json`, publication of the whole set at once |
| G3 | interference with another installation | `container_name` is fixed for all 10 services (`DC:27,81,130,226,263,303,339,368,430,471`), mutable tags `edu_ai_api:1.0`, `edu_ai:1.1.0`, `edu_ai_postgres:cs`, `edu_ai_search:1.0`, `edu_ai_web_chat:0.1`, `edu_ai_qa_admin:0.1`; a second installation on a host is not possible (`A4A:233`). The same `COMPOSE_PROJECT_NAME` points to a foreign project. Preflight runs before `.env` (`BS:587-591`) | **B06-2, host:** one installation per host, project `edu_ai`. Read the project from the configuration before the check. Take names and tags from the **rendered model** (`docker compose config --format json`), not hard-coded. Every Compose command with `-p`, `--project-directory` and an explicit `--env-file`. Only list foreign containers |
| G4 | port collisions | The check covers only 80/8443/8444 by connecting to 127.0.0.1 (`BS:167-170`): a listener on another address and Bifrost 8099 (`DC:234`) are not seen. An unpinned `COMPOSE_FILE` picks up the override and publishes 5432, 7860, 8080, 8081, 8090, 5050 (`DO:24-46`) | **B06-2, host:** `ss -H -ltn` (v4 and v6, all addresses). Derive ports from the rendered model. Write `COMPOSE_FILE`. Unreadable `ss` = exit 3 |
| G5 | network collisions | Docker chooses the five networks from pools, `tunnel` has a given CIDR. The `tunnel` check is handled by `TS` (read-only, "undeterminable is not free", `TS:11-13`) | **B06-2, host:** the model requires `EDU_AI_TUNNEL_SUBNET` → `tunnel_subnet.py allocate --project edu_ai --reserved <administrator's LAN/VPN>`, the value into the configuration, `check` before every `up`. For the other networks list the pools and host routes and refuse a collision with `--reserved` **[?]** (nobody measured Docker's behaviour on a collision) |
| G6 | public exposure | nginx publishes 80/8443/8444 on all interfaces (`DC:28-31`); `/api/` writes without login (`NX:168-174`); the perimeter is held by the firewall and Cloudflare outside the repo (`ON:121-148`) | The installer claims nothing about the perimeter. The result carries `PERIMETR NEOVĚŘEN` until the administrator documents `perimeter_check.py --strict --ip` from outside. The script has the production hostname as its default (`PC:55`), `--host` is necessary. Bind variables in Compose = Q7 |
| G7 | destructive commands | `ON:337` recommends `down -v`; `CI:226-228` forbids it during deploy | **B06-1:** calls no Docker, SSH, network or database; the only subprocess is local `git` with seven permitted read forms. Later a whitelist of Docker verbs, in B06-A never `down`, `rm`, `volume rm`, `network rm`, `prune`, `-v`, `--remove-orphans` |
| G8 | interrupted or unclear state | `set_env` non-atomic (`EF:83`) and `key_works` took an error as invalidity (`AK:63-75`); both are fixed by the bootstrap candidate (section 10.4) | **B06-1:** publication of the whole set at once, unclear = exit 3. Including the API key rotation in the installer belongs in B06-2 |
| G9 | architecture | `edu_ai` is forced to `linux/amd64` (`DC:129`) | **B06-2, host:** `uname -m` ≠ x86_64 → refuse |
| G10 | migration runner | `AG` forbids it on a school installation | The installer does not call it and explicitly reports "bez podporované aktualizace" |

## 7. Verifying a functional installation without a paid LLM

`up --wait` alone is not evidence (`PR:50`). Below is what `smoke.py` verifies today and what is being added. A skipped check is not met (`SM:23-24`).

| ID | What is verified | How | LLM | Write | Status |
|---|---|---|---|---|---|
| V0 | state and identity | `compose ps`: the expected set of services, `running`/`healthy`, RestartCount 0, project label; image IDs into `install-record.json` | no | no | **[S]** part `SM:98-126`; the rest **[N]** |
| V1 | services respond | `SM:149-177` (api with `missing_config`, search, chat) + nginx `/health` (`NX:163-166`). The search `/health` is static (`SV:350-352,803-805`) and only the API has `missing_config` (`API:158-198`), so for search it **proves nothing** | no | no | **[S]** |
| V2 | seed complete | read-only SQL: `qa_stopword` without the column `form` and with PK `(lang,term,scope)`; `search_answer_generation` v15 active and no key with two active; row counts of the seed tables equal to the number of `INSERT`s in `02` (the generator uses `--column-inserts`, `B216.json`) plus the rows of `03`; content tables 0. **Not via the runner** | no | no | **[N]**, based on `B3C:29-37` (isolated DB) |
| V3 | identity and router | `POST /search/v1/explain` via nginx (the path is published, `NX:68-91`): an identity question for an entity **selected from the DB**, text from `qa_blocklist` (identity category), not hard-coded. Expect 200, `resolved`, `answer_source=meta`, `answer` containing `display_name`. `explain` ends after `qa_lexical` (`SV:1841-1897`), embedding comes only in `qa_vector`/`rag_vector`; route guard `RT:432-471`. It **does not prove** the reading of QA records or their entity, visibility, role and activity filters: the guard ends earlier | no | no | **[N]**; `/ask` evidenced `B3C:60-78`, `/explain` **[?]** |
| V3b | QA search and scope | an isolated synthetic QA fixture (not on a school DB): a positive `qa_exact` or `qa_lexical` and negative cases of another entity, role/visibility and an inactive record; answer source from a public deterministic prefix. No change of the cascade, thresholds, normalization and scope filters | no | isolated DB only | **[N]**, later stage |
| V4 | query log write | one `POST /search/v1/ask` with a unique `session_id`; a row in `question_answer_log` with the entity and the answer. Reason: `log_search` swallows the error (`A3A:40`) | no (guard, `B3C:74-78`) | **1 row** | **[N]**; keep vs. delete = Q5 |
| V5 | nginx edge | via nginx `/search/v1/query-log` and `/search/v1/staff-users` → 404 with an envelope; published paths 200 (`ON:150-154`, `NX:68-91`) | no | no | **[N]** |
| V6 | Langflow | superuser login, valid API key, 6 aliases, types of Global Variables, probe flow (decryption of Credential and connection to Postgres) | no | no | **[S]** `AK`, `SM:184-215,392-494` |
| V7 | ingest gate | dry-run `/v1/ingest` as an active `staff_user` from the seed | no | no | **[S]** `SM:303-359` (without a user only skip) |
| V8 | persistence | `up -d --force-recreate edu_ai`, V6 again; `stop` + `up -d --wait`, V2–V3 again. A second run of the installer is a no-op: hash of the external `.env`, container IDs and the list of volumes identical. Without `down`, without `-v` | no | no | **[N]** |
| V9 | Bifrost | container healthy (`DC:245-250`); validity of the provider key is **not verified** | no | no | **[S]** liveness |

**Not proven without a paid call:** a valid provider key in Bifrost, embedding (`qa_vector`, `rag_vector`), answer generation, ingest of documents and QA via Langflow (embedding), a cloud reranker. QA retrieval is also not proven without V3b.
This is verified by one deliberately approved paid call outside the automatic acceptance. Until then the result of the installation is `FUNKČNÍ BEZ LLM`, not `HOTOVO`.

## 8. B06-1: offline preparation of external configuration (implemented)

A new [scripts/install.py](../../scripts/install.py), standard library only. Every command takes the mandatory `--source-sha` (the full 40 lowercase hex, no `HEAD`, abbreviation or default value) and `--target-dir` (an absolute path without `..`) and optionally `--repo` (the default is the checkout of the script). Only the objects of the given commit are read from the repository, never the working tree or `.env`.

| Command | What it does | Write |
|---|---|---|
| `preflight` | verifies the commit, reads `docker-compose.yml` and `.env.example` from it, inspects them statically and checks the target | none |
| `plan` | `preflight` + listing: what will be created, the four generated names, `MISSING_MANUAL`, `SERVICE_ISSUED_LATER`, `UNRESOLVED_REQUIRED`, `OPTIONAL_NOT_IN_TEMPLATE`, `NOT_EVALUATED` | none |
| `init-config` | `preflight` + creation of a new protected set | only the new `--target-dir` with `.env` and `install-record.json` (0700, files 0600) |

**Output.** Every result ends with the lines `HOST_NOT_CHECKED` and `INSTALLATION_NOT_STARTED`. A successful write prints `CONFIG_PREPARED`, the path to the protected file and the full SHA-256 of the Langflow key; secret values are printed only by the one-time final summary in an interactive terminal per K7, otherwise none. The state "READY" does not exist: a set with missing inputs is `CONFIG_PREPARED` and in the record has `ready_for_install: false` with `blockers` (always `HOST_NOT_CHECKED`, further `MISSING_MANUAL:<name>` and `UNRESOLVED_REQUIRED:<name>`). Return codes: 0, 2 refused, 3 cannot be verified or completed (convention of `TS:11-13`).

**Inputs.** Only the four secrets that the bootstrap already introduces are generated (`BS:88-93`); `LANGFLOW_API_KEY` is issued by Langflow later. Manually supplied values (the remaining `CHANGE_ME_*`, today `OPENAI_API_KEY`) are reported by name. A required Compose variable that the template does not provide (today `EDU_AI_TUNNEL_SUBNET`) is reported as `UNRESOLVED_REQUIRED`; the subnet is not invented without a host inventory and is not written to `.env`. No application variable or settings flag is added: `.env` is the template from the commit with four lines overwritten.

**The static Compose scan** knows `$VAR`, `${VAR}`, `:-`, `-`, `:?`, `?`, `:+`, `+` and `$$`, skips comments, but not the text of block scalars. Nested, unterminated and multi-line constructs with quotes, `include:` and `extends:` are reported as `UNVERIFIED` (exit 3), it does not guess. Found or missing required variables do not imply completeness: the Compose model is not rendered and `INPUT_GAPS` is only a static count. Files outside the given commit, the override and `env_file` are not read.

**What B06-1 does not do:** it calls no Docker, SSH, network or database; it checks no ports, networks, volumes, containers or images; it neither runs nor changes the bootstrap; it creates no API or provider keys; it does not create schools; it does not modify Compose, nginx, R2, the applications or the schema. `init-config` requires Linux (`renameat2`) and refuses elsewhere (`UNSUPPORTED_PLATFORM`); `preflight` and `plan` run wherever `git` is available.

**Verifiable conditions and tests** ([test_install_preflight.py](../../tests/test_install_preflight.py), [test_install_config.py](../../tests/test_install_config.py), [test_install_findings.py](../../tests/test_install_findings.py) for findings R1–R4 of the first round, [test_install_publication_regression.py](../../tests/test_install_publication_regression.py) (R1 and R4) and [test_install_git_regression.py](../../tests/test_install_git_regression.py) (R2 and R3) of the second round, [test_install_reveal.py](../../tests/test_install_reveal.py) (the one-time secrets summary, K7) and from the independent reviews [test_install_cleanup_safety.py](../../tests/test_install_cleanup_safety.py), [test_install_git_binding.py](../../tests/test_install_git_binding.py), [test_install_parent.py](../../tests/test_install_parent.py) and [test_install_closeout.py](../../tests/test_install_closeout.py), the helpers [install_fixtures.py](../../tests/install_fixtures.py) and [install_child.py](../../tests/install_child.py); synthetic data and temporary directories only):

1. **Explicit inputs:** a missing `--source-sha` or `--target-dir` is an error; `HEAD`, a branch, an abbreviation, uppercase, 39 and 41 characters, 64 hex, a space and a newline are refused before `git` is called; the target must be absolute, without `..`, not the root; the list of options has no `--force`, rotation or `reveal-key`.
2. **The source is a commit:** the given commit is read (an older commit vs. `HEAD` vs. a dirty working tree), a tag, a tree, a non-existent SHA and a partial clone are refused, a missing file, CRLF, NUL, non-UTF-8 and a huge file have a defined result, `GIT_DIR` from the environment redirects nothing, a missing `git` is exit 3. A promisor/partial clone is refused before any object is read (a call-order test), also with a local synthetic promisor remote and with an ignored `GIT_NO_LAZY_FETCH`, and neither the repository nor the remote changes in the process (full snapshots of `.git` before and after are identical, also over a real `git clone --filter=blob:none` via `file://`); lazy fetch is allowed neither by an inherited `protocol.file.allow=always` (system or global configuration, `GIT_CONFIG_COUNT`, `GIT_CONFIG_PARAMETERS`) nor by `GIT_ALLOW_PROTOCOL=file`; for every "nothing happened" the test verifies that an unprotected git would have fetched the object; `GIT_NO_REPLACE_OBJECTS=1` is given to every git process even with another inherited value, and a replacement ref on the commit, tree and blob, `packed-refs` and a custom `GIT_REPLACE_REF_BASE` do not change Compose or the template.
3. **Static scan and template:** a table of supported and unsupported constructs; the template parser is identical to `envfile.read_env`; duplicates, unrecognised lines, `$` in a value and missing or non-placeholder generated keys are reported.
4. **Target:** an absent, empty, foreign, partial and complete directory, a file, a symlink and a dangling symlink; everything is refused and stays unchanged (a snapshot of the tree before and after); key fingerprint of an existing set MATCH, MISMATCH, UNVERIFIABLE without printing the value; a target in the repository, a missing parent, a parent writable by others, a foreign owner of the parent.
5. **Write and publication:** exactly two files with modes 0600 and a directory 0700, LF; `.env` differs from the template in exactly four lines; the full SHA-256 of the key and the hash of `.env` in the record; the set is never visible half-written; an error, an interruption and an `fsync` failure in every step publish nothing and clean up; `renameat2(RENAME_NOREPLACE)` replaces neither an empty nor a full directory, a file or a symlink; a foreign directory, file or symlink that appears at the target is neither overwritten nor cleaned up (publication, error and interruption); the staging is pinned by a descriptor: a foreign object under the staging name (a directory with content, an empty directory, a symlink, an object of another owner) gets no secret, is not published, and neither its content, inode, mode, owner nor mtime changes, nor is it cleaned up (`STAGING_REPLACED`, `STAGING_LEFT_EMPTIED`, `LEFT_IN_PLACE`); a signal (SIGTERM, SIGINT) delivered right after `rename` and an exception escaping from a call after a successful `rename` give a `CONFIG_PREPARED` corresponding to the disk (`INTERRUPTED_AFTER_PUBLICATION`, `ERROR_AFTER_PUBLICATION`); an unexpected exception in any step publishes nothing and cleans up; a swap of the result after `rename` gives `PUBLICATION_STATE_UNKNOWN` with a path and without deleting; a second real signal while determining the outcome or during cleanup is `INTERRUPTED_WHILE_FINISHING`, not a traceback or a silent exit; real SIGINT, SIGTERM and SIGKILL from outside in the windows before the syscall, after it, during the parent's `fsync` and during cleanup; a hard termination before and after `rename`; a repeated run after an interruption regenerates nothing; a lost race; six concurrent processes give one set.
6. **Hygiene and scope:** no generated value in the output, even on errors, except for the one-time final summary of a successful `init-config` in an interactive terminal (K7); the subprocess is only `git` in seven read forms with a fixed environment and a ban on all built-in transports (K9); the source imports no network, database or Docker modules, no `bootstrap` or `migrate` (`ctypes` serves only for `renameat2`); the checkout is not changed and `.env` is not read from it.
6a. **One-time secrets summary (K7)** ([test_install_reveal.py](../../tests/test_install_reveal.py), assertions name the leaked secret, never its value): the decision on the interactive terminal (stdout and stdin tty, no CI environment) and the print in `Report.emit`; the summary stands right before the final lines, is printed once and only the four generated names; in CI, with redirected stdout or stdin only the path is printed; it is not printed on refusal, on failure in any step (not even when the exception text contains the value), after an interruption and an error after publication, on a repeated run, in `preflight` and `plan`; supplied tokens and provider keys are never printed; the displayed value is identical to the protected `.env` (0600), which the summary does not change; all of this also through the whole command line via a real pseudoterminal (pty).
7. **Parity:** the names and shapes of the four secrets correspond to `bootstrap.GENERATED_SECRETS`, the placeholder rule is `envfile.is_placeholder`.
8. `python docs/tools/docs_check.py` passes. The normalization parity and the regression set are not run: B06-1 changes neither normalization nor retrieval.

**Further stages (each a separate assignment, none approved):** B06-2 host preflight (G1, G3–G5, G9, subnet inventory via `TS`) and `apply` (checkout at the SHA, directories, symlinks, `COMPOSE_FILE`, delegation to the bootstrap with fixes, without printing passwords outside the one-time summary per K7 and with rotation only after 401/403; requires root). B06-3 `verify` V0–V9 and V3b, first against a lab stack. B06-4 a full run in isolated Linux with Docker including the lab verification of the key (`[?]` from section 4). B06-5 only after the decisions Q3 and Q7.

## 9. Open decisions and conflicts in sources

### Owner's decisions (none of these is approved)

| ID | Question | Recommendation |
|---|---|---|
| Q1 | Which SHA is installed and from where? Pre-R2 `af5f491` (identical to production) × the state with R2 (requires `EDU_AI_TUNNEL_SUBNET`, R2 is not approved for deployment). The URL of the new GitHub and the way of access | The SHA is a mandatory input of all commands and has no default value; B06-1 works over both states |
| Q2 | The layout `/opt/edu_ai` + `/srv/edu_ai` with symlinks, or a clean solution with variables in Compose (unapproved S5)? | symlinks until B06-4, Compose only after R2 |
| Q3 | `02_seed_config.sql` contains **fictitious** entities and a `staff_user` (`SD:12-15`, `B3B:69-71`). Will they stay on a school server, and how is a real school created (`A3A:46`)? | keep for stages 1–4 (V3, V7 need them); the introduction of a real school is a separate task |
| Q4 | `OPENAI_API_KEY`: a real key, or a formal value when the provider is held by Bifrost (`DC:182-183`)? Is a formal value sufficient for all components? **[?]** | decide after the lab verification |
| Q5 | The query log row from V4: keep marked, or delete? | keep (deleting data is irreversible) |
| Q6 | Where and who holds the custody of the Langflow key and passwords? | outside the server, a different place than the DB backup |
| Q7 | Exposure of 80/8443/8444: only `PERIMETR NEOVĚŘEN` + `perimeter_check.py`, or bind variables in Compose? | only after R2 |
| Q8 | Cloudflare tunnel on the new server: yes (token, Access policy), or off? | off by default; do not start the service when the token is empty |

### Conflicts in sources (per `AG` they are defects, here they are only reported)

- **R-1** `EX` × `DC`: Compose requires `EDU_AI_TUNNEL_SUBNET` (`DC:537`) and reads `CHAT_LEGAL_ENTITY`, `CHAT_ENTITY_PICKER`, `QA_PORTAL_ALLOWED_ORIGINS`, `JINA_API_KEY`; the template does not contain them (`A4A:237`). The installer does not modify the template (R2), it supplements outside it.
- **R-2** `BS:83-87,261` claims that the search `/health` reports a missing OpenAI key. The code (`SV:350-352`) returns only `status` and `service`, only the API has `missing_config` (`API:158-198`). `SM:173-177` therefore always passes for search.
- **R-3** `ON:37-44` and `BS:256-262` want a real OpenAI key in `.env`, `DC:182-183` and `EX:160-161` say that the provider is held by Bifrost and the key in `.env` is formal. `ON:3` states 7 containers, Compose has 10 services.
- **R-4** The comment `DC:484-486` says that a write from a foreign origin is rejected. `QS:34-35` skips the origin check when the allow-list is empty; only the `X-QA-Portal` header remains.

Removed after review: `ON:337` (`down -v` on a manual restore) and `CI:226-228` (prohibition during deploy) are different contexts, not a conflict; for the installer, the protection of existing data applies. The missing tracked `postgres/releases/` is not a defect of the map in `AG`: the generators create the output, at this SHA there is just no published release manifest. This is a distribution limitation, not an obstacle to working with an exact Git SHA.

Further limitation: images are built on the host, so the result depends on unpinned bases and downloads (`DL:8` tag only; `web_chat/Dockerfile` and `qa_portal/Dockerfile` without a digest) and the pg_search `.deb` is downloaded without a checksum (`DP:22-25`). Versioned releases with ready-made images are outside B06-A.

## 10. Implementation status of B06-1 and bootstrap (candidate)

**Candidate B06-1 + bootstrap, 2026-10-06.** It is not a finished installation or a supported school upgrade. The host is not checked (`HOST_NOT_CHECKED`), nothing is started (`INSTALLATION_NOT_STARTED`),
B06-2 and B06-3 do not exist. Everything below is published on `origin/clean_init` (code and tests in commit `d2cc0c70d6bc669cc5ebd64744d0327ba7e3992f`, documentation in the following commit; its SHA cannot be stated inside it, see `git log` of these files). Basis: the clean `cfd6f7de5b23d37e61efbfba119d6c905bde147d` plus this work;
three independent reviews assessed the immutable snapshot `c93527e0d0ab833d3e3f81cac1f2cf043b31a305`; the final Codex review then led to five further fixes (review record, section 4).

### 10.1 External configuration of B06-1 and the `.env` of the bootstrap

These are two separate things that are not yet connected. `install.py init-config` creates a **new protected set** (`.env` + `install-record.json`) in an external directory.
`scripts/bootstrap.py` still works with the `.env` in the root of the checkout (or with a symlink to it) and does not read the external set; `init-config` does not call the bootstrap. The operational connection
(symlink `.env` → external set, delegation to the bootstrap, inclusion in `apply`) belongs to B06-2. What awaits it and is documented and tested today:

- Return codes: the bootstrap and `create_api_key.py` have `0` done, `1` error, `3` the key could not be verified and **nothing changed** (try again); `install.py` has `0`, `2` refused (nothing changed), `3` cannot be verified or completed.
  Mapping for B06-2: bootstrap `3` is "repeat", bootstrap `1` is an error after a possible partial change, therefore not "refused" (installer code 2 says that nothing changed).
  `langflow_url() is None` returns `1` today even though it is a typical "try again"; unchanged.
- `bootstrap.step_env` today creates `.env` in the checkout root with permissions 0600, atomically and without overwriting; the set from `init-config` is a different file with different values. Until they are connected, the two must not be deployed as if they were the same thing.

### 10.2 Files and hashes

Changed or new files (hash of `install.py` and the other scripts after the final Codex review; tests in the table below). All have LF and no NUL. The hashes of all 29 transferred files are in `tests/eval_out/b06_codex_fix_20261006/final_transfer_sha256.txt` (ignored by git).

```text
7dae1aa79118b2a1ba2b043e377f52e71eeb0a4d06674531a813dfa6f1290202  scripts/install.py
4c168a3515d50e9ed5bd65bf7e501b75be3f84fa67d0e0c44c06af4f4ea596c1  scripts/bootstrap.py
11aca6c3dbc07fb01ddc9fd7d2a8e3b7768ef53ee4e024d7e34fc9118a164d6c  scripts/create_api_key.py
97b9e6c65b156d95b17a6bac90733bc5e9d64aa0bec5b8a50c15b3060800cbd5  scripts/envfile.py
33eaec4a83b05fabf338df7ad09515f1abbe77c5e84323c8ad8259863e6e7f9e  scripts/http_safe.py                (new)
5c304fe622b48b673c8db6f702cc8a607353e88163ce869c51790f3d6f921e8a  scripts/import_flows.py
19a2b48b55908b8dd66ec645f6f7aeeb8d7d6f3e54477692d8e5765785125f53  scripts/smoke.py
```

| File | Tests | What it guards |
|---|---|---|
| `tests/test_install_preflight.py`, `test_install_config.py`, `test_install_findings.py` | 42, 29, 20 | B06-1 basis, R1–R4 of the first round |
| `tests/test_install_publication_regression.py` | 36 | R1 and R4 of the second round (staging identity, signals); since round 4 also the prohibition of removing a name in the source (AST, runs on Windows too) |
| `tests/test_install_git_regression.py` | 17 | R2 and R3 of the second round (transports, `git replace`) |
| `tests/test_install_reveal.py` | 29 | the one-time secrets summary (K7) |
| `tests/test_install_cleanup_safety.py` | 6 | never `rmdir` by name, adoption of a staging, FIFO, SIGHUP (review A) |
| `tests/test_install_discard_race.py` | 28 | point 1 of the Codex review: a second process swaps an item before each cleanup call (112 scenarios), inode, symlink, directory, FIFO, `ftruncate` fails, descriptors are always closed |
| `tests/test_install_git_env.py` | 19 | point 2 of the Codex review: Git environment of children, inherited `GIT_CONFIG` (file empty, missing, `GIT_CONFIG_COUNT`, `GIT_CONFIG_PARAMETERS`), call order, emulation of an old Git |
| `tests/test_install_git_binding.py` | 12 | `partialclonefilter`, `fd`, symlink, SHA recomputation (review B) |
| `tests/test_install_parent.py` | 6 | swap of the parent (`TARGET_PARENT_CHANGED`) |
| `tests/test_install_closeout.py` | 3 | pipe closed after publication |
| `tests/test_bootstrap_key_safety.py` | 57 | three key states, secrets in output, bootstrap summary |
| `tests/test_bootstrap_env_write.py` | 24 | N2 and N3: atomic write and private permissions of `.env` |
| `tests/test_bootstrap_key_rotation.py` | 30 | N4, N5, N8, N9: rotation order, redirect, `env_path`, code 3 |
| `tests/test_bootstrap_key_lifecycle.py` | 39 | points 3 to 5 of the Codex review: state after a possible `.env` write, real SIGINT, 403, the old key is not deleted, failed listing |
| `tests/test_bootstrap_output.py` | 13 | N1: `redact`, `relay`, duplicate line in `set_env` |
| `tests/install_fixtures.py`, `install_child.py` | | helpers |

Documentation: this proposal, the implementation review, [bootstrap hardening](b06-bootstrap-hardening-20261006.md),
three independent reviews, `ONBOARDING.md` §1 and §5. Shared plans: only the B06 passages in production-readiness.md and clean-init-progress.md.

### 10.3 Fixes R1–R4 (four rounds)

| Finding | Round 1 (snapshot `3b62a25`) | Round 2 (two workers) | Round 3 (independent reviews A and B) | Round 4 (final Codex review) |
|---|---|---|---|---|
| R1 swap of the reserved target | the `mkdir` reservation removed; publication by a single `renameat2(RENAME_NOREPLACE)` | staging pinned by a descriptor, identity check before and after publication, cleanup by inode, `STAGING_REPLACED` | **`rmdir` by name deleted a foreign empty directory, adoption of a foreign directory between `mkdtemp` and open deleted foreign files**: a directory is never deleted by name, the staging only empty 0700 without `fchmod`, own files only by inode, the rest `STAGING_LEFT_EMPTY` and `STALE_STAGING_EMPTY`; parent identity `TARGET_PARENT_CHANGED` | **race `os.stat` → `os.unlink` in cleanup** (a second process swaps the file between the calls): cleanup deletes **no name**, destroys the content of its own files through the held descriptor (`ftruncate`), the staging remains and the report carries the full path and state (`STAGING_LEFT_EMPTIED`, `STAGING_LEFT_NOT_EMPTIED`; replaces `STAGING_LEFT_EMPTY`) |
| R2 late partial-clone check | promisor configuration before objects, `protocol.allow=never` | ban of all built-in protocols and removal of `GIT_ALLOW_PROTOCOL` | the detector also knows `remote.*.partialclonefilter` and any occurrence of `extensions.partialclone`, transport `fd` | **inherited `GIT_CONFIG`** hid the repository's promisor from the detector: Git children do not inherit `GIT_CONFIG`, `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_n`, `GIT_CONFIG_VALUE_n`; the detector reads the whole actual configuration and runs before any object is read |
| R3 content substitution via `git replace` | `GIT_NO_REPLACE_OBJECTS=1` | no code change, extended tests | a symlink in a commit is refused, the content is bound to the SHA by **recomputation** of the chain commit → tree → blob | unchanged |
| R4 signal after `rename` | result according to the disk | exceptions according to the disk, `INTERRUPTED_WHILE_FINISHING`, `ERROR_AFTER_PUBLICATION` | SIGHUP, FIFO, pipe closed after publication (the exit status remains the result) | unchanged; a second interruption during completion now empties the files when it is certain that nothing was published |

### 10.4 Bootstrap: status of N1–N9

Origin of the N1–N9 labelling: [bootstrap hardening](b06-bootstrap-hardening-20261006.md), section 6. All tests are mocked (see section 10.7), not against Langflow 1.10.1.

| # | Status | What happened |
|---|---|---|
| N1 | partly closed, partly **open** | the output the bootstrap relays (`relay`, `redact`) is harder (only child line shapes, the rest of a multi-line message gone, control characters and credentials in URLs gone, `redact` never fails open). The standalone `import_flows.py` and `smoke.py` still print response bodies: a separate task |
| N2 | closed | `envfile.write_atomic`: a temporary file in the same directory, `fsync`, `os.replace`, the symlink stays, the existing mode is preserved; a failure leaves the bytes, mtime and directory listing. `set_env` overwrites all occurrences of a duplicate key |
| N3 | closed on Linux | `.env` is created 0600 from the first byte and never overwrites an existing one (`create_private`); an existing file with permissions for group or others only warns. On Windows `chmod` does not prove private ACLs and nothing is claimed |
| N4 | closed, changed after the Codex review | order list → create → verify → atomic write; the old key is never deleted; the new key is deleted only by the response `id` or on two successful listings, and after a possible `.env` write only when reading `.env` proves that it was not written; 403 is an unverifiable state (code 3); also applies to `--force`; a manual key is verified before storing |
| N5 | closed | `http_safe.py` refuses redirects on all calls that carry a key or token |
| N6 | closed | the message about `OPENAI_API_KEY` no longer claims that `/health` reports it |
| N7 | closed | `ONBOARDING.md` §1 and §5, the bootstrap table in section 2 and this section |
| N8 | closed | `ensure_api_key(env_path)` without a default, all callers pass it |
| N9 | tested, wrapper not created | propagation of code 3 via `bootstrap.main()` and the CLI, `--help` of both commands; mapping for B06-2 in section 10.1 |

### 10.5 Change of requirement: one-time secrets summary (2026-10-06)

The owner changed the earlier rule "never any output": after a successful first run in an interactive terminal, the local passwords and keys created by this very run are printed once so that the user can store them in a vault. The rule is in K7;
the implementation is in `scripts/install.py` (`interactive_terminal`, `Report.reveal`, `Report.emit`, wiring in `init_config`) and does not change the publication of the set, `.env` or the record. The scope is `init-config`, because B06-1 creates no other secrets;
"successful first installation" here means a successful first run of `init-config` (`CONFIG_PREPARED`), not an installation. The same principle applies to the summary in `bootstrap.py` (`step_summary`) for the values created by the bootstrap.

Confirmed owner choices (kept, no further confirmation required): interactive means both stdin and stdout; the CI detection is kept (below); the summary stands before the final lines; after an interruption or an error after publication the values are not shown; secrets stay in the protected `.env`.

The current CI detection differs by tool and is kept so:

| Tool | Interactive when | CI recognised by |
|---|---|---|
| `install.py` | stdout and stdin are tty and it is not CI | 13 variables: `CI`, `GITHUB_ACTIONS`, `GITLAB_CI`, `BUILDKITE`, `JENKINS_URL`, `TF_BUILD`, `TEAMCITY_VERSION`, `CIRCLECI`, `TRAVIS`, `APPVEYOR`, `DRONE`, `CODEBUILD_BUILD_ID`, `BITBUCKET_BUILD_NUMBER` |
| `bootstrap.py` | stdin and stdout are tty, it is not CI and there is no `--no-input` | 6 variables: `CI`, `GITHUB_ACTIONS`, `GITLAB_CI`, `JENKINS_URL`, `BUILDKITE`, `TF_BUILD` |

For both: the value `0`, `false`, `no` or empty does not establish CI; another CI system without these variables would show the values with a terminal on both streams. `install.py` has no `--no-input` (a test checks its list of options).

### 10.6 Results

The coordinator ran the whole install and bootstrap suite on the resulting candidate once per platform after the last change of code and tests of the final Codex review (hashes of code and tests before and after the run identical); repetition without a code change was not done. A skipped test is not reported as met.

| Run | Result |
|---|---|
| Windows 11, Python 3.11.9, Git 2.54.0.windows.1: `test_install_*.py` | 247 tests, OK, **139 skipped**, 108 executed. Skipped: 136× require Linux (`renameat2`, `fork`, `/proc`, signals, modes, pty, pipes, `sh`), 2× cannot create a symlink, 1× POSIX modes outside root |
| Windows: `test_bootstrap_*.py` | 163 tests, OK, **11 skipped** (POSIX modes and links), 152 executed. Mode, umask and symlink are not verified on Windows |
| WSL2 Ubuntu (Python 3.14.4, Git 2.53.0, UID 1000), copy and `TMPDIR` on ext4: `test_install_*.py` | 247 tests, OK, 0 skipped |
| WSL: `test_bootstrap_*.py` | 163 tests, OK, 0 skipped |
| Two-process reproductions of the Codex review findings (coordinator, WSL ext4): cleanup, `GIT_CONFIG`, readback and SIGINT, two identical prefixes | snapshot: a foreign object deleted, promisor overlooked and a custom helper run, the new key deleted, both keys deleted; fixed candidate: none of this. Details in the review record |
| Targeted mutation check of the round 4 fixes (workers, copies of sources) | cleanup 10 mutants, Git 4, key 19: all 33 caught. Earlier round: 23 defects (coordinator) and 10 earlier defects of the secrets summary |
| Lab verification against Langflow 1.10.1 (2026-10-06, image `edu_ai:1.1.0` `sha256:b85e298b6380…`, a separate container without network, its own temporary database, test keys) | all eight checks executed, no proven compatibility defect, code and tests were not changed; main finding: **an invalid key gives 403**, `check_key` must unpack gzip, login is rate-limited (429). Section 5 of the review record |
| Three independent reviews and the final Codex review | record in b06-independent-review-20261006.md |
| `python docs/tools/docs_check.py` (Windows), `git diff --check` against the snapshot | docs_check OK (in a clean copy of the candidate: 14 pages, 71 files), diff-check clean |

Earlier runs (rounds 1 to 3, 10 mutants of the summary, 18 and 10 mutants of the workers, 23 mutants of the coordinator) are history and were not repeated in this round.

### 10.7 Unverified items and limitations

- **Real Langflow 1.10.1:** verified in the lab 2026-10-06 (section 5 of the review record): an invalid key gets **403** (not 401), the create response carries `id` (a UUID as a string), the listing is masked (`sk-` and 5 characters, the rest `*`), `GET /api/v1/flows/` returns a gzipped JSON list. Still unverified: whether 401/403 always come from Langflow (a proxy in front of it), PostgreSQL instead of SQLite and an actual run of the bootstrap. Offline tests are still based on mocks and on a local fake transport under the real redirect mechanics of `urllib`.
- **Consequence of 403 on Langflow 1.10.1:** `replaced` never fires; after `down -v`, `create_api_key.py --force` is needed (the bootstrap does not have it). The owner confirmed the rule and this consequence on 2026-10-06: HTTP 403 is an unverifiable state (code 3, no automatic replacement), a manual `--force` may create a new key, the original key is not deleted automatically; no further confirmation is required.
  An unknown listing shape only leads to nothing being deleted (a safe error); an unknown `id` leads to looking for the new key by the difference of two successful listings.
- **The old key remains:** a masked prefix does not identify it (keys of the same name may share a visible prefix and a rejected key is not in the listing at all), so it is never deleted. After a replacement and after a lost `.env`, old keys named `edu_ai-bootstrap` remain in Langflow; the script says so and the operator removes them manually (Settings → API Keys).
- **403 is not replaced automatically** (code 3, `--force` is a manual choice). If Langflow returns 403 even for an invalid key, an automatic replacement after `down -v` never happens; verify in the lab. After `replaced`, `up -d --force-recreate` is run for all services; there is no lock between concurrent bootstrap runs; `check_key` has no overall deadline or response size limit. Unchanged.
- **R1, remnants:** `renameat2` takes names, so a foreign object under the staging name between the check and the call is moved onto the target (nothing is deleted or overwritten, result `PUBLICATION_STATE_UNKNOWN`, the next run `TARGET_EXISTS_*`); a swap of the parent is only admitted, not prevented;
  an empty directory of the same user with mode 0700 that took over the name between `mkdtemp` and open is not distinguished (it is used as the staging, nothing is deleted in it, files created in the meantime are emptied). After an unsuccessful run, `.name.staging-*` with two zero-length files remains (content destroyed through the descriptor, `ftruncate` does not overwrite freed blocks); names are not deleted and the operator removes the directory after inspection.
- **R4, remnants:** SIGTERM and SIGINT after returning from `init_config` (during printing) kill the process without a report, or with a traceback respectively; the set is complete and the values are in `.env`. SIGQUIT is not handled, calling from a thread other than the main one was not tried.
- **Secrets summary:** a terminal with session recording captures the values and the script cannot tell. The `Report` object holds the values before `emit` (`repr` does not). argparse errors and `--help` have no final markers. Windows ConPTY, `tmux`, `screen` and `ssh -t` were not tried.
- **Cleanup, round 4 remnants:** the window between the return of `os.open` and writing the descriptor into `run.held` (the file is then empty, nothing leaks; after an interruption it remains as a foreign item); a third interruption inside the handler of the second escapes from `init_config`; `_locate` depends on `/proc`; SIGKILL and `os._exit` leave full secrets in the staging (the next run `STALE_STAGING`).
- **Git, round 4:** a `safe.directory` passed via `GIT_CONFIG_COUNT` no longer works (it belongs in the global configuration, `GIT_CONFIG_GLOBAL` stays), it fails safely as `GIT_FAILED`. Outside the scope of B06, `scripts/eduai_migrate/manifest.py` also runs `git` (`git show`, `git -C`) without sanitising the environment; unchanged, it is not part of the installation path.
- **Git:** tested Git 2.54.0 (Windows) and 2.53.0 (WSL); a real Git older than 2.44 only by emulation. A custom remote helper is reachable only on Git < 2.44 and did not run after the detector was closed. The tests require `git` in `PATH`. SHA-256 repositories: a 40hex SHA is refused, a 64hex is refused by the regexp.
- **Filesystem:** `renameat2(RENAME_NOREPLACE)` verified only on kernel 6.18 (ext4, tmpfs). NFS, overlayfs, FUSE, `/mnt/d` (drvfs), running as root, ACLs and SELinux were not tried. Mode 0700 after `mkdtemp` can cause a failure on a filesystem that ignores the mode on `mkdir`.
- **Windows:** `init-config` refuses there (`UNSUPPORTED_PLATFORM`); publication, signals, modes and pty are not verified there. For a `.env` created by the bootstrap on Windows privacy is not claimed.
- The static scan is limited to the subset of YAML that Compose uses today; the classification of `LANGFLOW_API_KEY` as "issued by Langflow later" is a constant in the script. After SIGKILL before `rename` the staging with unpublished secrets remains (the next run reports it as `STALE_STAGING`).
- None of the host checks (G1, G3–G5, G9), `apply`, `verify`, the full bootstrap or PostgreSQL was tried (the behaviour of API keys was, section 10.6). The normalization parity and the retrieval regression set were not run: the candidate does not change them.
- **Review:** the independent reviews and the final Codex review took place (section 10.3 and the review record); the points of the final review are closed and verified offline; compatibility with Langflow 1.10.1 is verified in the lab (section 10.6). The candidate is published (section 10); the rule for 403 is confirmed.

### 10.8 Next step

1. ~~Decision on the commit of the candidate~~ Done 2026-10-06: the candidate after the final Codex review (five points closed, section 10.3 and the review record) is published on `origin/clean_init` (code and tests in commit `d2cc0c70d6bc669cc5ebd64744d0327ba7e3992f`, documentation in the following commit; its SHA cannot be stated inside it, see `git log` of these files).
2. ~~Lab verification against the real Langflow 1.10.1~~ Done 2026-10-06 (section 5 of the review record). The rule for 403 (the only signal of a dead key in Langflow 1.10.1) and a manual `--force` were confirmed by the owner; further verifications remain: a proxy in front of Langflow, PostgreSQL, the full bootstrap against a stack.
3. Only then a separate assignment B06-2 (host preflight, `apply`, mapping of the codes from section 10.1 and connecting the external set with `.env`). B06-3 and B06-4 unchanged. [B06-U](b06-upgrade-strategy.md) is a separate proposal of supported upgrades; nothing of it is implemented.

## Sources

The abbreviations in the text refer to these files (the number after the colon is the line).

| Abbr. | File | Abbr. | File |
|---|---|---|---|
| AG | [AGENTS.md](../../AGENTS.md) | NX | [nginx/conf/nginx.conf](../../nginx/conf/nginx.conf) |
| PR | production-readiness.md | SV | [search/app/server.py](../../search/app/server.py) |
| ON | [ONBOARDING.md](../../ONBOARDING.md) | RT | [search/app/nodes/route.py](../../search/app/nodes/route.py) |
| BS | [scripts/bootstrap.py](../../scripts/bootstrap.py) | API | [api/app/main.py](../../api/app/main.py) |
| EF | [scripts/envfile.py](../../scripts/envfile.py) | WC | [web_chat/web_chat.py](../../web_chat/web_chat.py) |
| AK | [scripts/create_api_key.py](../../scripts/create_api_key.py) | QS | [qa_portal/backend/app/api/security.py](../../qa_portal/backend/app/api/security.py) |
| IF | [scripts/import_flows.py](../../scripts/import_flows.py) | SD | [postgres/init_clean/02_seed_config.sql](../../postgres/init_clean/02_seed_config.sql) |
| SM | [scripts/smoke.py](../../scripts/smoke.py) | B216 | [postgres/baseline/B216.json](../../postgres/baseline/B216.json) |
| TS | [scripts/tunnel_subnet.py](../../scripts/tunnel_subnet.py) | GI | [.gitignore](../../.gitignore) |
| PC | [scripts/perimeter_check.py](../../scripts/perimeter_check.py) | CI | [CI-CD.md](../../CI-CD.md) |
| EX | [.env.example](../../.env.example) | DM | [DATA_MODEL_AND_SETUP.md](../DATA_MODEL_AND_SETUP.md) |
| DC | [docker-compose.yml](../../docker-compose.yml) | B3B | b03b-progress.md |
| DO | [docker-compose.override.yml](../../docker-compose.override.yml) | B3C | b03c-progress.md |
| DP | [Dockerfile.postgres](../../Dockerfile.postgres) | B3E | b03e-progress.md |
| DL | [Dockerfile](../../Dockerfile) (Langflow) | A3A | audit-03a-installation-baseline.md |
| R2L | r2-lab-results-v2.md | A4A | audit-04a-containers-networks.md |
| INS | [scripts/install.py](../../scripts/install.py) | A2A | audit-02a-backup-assessment.md |
