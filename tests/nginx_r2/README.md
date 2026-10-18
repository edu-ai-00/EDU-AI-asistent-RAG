# nginx R2: lab for the trust boundary of proxy headers

Provides evidence for R2, variant 2 (implemented in this tree in `docker-compose.yml` and `nginx/conf`; deployment on a specific host cannot be verified from the repository) ([proposal](../../docs/plans/r2-trust-boundary-proposal.md)):
whom nginx may trust for the client address (`CF-Connecting-IP`) and which parts of `X-Forwarded-*` may reach the applications; how the subnet of the
trusted network is allocated to an installation and verified; how the lab is safely cleaned up.
The lab (`r2lab.py`) does not modify `docker-compose.yml`, `nginx/conf` or any running stack.
**No access to production, no `.env`, keys, tunnel credentials or LLM.**

The implementation candidate lies in three separate configuration commits (steps 1 to 3) and in the operational tools `scripts/nginx_r2_switch.sh`
and `scripts/tunnel_subnet.py` (see the runtime sources in this checkout). This folder is their lab and rehearsal:
`candidate.py verify` shows that the step commits are byte-for-byte derived variants of the proposal, `rehearsal_linux.py` exercises the real script on
the real commits (introduction, refusal, failure, interruption, return), `host_iproute2.py` reads routes with a real iproute2.

## Files

| File | Purpose |
|---|---|
| `candidate.py` | derives the proposal from the **deployed S2 commit** (`S2_BASE`, not from HEAD) by exact computed substitutions (nothing is edited by hand); one source of CIDR; variants and deliberately wrong mutants; `render`, `check` (without Docker); `apply --step N` writes the files of a step, `verify` shows that the three step commits are exactly the derived variants `transition`, `transition2`, `candidate` |
| `tunnel_subnet.py` | only a stand-in: the operational tool is [`scripts/tunnel_subnet.py`](../../scripts/tunnel_subnet.py), **one implementation** (code 0 free, 2 rejected, 3 cannot be verified; the caller continues only on 0; an unreadable Docker pool is 3; checks of the effective Compose model `model-subnet`, `model-check`, `render-trust`, `wiring`, `image`). Import and execution from here keep working |
| `ownership.py` | ownership of lab resources by labels and the cleanup that relies on it (`cleanup_run`, `safe_remove`) |
| `offline_tests.py` | tests of the previous three and of `switch_offline.py` without Docker, with a fake Docker and a fake host (the helper-container script runs in a **real POSIX shell**, only `ip` is fake; `sh` is needed, on Windows it is provided by Git for Windows; without it the test is `ERROR`, never PASS); also run by `r2lab.py selfcheck` |
| `switch_offline.py` | judgements of the scripts **`scripts/nginx_r2_switch.sh`** and **`scripts/nginx_s2_guard.sh`** without a daemon (bash, git and a real `docker compose config` client; `docker` is `fakedocker.py`): the exact model change of step 2 (R2-R4), the scope of the commit and of the fast advance, the return inputs (R2-R5), the S2 return safeguard (it also refuses an unverifiable state: failure of `docker inspect`, of the container list, an unreadable file; a provably missing container does not block the restore); the first assessed versions (`59c1e02` script, `8751bff` safeguard) are run against the same cases and must accept them, the fixed ones refuse them |
| `fakedocker.py` | fake `docker` for the previous one: `compose config` goes to the real client (with a cache), other commands are answered by a small "world" in JSON, every call is written down and marked as mutating; an unknown command ends with 99 |
| `r2lab.py` | proposal lab: `selfcheck`, `static`, `run`, `cleanup --run-id` |
| `rehearsal_linux.py` | runs the rehearsal inside an isolated Linux (Ubuntu 24.04, GNU bash, real signals, **Compose 5.3.1** with a verified sha256) against the same Docker daemon; the candidate is the real commits (`--tools --step1 --step2 --step3`) or the working tree |
| `rehearsal.py` | the rehearsal itself: seven scenarios (introduction of all steps and return, return from each phase, refusal before a change, failure during a step, interruption by signals, two installations, continuity with the S2 return) with the **real** `scripts/nginx_r2_switch.sh` on disposable clones |
| `host_iproute2.py` | the `--routes host` mode with a **real iproute2** in an isolated namespace (`unshare -Urn`): policy table 51820, bridge-like interface, real `ip` errors |
| `egress_check.py` | outbound path of a container on the networks [internal, internal, egress] (like cloudflared after step 2): a default route from the ordinary network, none without it; reaching the Cloudflare edge is only an observation (no token, no tunnel protocol; without internet `NOT_RUN`, never PASS) |
| `asgi_echo.py` | ASGI application for a real Uvicorn |

## What is tested

| Variant | Content |
|---|---|
| `baseline` | the files that production actually runs (read from git, `--ref`, default `HEAD`) |
| `candidate` | the proposal: network `tunnel`, `EDU_AI_TUNNEL_SUBNET` as the single source of CIDR for both the network and the nginx trust, `X-Forwarded-For` = `$remote_addr` |
| `transition`, `transition2` | deployment steps (the old range still trusted; compose old, then new) |
| `mut_broad`, `mut_notrust`, `mut_xff`, `mut_literal`, `mut_cidr` | deliberately wrong proposals (trust widened again, no trust, XFF preserved, a second constant in `nginx.conf`, a different literal in the configuration); the corresponding check must detect them |

Further live scenarios: `subnet.*` (allocation and preflight against a real daemon, refusal of collisions; `subnet.read.*`: reading all routing tables with a real helper image, the policy table in an isolated namespace of a disposable container, a real failure to read routes or addresses via `ip` in a container, a repeated preflight of the installation's own running instance), `multi.*` (two concurrent installations with different projects and their own non-overlapping
subnets), `cleanup.*` (cleanup of one run while preserving foreign and other runs), `final.nothing_else_changed` (everything outside the lab is the same after the run as before it).

## Rehearsal of introduction and return

```bash
python tests/nginx_r2/candidate.py verify --repo . --step1 <SHA> --step2 <SHA> --step3 <SHA>      # step commits = derived variants
python tests/nginx_r2/rehearsal_linux.py --compose-binary docker-compose-linux-x86_64 --compose-sha256 <hex> --out DIR \
    --tools <SHA> --step1 <SHA> --step2 <SHA> --step3 <SHA> [--only forward_return,refusals,...]       # approx. 40 minutes, Docker
wsl -d Ubuntu -e unshare -Urn python3 tests/nginx_r2/host_iproute2.py --out FILE                       # real iproute2, no Docker
python tests/nginx_r2/egress_check.py --out FILE                                                    # outbound path of a container, Docker
```

`--tools` is the commit the operator copies the scripts from; it must be a descendant of S2 but need not precede step 1 (the fixed script lies after the last step; the step variants hang off `step1^`).
The rehearsal needs Docker, the local image `nginx:1.27-alpine` (exactly the production one), `python:3.12-slim`, `mcr.microsoft.com/playwright/python:v1.49.0-noble` (executor)
and `docker:cli` (only as the source of the `docker` client). The Compose binary is passed explicitly and verified by a checksum (production: 5.3.1). The rehearsal server is a clone
of the release (deployed S2 + tools + three step commits) with the real `docker-compose.yml`; the lab override only swaps eight services for stubs, cloudflared for a
stand-in (it logs routes in the format of the real log and is the client from which `CF-Connecting-IP` is sent) and the container names. **The stand-in is not evidence of real
Cloudflare operation**; neither a token nor Access is used. The offline tests and `selfcheck` need `sh` (on Windows, Git for Windows).

## What is needed

| Requirement | Note |
|---|---|
| Docker Engine + Compose v2 ≥ 2.24 (`!override`; `configs` with `content` from 2.23.1) | nothing is published to the host; the lab resources carry the labels `edu_ai.purpose`, `edu_ai.run`, `edu_ai.project` |
| image `nginx:1.27-alpine` **exactly as production** (`sha256:65645c7bb6a0…`) | otherwise the precondition fails (instructions in [tests/nginx_s2/README.md](../nginx_s2/README.md)); also used as a helper container for reading the daemon's routes |
| image `python:3.12-slim` | synthetic upstreams and clients (`stub.py` and `client.py` from the S2 lab are reused) |
| optionally a local image with Uvicorn | `--asgi-image LABEL=IMAGE`; without it the ASGI part is `NOT_RUN`, never PASS |
| Python ≥ 3.10, standard library only | |

## Rehearsing a newer nginx image

The evidence above ran on the production image (1.27.5). `docker-compose.yml` may name a newer tag; to run the
same checks against it, set both variables (the ID is the one `docker image inspect <tag> --format '{{.Id}}'` prints):

```bash
EDU_AI_LAB_NGINX_IMAGE=nginx:1.30.6-alpine EDU_AI_LAB_NGINX_ID=sha256:<id> python tests/nginx_r2/r2lab.py run --out r2.json
```

The `env.nginx_image` check then compares against that ID. The rehearsals that check out historical commits
(`rehearsal*.py`, `candidate.py`, `switch_offline.py`, the S2 `--baseline`) need a clone with that history.

## Running

```bash
python tests/nginx_r2/candidate.py check                 # without Docker: derivation of the candidate, one source of CIDR, diffs apply to the baseline
python tests/nginx_r2/offline_tests.py                   # without Docker: cleanup, ownership, subnet allocation and preflight, one source of CIDR, introduction scripts and the S2 safeguard
python tests/nginx_r2/switch_offline.py                  # only the judgements of the scripts (R2-R4, R2-R5, S2 safeguard): needs bash, git and the `docker` client (not the daemon)
python tests/nginx_r2/r2lab.py selfcheck                 # without Docker: the same + failures of probes and comparators must not be PASS
python tests/nginx_r2/r2lab.py static                    # nginx -t, delta of the configuration and of the Compose model, one source of CIDR
python tests/nginx_r2/r2lab.py run --out r2.json \
    --asgi-image api=edu_ai_api:1.0 --asgi-image chat=edu_ai_web_chat:0.1 [--reserved 10.20.0.0/16]
python tests/nginx_r2/r2lab.py cleanup --list-runs       # read-only: run IDs found on labels
python tests/nginx_r2/r2lab.py cleanup --run-id ID [--dry-run]
python tests/nginx_r2/tunnel_subnet.py allocate --project NAME --routes docker --helper-image nginx:1.27-alpine
python tests/nginx_r2/tunnel_subnet.py check 10.x.y.z/29 --project NAME [--reserved CIDR ...]
python tests/nginx_r2/candidate.py render --out DIR      # diffs of the proposal and derived files
```

The exit code of `r2lab.py` is 0 only when **every** check is `PASS`. `FAIL` is a satisfied condition with the opposite result, `ERROR` a check that could not run (probe, Docker,
client), `NOT_RUN` a test that had nothing to run on. `run` prints the **run ID** at the start; after an interruption it is cleaned up with `cleanup --run-id ID`.

## Cleanup: what it may and may not do

`cleanup` **requires a specific run ID** (`MMDDhhmm` + 4 hexadecimal digits; a prefix, a pattern or `*` is refused with code 2). It selects candidates **only by the label `edu_ai.run`**,
never by name. It removes a resource only after verifying the labels: `edu_ai.purpose` = the lab label, `edu_ai.run` = this ID and, for Compose resources, `com.docker.compose.project` equals the project
recorded for this run (`edu_ai.project`) and has the exact form of this run's project. It removes by **ID**, containers before networks before volumes. What is not its own (another run, a foreign network with a similar
name), what cannot be verified (inspection failed) and what carries the labels of this run but with a disagreeing project is **preserved and reported** (code 1). Every other
removal in the lab behaves the same way as the cleanup (`safe_rm`, `compose down` only after verifying all resources of the project); a planned name already held by a foreign resource is **not adopted or deleted** by the lab, but refused.

## Four layers that must not be confused

For each case the following is recorded and compared: the **real TCP sender** (the container address nginx sees; verified by a paired request without the header and the access log),
the **received headers**, the **client address in nginx** (`$remote_addr`, the first field of the access log) and **what the application sees** (an echo upstream and, for ASGI, a real `uvicorn` in a real image:
`scope["client"]`, `scope["scheme"]`).

## Wiring

* nginx is the **real** `nginx` service from the `docker-compose.yml` of the given variant (`docker compose up --no-deps nginx`); only the container name is overridden, no ports to the host, the DNS alias
  `edu_ai_nginx` (in production given by `container_name`) and the lab labels. Networks, subnets, `internal` and `configs` come from the file. Compose runs with an empty `.env` and with a
  **disposable `--env-file` that contains only the `EDU_AI_TUNNEL_SUBNET` of the given installation**; the real `.env` is not read.
* each installation gets a subnet from `tunnel_subnet.py` (`allocate` + `check` against a real daemon) **before** anything is created; a refusal raises an error with no side effects.
* the networks are created by Compose from the file; the remaining networks (`app`, `tunnel_egress`, …) are added by the lab from the effective model with the same labels. An existing network with the planned name is **not used
  or changed**. The counterparts are containers on the same networks: a **member of `edge`** (representing API, Langflow, search, pgAdmin…), a **client in `app` only**, a **cloudflared stand-in** with the networks
  the file assigns to it, six synthetic upstreams with aliases of the names from `nginx.conf`. The stand-ins carry the project label of their installation (it overrides the label inherited from the image built by Compose).
* variants run one at a time; the `multi` scenario holds two installations with different projects simultaneously.
* ASGI: `asgi_echo.py` under a real `uvicorn` from the given image, started as in production (`uvicorn … --host 0.0.0.0 --port …`, without proxy flags),
  once without and once with `FORWARDED_ALLOW_IPS=*` (a negative control: why `*` is not enough).

## Limitations

Docker Desktop (WSL2), not the Linux VM of production; routes and addresses are read through the daemon's namespace (a helper container), not through the Windows host or production Linux; synthetic upstreams,
not real services; pgAdmin (`ProxyFix`) and Langflow are not tested; real cloudflared and Cloudflare are not included (the stand-in is a container with the same network and name); spoofing of the source address
at L2 (raw sockets, ARP) is not tested; the preflight sees only what the host knows. Keep the result of a specific run outside public Git.

## Public checkout

Historical derivation of candidates through fixed baseline Git SHAs requires the corresponding
ancestors. A clean public root does not contain the private history. These historical variants
are therefore not a supported test of a new public checkout; they need their own
verified baseline. The installation and runtime configuration do not depend on it, but `candidate.py check`, `switch_offline.py`,
`offline_tests.py` and `r2lab.py selfcheck` read fixed historical SHAs via `git show` which a clean public checkout does not have;
they fail in it (audit: `candidate.py check` reports `git show af5f491…` failed, `switch_offline.py` 0/16 PASS).
