# nginx S2: local lab and tests

Provides evidence for the S2 candidate (a historical lab suite):
directory mount `nginx/conf` → `/etc/nginx`, dynamic DNS of upstreams (`resolver` + `zone` + `resolve`),
no `depends_on`, an nginx healthcheck. **No access to production, no secrets, no real key, tunnel or LLM.**
It covers only S2; nothing else from B04a (networks, profiles, `admin_proxy`, env, data) is tested here.

## What is needed

| Requirement | Used during the recording | Note |
|---|---|---|
| Docker Engine + Compose v2 ≥ 2.24 (`!override`) | Docker 29.6.1, Compose 5.3.0 (Docker Desktop, WSL2) | a local daemon is enough; the lab publishes nothing to the host |
| Python ≥ 3.10, standard library only | 3.11.9 | `python` on Windows, `python3` on Linux; with an older Python run_s2.py exits immediately, with a message |
| git | – | the baseline is read from `git archive`, not from the working tree |
| bash + GNU coreutils (`timeout`, `sort -V`, `mktemp`) | Git Bash 5.x (Windows), the system one on Linux | only the `rehearsal` command (it runs the real `scripts/nginx_s2_switch.sh`); `docker` and `git` must work in it, otherwise set `S2_BASH` to a suitable bash |
| image `nginx:1.27-alpine` **exactly as production** | `sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10` (nginx 1.27.5) | the `static` test fails if the ID is different; see below |
| image `python:3.12-slim` | `sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea` | only for the synthetic upstreams and the client; the ID is only recorded |

```bash
# when the nginx image is missing or has a different ID (the tag 1.27-alpine moves on Docker Hub):
docker pull nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10
docker tag  nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10 nginx:1.27-alpine
docker pull python:3.12-slim
```

## Rehearsing a newer nginx image

The evidence above ran on the production image (1.27.5). `docker-compose.yml` may name a newer tag; to run the
same checks against it, set both variables (the ID is the one `docker image inspect <tag> --format '{{.Id}}'` prints):

```bash
EDU_AI_LAB_NGINX_IMAGE=nginx:1.30.5-alpine EDU_AI_LAB_NGINX_ID=sha256:<id> python tests/nginx_r2/r2lab.py run --out r2.json
```

The `env.nginx_image` check then compares against that ID. The rehearsals that check out historical commits
(`rehearsal*.py`, `candidate.py`, `switch_offline.py`, the S2 `--baseline`) need a clone with that history.

## Running

From the repository root, with the S2 candidate in the working tree and **not yet committed** on the commit that is the baseline:

```bash
python tests/nginx_s2/run_s2.py all --baseline c2355aeb0b1e743f2b6ff591f8e91e57c34126c7 --out b04a-s2-results.json
python tests/nginx_s2/run_s2.py static --baseline c2355aeb0b1e743f2b6ff591f8e91e57c34126c7   # ~20 s, no running containers
```

**The result is taken from a clean clone**, not from a working tree with untracked material (S2-R4). The wrapper creates a fresh clone,
copies into it only an explicit list of candidate files (the modified tracked files + the untracked `nginx/conf`, `tests/nginx_s2`,
`scripts/nginx_s2_switch.sh`; nothing from `docs/plans`, `QA` etc.), verifies that the clone's `git status` is exactly that list, and in the clone runs
`git diff --check`, docs-check and the suite with the explicit full baseline SHA:

```bash
python tests/nginx_s2/clean_clone.py --baseline c2355aeb0b1e743f2b6ff591f8e91e57c34126c7 --out tests/eval_out/s2_clean   # ~25 min
```

Without `--baseline` with a full SHA that is at the same time `HEAD`, the wrapper exits. Any command can also be passed individually (`... static selfcheck`). The output is human-readable, plus JSON (`--out`) with the machine results,
image IDs, SHA-256 of the candidate files and time series. The exit code is 0 only when **everything** is `PASS`;
`FAIL` (the check failed) and `ERROR` (the check could not run) are both non-zero.

| Command | What it shows |
|---|---|
| `static` | A: `nginx -t` without upstreams in the production image (and a check that the baseline fails the same test); the Compose diff = only the `nginx` service and only the keys `volumes`, `depends_on`, `healthcheck`; the mount contains only `nginx.conf`, `search_api.conf`, `mime.types`, no default server or `conf.d`; **the effective configuration (`nginx -T`) differs exactly by DNS (`resolver`, `zone`, `resolve`); `access_log` and every `location` unchanged, the old `nginx/*.conf` byte-identical and unused**; the trusted proxy range and pgAdmin headers unchanged; `mime.types` byte-identical to the image |
| `equivalence` | B: the same 173 requests against the baseline and the candidate behind the same synthetic upstreams; whole responses (status, all headers in order except `Date`, the whole body); `identical + different + not evaluated`; explicit assertions independent of the match; rate limit (tolerance); **a real SSE and a real WebSocket handshake** |
| `resilience` | C: start with a missing upstream, the other paths, return without reload and restart, replacement with a provably different IP, outage: stopped service / removed DNS record / dead IP / unresponsive process (status and time), log noise; the healthcheck is logged like any other request (and ordinary loopback and `CF-Connecting-IP: 127.0.0.1` are logged too) |
| `atomic` | E: atomic replacement of a file in a mounted directory + `nginx -t` + `nginx -s reload` really changes the response; a check that **today's mount of individual files** goes stale after the same replacement; a proven limitation (replacing the directory itself is not picked up) |
| `selfcheck` | D: fault injection of the checks themselves (unhealthy, no healthcheck, `starting`, a non-running container, `docker exec` failure, a missing interpreter, HTTP client failure, the comparator) and **a sensitivity check by targeted mutations** of the candidate files created directly in the test (a missing pgAdmin `X-Forwarded-Port`, a widened trusted range, a changed `/health`, a re-added log filter): each must be found and the unmutated check must have 0 differences, without any untracked material; cleanup must work independently of the cwd and a foreign `COMPOSE_FILE`/`.env` and also watch for leftover volumes |
| `rehearsal` | F: the real `scripts/nginx_s2_switch.sh` on disposable clones from the production SHA `38a0bef` through `3a2b1d6` and `c2355ae`, the tools commit and the S2 commit: preflight without change and deployment with outage measurement (F1–F7); **refusal before any change** (F8: unknown commit, local commit, local change, file collision, `nginx -t`, `daemon off;` in the configuration, a different service, an earlier change of the old files, a missing/non-running container, an unavailable search and API upstream, a changed image in the file and in the override, a moved tag, ports, mount, healthcheck, a missing arguments file); automatic return after a failure of health / smoke / the `compose up` deadline / a checkout change (F9) and **a moved tag that stays moved through the whole deploy and return**, a late manual rollback in a new process after a tag move and **a real restart loop of the new container** (F9); interruption (F10); return on a conflict, a local change, after S2b and with a missing upstream (F11); a hash safeguard of the fallback (F12); unit tests of the safeguards (F13); **real signals INT/HUP/TERM** (F14); **container name collision** (F15: its own half-created and renamed nginx is removed after verifying the project and service labels; a holder of the name with a different project/service or without labels stays unchanged and the return ends with code 5 and a collision error); without `reset --hard`, `clean`, `stash`, force push |

## How it is wired (so that the result means something)

* nginx is started by the **real** `nginx` service from `docker-compose.yml` (baseline from `git archive`, candidate from a copy of the
  working tree) via `docker compose up --no-deps nginx`; only `container_name`, `ports` (nothing is published)
  and `networks` (lab networks) are overridden. Compose runs with an **empty** `--env-file` and an empty `.env` in the temporary copy:
  the repository's real `.env` is not read.
* the upstreams are synthetic ([stub.py](stub.py)): an echo of the whole received request, a timed SSE, a real WebSocket.
  Each is a separate container with an alias of the name from `nginx.conf`, so it can be individually stopped, disconnected from the network, paused,
  replaced with a different IP.
* two networks: `172.x` (in the range that `set_real_ip_from 172.16.0.0/12` trusts) and `192.168.x` (not). A client from each.
* the client ([client.py](client.py)) runs inside the network, does not follow redirects, returns whole responses; a transport error is `error`,
  never a status.
* the atomic replacement runs on a real Linux filesystem (a named volume inside the Docker VM, a bind mount from its
  `Mountpoint`), not over a Windows disk share, where inodes do not have the same semantics.

Everything carries the labels `edu_ai.purpose=b04s2-nginx-lab` and `edu_ai.run=<id>` and the names `b04s2_<id>_*`. **Cleanup deletes only the resources of its own
run** (containers, networks, **volumes**, Compose projects by name and by the label of this run, the temporary directory including git clones)
and reports if anything remained. Note: `docker compose up --no-deps nginx` creates **all** networks and volumes of the project
(3 networks and 4 volumes), not only those nginx uses; therefore cleanup is done via `down -v` with the same explicit definition
as the start, and leftovers are also watched by volumes. After an interrupted run (Compose projects carry no labels, only the name):

```bash
python tests/nginx_s2/cleanup_lab.py                  # lists the run IDs that have leftovers, deletes nothing
python tests/nginx_s2/cleanup_lab.py <run id>         # lists and deletes the containers, networks and volumes of that one run only
```

## Deployment and return script

`rehearsal` does not perform its own imitation of the procedure: it runs the **real** [`scripts/nginx_s2_switch.sh`](../../scripts/nginx_s2_switch.sh)
(`smoke`, `preflight`, `deploy`, `rollback`) on disposable clones. The clone starts at `38a0bef`, nginx in it is created from
its own `docker-compose.yml`, and the release goes through `3a2b1d6` → `c2355ae` → the tools commit → the S2 commit. The last two
commits are built from the candidate files of the working tree (tools and S2 separately, so that only S2 is reverted on return).
Single-defect variants (`bad_conf`, `bad_compose`, `bad_health`, `bad_smoke`, `bad_mount`, `bad_mount_wide`, `bad_image`, `bad_ports`, `bad_healthcheck`, an earlier change of the old files) and later releases (`l1` different files, `l2` a conflict with a revert and an empty line inside a block, `l3` = S2b removes the old files) are produced in the test.
The script's Compose arguments are read from `S2_COMPOSE_ARGS_FILE` (one argument per line); the line `@BASE@` marks the base compose file: for `up` it is the checkout's file, for preflight the candidate from stdin is read in its place (`-f -`), with the same arguments, env and override. Without the file, `.env` must pin `COMPOSE_FILE=docker-compose.yml`.
The script creates nginx from the **image ID**, never from a tag (the override `image-pin.yaml` as the last `-f`), and stores `images.env` (the original and validated ID, the candidate) and private tags `S2_EVIDENCE_REPO:original-<id>` / `validated-<id>`; a later `rollback` in a new process reads this evidence. The lab uses its own tag repository and deletes them afterwards.
Helper switch: `S2_REHEARSAL_ONLY=scn_refusals,scn_interruptions python tests/nginx_s2/run_s2.py rehearsal ...`;
`S2_EXPORT_PATCHES=<directory>` (or `clean_clone.py --patches`) exports both commits as patches.

## Isolated Linux (real signals)

```bash
python tests/nginx_s2/clean_clone.py --baseline <full SHA> --linux rehearsal static     # about 40 minutes
```

`--linux` runs the suite **inside a Linux container** over the same Docker daemon: Ubuntu 24.04 (`mcr.microsoft.com/playwright/python:v1.49.0-noble`, GNU bash,
coreutils, git, Python 3.12), the `docker` CLI and the Compose plugin copied from the official image `docker:cli` (pulled once with the owner's consent; `docker pull docker:cli`),
the Docker socket attached inside. Names that the daemon mounts must also be valid for the daemon: everything in the lab lies on a named volume attached at its own
Mountpoint, so the same absolute path is valid both inside the container and for the daemon. The real checkout is not attached, the container receives a copy of the clean clone.
A specific Compose release (e.g. production's 5.3.1): `--compose-binary <file> --compose-sha256 <hex>` replaces the plugin from `docker:cli`; the file is verified by a checksum. Cleanup: `cleanup_lab.py <run id>` (names `b04s2_<id>_linux`, `b04s2_<id>_lxdata`, image tags `b04s2_<id>_evidence/nginx:*`).

## Limitations

* Measured on Docker Desktop (WSL2), not on the school's Linux VM or on the production machine. Latency and kernel behaviour
  (ARP, SYN timeouts) may differ; the "dead IP" outages show that the result fluctuates between rounds (503 after tens of seconds /
  504 after 60 s) equally for both variants.
* The stub is not a real application: it proves the behaviour of nginx, not of the applications (e.g. the SSE of the real `search`, pgAdmin login).
* The rate limit is assessed statistically (the first 429 around request no. 21, tolerance ±3), not by an exact match.
* The script ran in Git Bash (MSYS) on Windows and in an isolated Linux (see above); it was not run on the production machine or on the school's Linux VM. Signals are sent for real (`kill -s`), not by stopping after a phase; the production host may differ in the version of bash, git and Compose.
* The Cloudflare tunnel, AWS security group, production permissions, backups and migrations are not exercised (outside S2).
* The R2 checks (the `CF-Connecting-IP` trust boundary) and R3–R7 from the B04a review are **not addressed** by this S2. The scripts in
  `docs/plans/archive/audit-04a-proposals/verify/` (for S5) still have a false PASS from R4 and are not used for S2.

## Public checkout

Historical derivation of candidates through fixed baseline Git SHAs requires the corresponding
ancestors. A clean public root does not contain the private history. These historical variants
are therefore not a supported test of a new public checkout; they need their own
verified baseline. The installation, runtime configuration and ordinary offline unit tests
do not depend on this historical derivation path.
