# B06-2: host preflight and safe bootstrap handoff

The public copy omits internal acceptance reports and links to them; this proposal is not evidence of the current deployment.

Audit note (SHA c8fe0543): the sentence about "never run against a real Docker" is historical; `host_preflight.py` ran in the `setup.py` wizard
on real VMs ([public-source.md](../releases/public-source.md)), but the final public SHA has no VM run of its own.

Date: 2026-10-07. **Status: candidate implemented offline only, verified only with stand-in objects (fake Host). It has never been run against a real Docker.**
Builds on the [B06 proposal](b06-clean-install-proposal.md) (sections 2, 4, 5, 6, 8, 10) and the shared layout `scripts/eduai_layout.py`. The scope is **first installation**;
supported updates are not included ([B06-U](b06-upgrade-strategy.md)). None of this is a deployment approval.

The script [scripts/host_preflight.py](../../scripts/host_preflight.py) is standalone: `scripts/install.py` stays offline and does not call Docker. The script uses only the standard library;
tests are [tests/test_host_preflight.py](../../tests/test_host_preflight.py) and [tests/host_preflight_fixtures.py](../../tests/host_preflight_fixtures.py).

## 1. Commands

```text
host_preflight.py check   LAYOUT [--min-free-gb N] [--min-compose X.Y.Z] [--reserved CIDR ...] [--refuse-pulled-images] [--accept-unmeasured-network-pools]
host_preflight.py handoff LAYOUT [same options] [--env-link-only] [--exec]
host_preflight.py pin-env LAYOUT [--tunnel-subnet CIDR | --allocate-tunnel-subnet] [--reserved CIDR ...] [--set NAME=VALUE ...]
```

`LAYOUT` is the options `--source-root --config-dir --state-dir --project` or the `EDU_AI_*` variables (as in `eduai_layout`: flag > variable > default). The configuration must be **outside the checkout**.
The output is lines `CODE: LEVEL detail` (`OK`, `NOTE`, `REFUSED`, `UNVERIFIED`), then `RESULT: ...` and always `INSTALLATION_NOT_STARTED`. Exit codes: **0** nothing found,
**2** `REFUSED` (proven conflict), **3** `UNVERIFIED` (cannot be proven). Whatever cannot be read or parsed is `UNVERIFIED`, never "free"; the output never says "ready", "free" or "installed" about an unverified state.
Only names, paths and counts are printed: `.env` values, keys and tokens are not, and the output of child processes is not forwarded.

## 2. What `check` verifies

| Safeguard | Line code | Refused (2) | Unverified (3) |
|---|---|---|---|
| layout (`eduai_layout.validate`, missing links allowed) | `LAYOUT_*` | conflict, wrong link target, directory in the checkout, configuration in the checkout (`CONFIG_NOT_EXTERNAL`) | unreadable name |
| G9 architecture | `ARCH_HOST`, `ARCH_DOCKER` | `uname -m` or the daemon is not x86_64/amd64 | unreadable or unparseable |
| daemon, context | `DOCKER_DAEMON`, `DOCKER_CONTEXT`, `DOCKER_OS`, `DOCKER_TARGET` | OSType other than linux | `docker info` fails, `DOCKER_HOST`/`DOCKER_CONTEXT` other than the default, client context not proven to be the default |
| Compose | `COMPOSE_VERSION` | below the minimum | version not parsed |
| inherited variables | `INHERITED_ENV` | | `COMPOSE_*`, `DOCKER_CONFIG`... and variables that `docker-compose.yml` interpolates and that would override `.env` (names only) |
| external set (G2, K2) | `CONFIG_DIR`, `CONFIG_FILES`, `KEY_FINGERPRINT` | incomplete set, foreign items, directory is not 0700, files are not 0600, foreign owner, fingerprint `MISMATCH` | unreadable file, `UNVERIFIABLE` fingerprint, modes on a platform without them |
| pins in `.env` (G3, G4) | `COMPOSE_FILE`, `PROJECT`, `TUNNEL_SUBNET_PIN` | `COMPOSE_FILE` not pinned (otherwise `docker-compose.override.yml` would be loaded and publish development ports) or names an override; project differs from the flag; invalid subnet | `EDU_AI_TUNNEL_SUBNET` missing (the model requires it at this SHA), project unknown |
| model | `MODEL`, `MODEL_OVERRIDE`, `TUNNEL_SUBNET_MODEL` | override ports in the model, model subnet ≠ pin | `docker compose -p P --project-directory SRC --env-file CFG/.env config --format json` cannot be rendered or has a different shape |
| G1/G3 existing resources | `EXISTING_CONTAINERS`, `_VOLUMES`, `_NETWORKS`, `_IMAGES` | a name from the model or the label `com.docker.compose.project=<project>`; an image tag that the installation builds | the listing cannot be parsed, model missing (labels only) |
| G1 data | `STATE_DIR` | non-empty `state_dir`, `langflow_data` and others, foreign items, symlink, parent missing | unreadable listing |
| G4 ports | `PORTS`, `PORTS_UNLISTED` | listener on any address (v4 and v6) on a port the model publishes | `ss` missing, fails or has a line that is not understood; UDP ports |
| G5 tunnel subnet | `TUNNEL_SUBNET` | `tunnel_subnet.py check` exits 2 | exits 3 or otherwise |
| G5 other networks | `NETWORK_POOLS` | | always, until the operator records with the option `--accept-unmeasured-network-pools` that they know about the unmeasured behaviour (then `NOTE`) |
| disk | `DISK_DOCKER_ROOT`, `DISK_STATE_DIR` | below the threshold | Docker root inside a VM (Docker Desktop), cannot stat, path missing |

Ports, container, network and volume names and image tags are taken from the rendered model; none of it is hard-coded. A public image that is already on the host is only a `NOTE` (`IMAGES_PRESENT_PULLED`)
unless you pass `--refuse-pulled-images`; tags that the installation builds itself (`build:`) do count as a collision.

**Provisional values (not measured):** Compose minimum `2.24.0` (derived from the features used, e.g. `configs.content`; only version 5.3.1 has been verified) and a disk threshold of `20 GiB` for both the Docker root and `state_dir`
(`--min-compose`, `--min-free-gb`). The production host had 96 GB and 51.7 GB of build cache (audit-04a); the actual need was not measured.

**Not measured and explicitly acknowledged:** collisions of blocks from the Docker pools (5 automatically allocated networks: `edge`, `app`, `data`, `bifrost_egress`, `tunnel_egress`) with host routes, LAN and VPN.
Nobody has measured Docker's behaviour in such a collision; `tunnel` is covered by `tunnel_subnet.py check` (all routing tables and interface addresses), the other networks are not.

## 3. Command safety

The only entry into the machine is the `Host` object. A fixed list of read commands is allowed: `docker info`, `docker compose version --short`, `docker ps -a`, `docker volume ls`, `docker network ls`, `docker image ls`
(all with `--format {{json .}}`), `docker compose -p P --project-directory SRC --env-file ENV config --format json`, `ss -H -ltn`, `uname -m` and `scripts/tunnel_subnet.py check|allocate` only with `--routes host`
(`--routes docker` would start a container). Any other command raises `Forbidden` before it runs; the words `down`, `rm`, `prune`, `-v`, `--remove-orphans`, `up`, `build`, `run`, `exec` and others are forbidden
even as an argument. Child processes receive a fixed environment (without inherited `DOCKER_*`, `COMPOSE_*` and model variables). The tests guard the list, the fixed environment and the source (AST: no `unlink`, `rmtree`, `rename`, `replace`; processes are started in only two functions).

## 4. `handoff`

1. Runs `check`. Anything other than exit code 0 (both REFUSED and UNVERIFIED) means **nothing is created** (`HANDOFF: NOTE not performed`).
2. On a clean `check` it creates only **missing** names: `state_dir` (the parent must exist), the subdirectories `uploads`, `langflow_data`, `sqllite_export`, `bifrost_data` with the modes from `bootstrap.DATA_DIRS`
   (the owner uid:gid is set by the bootstrap step) and the symlinks in the checkout (`.env` and the state directories) via `os.symlink`, which fails on an existing name. It replaces and deletes nothing;
   if it fails midway, what was created stays valid and the next run completes the rest.
3. It re-verifies the layout with the links required and prints the exact command: `NEXT_COMMAND: NOTE python3 .../scripts/bootstrap.py --source-root ... --config-dir ... --state-dir ...` (from `Layout.argv()`, no secrets).
4. `--exec` runs `bootstrap.py` with the same options and a fixed environment (it keeps only the variables by which bootstrap recognises CI). The `check` output ends with the line `INSTALLATION_NOT_STARTED` **before** the launch;
   then `BOOTSTRAP_LAUNCHED` and `BOOTSTRAP_EXIT: n` follow. Bootstrap code 3 means "repeat later" (nothing changed), code 1 is an error after a possible partial change (handoff returns 3, not 2).

`--env-link-only`: Compose renders `env_file: .env` relative to the checkout, so the model most likely does not render without the `.env` link (not measured) and `check` stays unverified. The option creates **only** the `.env` link,
and only when there is no `REFUSED` and the only `UNVERIFIED` is the model and what follows from it (or `NETWORK_POOLS`); it then runs `check` again and creates nothing else.

Until nobody has measured Docker's behaviour on a pool collision, `check` and `handoff` pass only with `--accept-unmeasured-network-pools`.

## 5. `pin-env`

It appends to the end of the external `.env` **only the missing** keys (`COMPOSE_FILE=docker-compose.yml`, `COMPOSE_PROJECT_NAME`, `EDU_AI_TUNNEL_SUBNET` from `--tunnel-subnet` or from `tunnel_subnet.py allocate`, and `--set NAME=VALUE`).
An existing key (even an empty one) is never changed, only named (`PIN_ENV_KEPT`, `PIN_ENV_DIFFERS`). No secrets are created: names with `PASSWORD`, `SECRET`, `TOKEN`, `KEY`, `CREDENTIAL` are rejected, and the value must not contain `$` or a line end.
The write is `envfile.write_atomic` (temporary file, `fsync`, `os.replace`); after a failure or interruption `.env` stays byte-for-byte the same and the result is reported according to the disk, not according to the exception. A file with CR, a non-private one or a foreign one is rejected.

## 6. What was not verified and what is needed from the owner

See the handoff report. Summary: none of this has ever been run against a real Docker, Compose or `ss`; the behaviour of `--env-file` with `COMPOSE_FILE`, rendering the model without the `.env` link,
the shape of `docker info`/`ps`/`network ls --format {{json .}}` and the thresholds are assumptions from the documentation and stand-in objects. Command and tests: `python tests/test_host_preflight.py` (Linux fully, Windows skips the tests with links and modes).
