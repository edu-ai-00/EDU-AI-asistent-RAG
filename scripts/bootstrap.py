#!/usr/bin/env python3
# last change in code by Jan Drozd and Martin Safranek (2026-08-11).
"""Bring a clean checkout up to a running edu_ai stack in one command.

    python3 scripts/bootstrap.py

Every step is idempotent and reports [ok] / [skip] / [fail]; an existing file is
never overwritten. Re-run it any time to top up whatever is missing — after
pulling a change that adds a variable to .env.example, for instance.

What it removes, in order of how much time each used to cost:

  * nginx's silent crash-loop. `.htpasswd` and `auth_keys.conf` are
    gitignored, so a fresh clone has none of them — and Docker answers a missing
    bind-mount source by creating an empty DIRECTORY, so nginx dies with
    `pread() … failed (21: Is a directory)`, which reads like a config error.
    This generates the files, and repairs the directories a previous `up` left
    behind (which needs --force-recreate: the old mount survives a restart).
  * "log into the UI and copy an API key" — see scripts/create_api_key.py.
  * hand-written passwords, and the CHANGE_ME_* placeholders they replace.

Standard library only; everything else needs just Docker.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from create_api_key import ensure_api_key  # noqa: E402
from envfile import is_placeholder, read_env, set_env  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
NGINX = ROOT / "nginx"

# Bind-mount sources that must exist before the first `up`, mapped to the
# uid:gid of the container user that writes there and the mode it needs. Docker
# would create a missing source itself, but root-owned, which those non-root
# users cannot write to. uids are pinned in the Dockerfiles for this reason.
# uploads is shared: the gateway (1000) owns it, Langflow (1001) writes via the
# group, and setgid keeps that group on files either one creates.
# bifrost_data is 750, not 755: config.db holds the provider API keys in the
# clear, so it has no business being world-readable on the host.
DATA_DIRS = {
    "uploads":         (1000, 1001, 0o2775),
    "langflow_data":   (1001, 1001, 0o0755),
    "sqllite_export":  (1001, 1001, 0o0755),
    "pgadmin_exports": (5050, 5050, 0o0755),
    "bifrost_data":    (1000, 1000, 0o0750),
}

def _password() -> str:
    """A strong password safe to put in .env.

    token_urlsafe yields only [A-Za-z0-9_-] — notably no '$', which Compose
    reads inside .env as the start of a variable reference, silently truncating
    the value.
    """
    return secrets.token_urlsafe(24)


def _fernet_key() -> str:
    """A Fernet key: exactly 32 random bytes, url-safe base64, padding included.

    Langflow feeds LANGFLOW_SECRET_KEY straight to Fernet, which rejects
    anything else with "Fernet key must be 32 url-safe base64-encoded bytes" —
    a generic password of the same visible length does not qualify.
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


# Secrets bootstrap can invent. OPENAI_API_KEY is deliberately absent — it comes
# from an external account and cannot be generated, so it stays CHANGE_ME and is
# reported at the end (and by GET /health on edu_ai_search, the only service that
# reads it from the environment; edu_ai_api's /health does not, because it never
# consumes the key).
GENERATED_SECRETS: dict[str, "callable"] = {
    "POSTGRES_PASSWORD": _password,
    "PGADMIN_DEFAULT_PASSWORD": _password,
    "LANGFLOW_SUPERUSER_PASSWORD": _password,
    "LANGFLOW_SECRET_KEY": _fernet_key,
}

# nginx file mounts. A directory here is the crash-loop signature described above.
NGINX_FILES = [NGINX / ".htpasswd", NGINX / "auth_keys.conf"]

# nginx's worker drops to this uid/gid and reads the two files above itself, so
# they are given gid 101 and mode 640 rather than left to the operator's umask.
NGINX_GID = 101
NGINX_FILE_MODE = 0o640

_ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

_summary: list[str] = []
_failed = False
_needs_recreate = False


# ───────────────────────────────────────────────────────────────
#  Output
# ───────────────────────────────────────────────────────────────

def report(status: str, message: str) -> None:
    print(f"  [{status:4s}] {message}")


def fail(message: str, hint: str = "") -> None:
    global _failed
    _failed = True
    report("fail", message)
    if hint:
        print(f"         → {hint}")


def phase(title: str) -> None:
    print(f"\n{title}")


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command in the repo root, capturing output."""
    return subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", **kwargs
    )


def compose(*args: str, stream: bool = False, timeout: int | None = None):
    """Invoke `docker compose` in the repo root."""
    command = ["docker", "compose", *args]
    if stream:
        print(f"         $ {' '.join(command)}")
        # The child writes straight to the terminal; without flushing first our
        # own buffered output lands after it and the log reads out of order.
        sys.stdout.flush()
        return subprocess.run(command, cwd=ROOT, timeout=timeout)
    return run(command, timeout=timeout)


# ───────────────────────────────────────────────────────────────
#  1. Preflight
# ───────────────────────────────────────────────────────────────

def step_preflight() -> None:
    phase("1. preflight")

    result = run(["docker", "compose", "version"])
    if result.returncode != 0:
        fail("docker compose is not available",
             "install Docker Desktop, or the compose plugin on Linux")
        return
    report("ok", result.stdout.strip().splitlines()[0] if result.stdout.strip() else "docker compose present")

    if not ENV_EXAMPLE.is_file():
        fail(f"{ENV_EXAMPLE.name} is missing", "run this from a complete checkout")
        return

    # Port checks only make sense before anything is up; once our own stack holds
    # 80 the check would flag the stack against itself.
    already_running = bool(compose("ps", "-q").stdout.strip())
    if already_running:
        report("skip", "port check (stack is already running)")
    else:
        import socket

        busy = []
        for port in (80, 8443, 8444):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.4)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    busy.append(port)
        if busy:
            report("warn", f"ports already in use: {busy} — nginx may fail to bind")
        else:
            report("ok", "ports 80/8443/8444 are free")

    _check_pg_search()


# The extension file the ParadeDB .deb installs; Dockerfile.postgres puts it there.
PG_SEARCH_CONTROL = "/usr/share/postgresql/16/extension/pg_search.control"


def _check_pg_search() -> None:
    """Refuse to continue on a postgres image that predates pg_search.

    Two ways a stale image bites, and the second one is sticky: compose starts
    postgres with `-c shared_preload_libraries=pg_search`, which is fatal when the
    library is absent, and init_clean does `CREATE EXTENSION pg_search`, which
    aborts the baseline halfway. init only ever runs on an EMPTY volume, so the
    retry skips it silently and leaves a half-built schema that looks initialised.
    `--build` is opt-in here, and `up` reuses an existing image, so an old
    edu_ai_postgres from before Dockerfile.postgres gained the .deb survives both.
    """
    result = compose("config", "--format", "json")
    if result.returncode != 0:
        report("warn", "could not read compose config — skipped the pg_search check")
        return
    try:
        image = json.loads(result.stdout)["services"]["postgres"]["image"]
    except (ValueError, KeyError):
        report("warn", "could not resolve the postgres image — skipped the pg_search check")
        return

    if run(["docker", "image", "inspect", image]).returncode != 0:
        report("ok", f"{image} not built yet — compose will build it with pg_search")
        return

    probe = run(["docker", "run", "--rm", "--entrypoint", "sh", image,
                 "-c", f"test -f {PG_SEARCH_CONTROL}"])
    if probe.returncode == 0:
        report("ok", f"{image} has pg_search")
    else:
        fail(f"{image} exists but has no pg_search — postgres will not start",
             f"rebuild it first: docker compose build postgres    "
             f"(or re-run with --build). If a first start already failed on this, "
             f"the volume is half-initialised: remove it before retrying.")


# ───────────────────────────────────────────────────────────────
#  2. .env
# ───────────────────────────────────────────────────────────────

def step_env() -> None:
    phase("2. .env")

    created = not ENV_PATH.is_file()
    if created:
        shutil.copyfile(ENV_EXAMPLE, ENV_PATH)
        report("ok", f"created .env from {ENV_EXAMPLE.name}")
    else:
        report("skip", ".env already exists — filling gaps only")

    example = read_env(ENV_EXAMPLE)
    current = read_env(ENV_PATH)
    updates: dict[str, str] = {}

    # Carry over keys added to .env.example since this .env was written, so an
    # upgrade never needs a manual diff of the two files.
    for key, value in example.items():
        if key not in current:
            updates[key] = value
            report("ok", f"added missing key {key}")

    for key, generate in GENERATED_SECRETS.items():
        if is_placeholder(updates.get(key, current.get(key))):
            updates[key] = generate()
            report("ok", f"generated {key}")
        else:
            report("skip", f"{key} already set")

    if updates:
        set_env(ENV_PATH, updates)

    final = read_env(ENV_PATH)
    if is_placeholder(final.get("OPENAI_API_KEY")):
        report("warn", "OPENAI_API_KEY is still CHANGE_ME — containers will start, "
                       "but ingestion cannot reach OpenAI")
        _summary.append(
            "OPENAI_API_KEY is not set. Put a real key in .env, then "
            "`docker compose up -d`. GET /health reports it under missing_config."
        )


# ───────────────────────────────────────────────────────────────
#  3. Directories
# ───────────────────────────────────────────────────────────────

def step_dirs() -> None:
    phase("3. directories")
    for name in DATA_DIRS:
        path = ROOT / name
        if path.is_dir():
            report("skip", f"{name}/")
        else:
            path.mkdir(parents=True, exist_ok=True)
            report("ok", f"created {name}/")
    _fix_data_dir_ownership()


def _fix_data_dir_ownership() -> None:
    """Give each bind mount the uid:gid of the container that writes to it.

    Creating them above is not enough: they end up owned by whoever ran this
    script, which is none of the container users. Not recursive — a directory's
    own ownership is what governs creating files inside it.

    Linux only. Docker Desktop translates bind-mount ownership, so there is
    nothing to fix and a chown would be actively wrong. Done in a throwaway
    container because chown needs root and nothing here assumes sudo.
    """
    if sys.platform != "linux":
        report("skip", "ownership (Docker Desktop maps uids itself)")
        return

    wrong = []
    for name, (uid, gid, mode) in DATA_DIRS.items():
        info = (ROOT / name).stat()
        if info.st_uid != uid or info.st_gid != gid or (info.st_mode & 0o7777) != mode:
            wrong.append(f"chown {uid}:{gid} '/mnt/{name}' && chmod {mode:o} '/mnt/{name}'")

    if not wrong:
        report("skip", "ownership (already correct)")
        return

    result = run(["docker", "run", "--rm", "-v", f"{ROOT}:/mnt",
                  "alpine:3", "sh", "-c", " && ".join(wrong)])
    if result.returncode == 0:
        report("ok", f"set ownership on {len(wrong)} director{'y' if len(wrong) == 1 else 'ies'}")
    else:
        fail(f"could not set ownership: {(result.stderr or result.stdout).strip()[:200]}",
             "the containers will start but cannot write to their bind mounts. "
             f"Fix by hand: sudo chown <uid>:<gid> {' '.join(DATA_DIRS)}")


# ───────────────────────────────────────────────────────────────
#  4. nginx secrets
# ───────────────────────────────────────────────────────────────

def apr1(password: str, salt: str) -> str:
    """Apache's MD5 crypt ($apr1$), the format nginx's auth_basic_user_file wants.

    Implemented here rather than shelling out to `openssl passwd -apr1` so that
    generating .htpasswd needs nothing but Python — one less thing to be missing
    on the machine doing the deployment.
    """
    pw = password.encode()
    sb = salt.encode()

    ctx = hashlib.md5(pw + b"$apr1$" + sb)
    final = hashlib.md5(pw + sb + pw).digest()

    length = len(pw)
    while length > 0:
        ctx.update(final[: min(length, 16)])
        length -= 16

    length = len(pw)
    while length:
        ctx.update(b"\0" if length & 1 else pw[:1])
        length >>= 1
    final = ctx.digest()

    for i in range(1000):
        round_ctx = hashlib.md5()
        round_ctx.update(pw if i & 1 else final)
        if i % 3:
            round_ctx.update(sb)
        if i % 7:
            round_ctx.update(pw)
        round_ctx.update(final if i & 1 else pw)
        final = round_ctx.digest()

    encoded = ""
    for a, b, c in ((0, 6, 12), (1, 7, 13), (2, 8, 14), (3, 9, 15), (4, 10, 5)):
        value = (final[a] << 16) | (final[b] << 8) | final[c]
        for _ in range(4):
            encoded += _ITOA64[value & 0x3F]
            value >>= 6
    value = final[11]
    for _ in range(2):
        encoded += _ITOA64[value & 0x3F]
        value >>= 6

    return f"$apr1${salt}${encoded}"


def _clear_stale_directory(path: pathlib.Path) -> bool:
    """Remove the empty directory Docker leaves where a missing file mount was.

    Returns True if something was removed — the caller then needs
    --force-recreate, because a container keeps the mount it was created with.
    """
    if not path.is_dir():
        return False
    try:
        path.rmdir()
    except OSError:
        fail(f"{path} is a non-empty directory where a file belongs",
             "inspect and remove it by hand, then re-run")
        return False
    report("ok", f"removed the empty directory Docker created at {path.name}")
    return True


def step_nginx() -> None:
    global _needs_recreate
    phase("4. nginx secrets")

    NGINX.mkdir(parents=True, exist_ok=True)

    for path in NGINX_FILES:
        if _clear_stale_directory(path):
            _needs_recreate = True

    # .htpasswd — Basic auth on /api/
    htpasswd = NGINX / ".htpasswd"
    if htpasswd.is_file():
        report("skip", ".htpasswd already exists")
        # The stored value is a one-way hash, so a re-run cannot tell the
        # operator what the password is. Say so, rather than letting the
        # summary's silence read as "nothing to write down".
        _summary.append(
            "nginx /api/ Basic auth  →  kept the existing nginx/.htpasswd; its "
            "password is hashed and cannot be recovered. Delete the file and "
            "re-run to issue a new one."
        )
    else:
        password = secrets.token_urlsafe(18)
        salt = "".join(secrets.choice(_ITOA64) for _ in range(8))
        htpasswd.write_text(f"admin:{apr1(password, salt)}\n", encoding="utf-8", newline="\n")
        report("ok", "generated .htpasswd (user: admin)")
        _summary.append(f"nginx /api/ Basic auth  →  admin : {password}")

    # auth_keys.conf — Bearer key map for /search/
    auth_keys = NGINX / "auth_keys.conf"
    if auth_keys.is_file():
        report("skip", "auth_keys.conf already exists")
        _summary.append(
            "/search/ API key        →  kept the existing nginx/auth_keys.conf "
            "(the key is readable in that file)."
        )
    else:
        search_key = secrets.token_urlsafe(32)
        auth_keys.write_text(
            "# Generated by scripts/bootstrap.py. NOT committed.\n"
            "# Rotate by adding a new line, then removing the old one.\n"
            "map $http_authorization $search_authorized {\n"
            "    default                    0;\n"
            f'    "Bearer {search_key}"    1;\n'
            "}\n",
            encoding="utf-8", newline="\n",
        )
        report("ok", "generated auth_keys.conf")
        _summary.append(f"/search/ API key        →  Authorization: Bearer {search_key}")

    _ensure_nginx_readable()


def _ensure_nginx_readable() -> None:
    """Let nginx's worker read the two mounted secret files.

    It runs as uid/gid 101, not root, so mode 600 — the natural instinct for a
    file holding a password — earns EACCES. nginx then answers 500 for /api/ and
    401 for every /search/ key, logging a crit that reads like a config error.
    """
    global _needs_recreate

    if sys.platform != "linux":
        report("skip", "nginx file modes (Docker Desktop maps uids itself)")
        return

    wrong = [p for p in NGINX_FILES
             if p.stat().st_gid != NGINX_GID
             or (p.stat().st_mode & 0o777) != NGINX_FILE_MODE]
    if not wrong:
        report("skip", "nginx files readable by the worker")
        return

    script = " && ".join(
        f"chown :{NGINX_GID} '/n/{p.name}' && chmod {NGINX_FILE_MODE:o} '/n/{p.name}'"
        for p in wrong)
    result = run(["docker", "run", "--rm", "-v", f"{NGINX}:/n",
                  "alpine:3", "sh", "-c", script])
    if result.returncode == 0:
        report("ok", f"made {len(wrong)} nginx file(s) readable by gid {NGINX_GID}")
        _needs_recreate = True
    else:
        fail(f"could not fix nginx file modes: {(result.stderr or result.stdout).strip()[:200]}",
             f"nginx will 500 on /api/. Fix by hand: sudo chgrp {NGINX_GID} "
             f"nginx/.htpasswd nginx/auth_keys.conf && sudo chmod 640 nginx/.htpasswd "
             f"nginx/auth_keys.conf")


# ───────────────────────────────────────────────────────────────
#  5./7. Bringing the stack up
# ───────────────────────────────────────────────────────────────

def step_up(services: list[str], label: str, timeout: int = 1800, build: bool = False,
            apply_pending_recreate: bool = False) -> bool:
    """`up -d --wait` — a real readiness gate now that every service has a
    healthcheck, so no sleeps or polling loops are needed anywhere.

    `apply_pending_recreate` belongs only to a call that covers EVERY service.
    A repaired mount or a new API key can concern a service this particular
    call does not name (nginx, edu_ai_api), and --force-recreate only affects
    the services listed — spending the flag on a partial `up` would leave the
    service that actually needed it running with the stale mount.
    """
    global _needs_recreate
    phase(label)

    args = ["up", "-d", "--wait", "--wait-timeout", "900"]
    if build:
        # Compose builds a missing image on its own; this forces a rebuild of
        # images that exist but predate a local source change.
        args.append("--build")
    if apply_pending_recreate and _needs_recreate:
        args.append("--force-recreate")
        report("ok", "using --force-recreate (a bind mount or .env value changed)")
        _needs_recreate = False
    args += services

    try:
        result = compose(*args, stream=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        fail(f"`docker compose {' '.join(args)}` timed out after {timeout}s",
             "check `docker compose ps` and `docker compose logs`")
        return False

    if result.returncode != 0:
        fail(f"`docker compose {' '.join(args)}` exited {result.returncode}",
             "check `docker compose logs` for the failing service")
        return False

    report("ok", f"{', '.join(services) if services else 'all services'} healthy")
    return True


# ───────────────────────────────────────────────────────────────
#  6. Langflow API key
# ───────────────────────────────────────────────────────────────

def langflow_url(override: str | None) -> str | None:
    """Find a Langflow URL reachable from the host, or None.

    Flag, then LANGFLOW_URL from .env, then the port Compose published, then the
    dev default. On a server 7860 is not published — nginx:8444 proxies the same
    REST API, so LANGFLOW_URL points there.
    """
    candidates = []
    if override:
        candidates.append(override.rstrip("/"))
    else:
        from_env = read_env(ENV_PATH).get("LANGFLOW_URL", "").strip()
        if from_env:
            candidates.append(from_env.rstrip("/"))
        published = compose("port", "edu_ai", "7860").stdout.strip()
        if published:
            # "0.0.0.0:7860" — 0.0.0.0 is a bind address, not a destination.
            port = published.rsplit(":", 1)[-1]
            candidates.append(f"http://127.0.0.1:{port}")
        candidates.append("http://127.0.0.1:7860")

    for candidate in candidates:
        try:
            with urllib.request.urlopen(f"{candidate}/health", timeout=5) as response:
                if response.status == 200:
                    return candidate
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return None


def step_api_key(url_override: str | None, allow_prompt: bool) -> str | None:
    phase("6. Langflow API key")

    base_url = langflow_url(url_override)
    if base_url is None:
        fail("Langflow is not reachable from the host",
             "7860 is published only by docker-compose.override.yml (dev). Where "
             "that override is not loaded, set LANGFLOW_URL=http://127.0.0.1:8444 "
             "in .env — nginx proxies the same API — or pass --langflow-url.")
        return None
    report("ok", f"Langflow reachable at {base_url}")

    try:
        api_key, outcome = ensure_api_key(base_url, ENV_PATH, allow_prompt=allow_prompt)
    except RuntimeError as exc:
        fail(str(exc))
        return None

    report({"skip": "skip"}.get(outcome, "ok"), {
        "skip": "LANGFLOW_API_KEY in .env already works",
        "created": f"created a new API key ({api_key[:8]}…) and wrote it to .env",
        "manual": "stored the pasted key in .env",
    }[outcome])

    if outcome != "skip":
        # env is read at container creation, so the API must be recreated to see it.
        global _needs_recreate
        _needs_recreate = True
    return api_key


# ───────────────────────────────────────────────────────────────
#  7b. Langflow connectivity variables
# ───────────────────────────────────────────────────────────────
# WHY THIS EXISTS: the ingestion flows bind their pg_* fields to Langflow
# variables by name (load_from_db). Langflow resolves such a field as
#   DB global variable  →  os.getenv(name)
# but the os.getenv fallback is a PER-RUN flag, not a setting:
#   * API run  (langflow/services/flow/flow_runner.py) passes
#     fallback_to_env_vars=True  → env works, ingestion via the gateway is fine
#   * WEB UI build (langflow/api/build.py) leaves the parameter at its default
#     False → the field raises "POSTGRES_DB variable not found." and the flow
#     cannot be run or debugged from the Langflow UI at all.
# No env var (LANGFLOW_FALLBACK_TO_ENV_VAR included) changes the UI path — the
# only alternatives were (a) create these as real Langflow variables, or
# (b) patch Langflow's own build.py inside site-packages. We deliberately chose
# (a): patching upstream code would have to be re-verified on every Langflow
# upgrade, while a variable is just data.
#
# NON-SECRET ONLY, and deliberately of type Generic: variables sourced from the
# environment via LANGFLOW_VARIABLES_TO_GET_FROM_ENVIRONMENT are always created
# as Credential, and a Credential value is REJECTED by a non-secret StrInput
# ("Cannot use a Credential-typed global variable in ..."). The two real secrets
# (POSTGRES_PASSWORD, OPENAI_API_KEY) therefore stay env-sourced and are NOT
# created here.

CONNECTIVITY_VARS = (
    "POSTGRES_DB_SERVER",
    "POSTGRES_SERVER_PORT",
    "POSTGRES_DB",
    "POSTGRES_DB_SCHEMA",
    "POSTGRES_DB_USER",
)


def step_langflow_vars(base_url: str, api_key: str) -> None:
    phase("7b. Langflow connectivity variables")

    values = read_env(ENV_PATH)
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    try:
        request = urllib.request.Request(f"{base_url}/api/v1/variables/",
                                         headers={"x-api-key": api_key})
        with urllib.request.urlopen(request, timeout=15) as response:
            existing = {item["name"] for item in json.loads(response.read())}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        fail("could not list Langflow variables", str(exc)[:200])
        return

    created, skipped, missing = [], [], []
    for name in CONNECTIVITY_VARS:
        if name in existing:
            skipped.append(name)
            continue
        value = values.get(name)
        if not value or is_placeholder(value):
            missing.append(name)
            continue
        payload = json.dumps({"name": name, "value": value,
                              "type": "Generic", "default_fields": []}).encode()
        try:
            request = urllib.request.Request(f"{base_url}/api/v1/variables/",
                                             data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=15):
                created.append(name)
        except (urllib.error.URLError, OSError) as exc:
            fail(f"could not create Langflow variable {name}", str(exc)[:200])
            return

    if created:
        report("ok", f"created {len(created)} Generic variable(s): {', '.join(created)}")
    if skipped:
        report("skip", f"{len(skipped)} already present")
    if missing:
        report("fail", f"not in .env: {', '.join(missing)} — flows will fail in the web UI")


# ───────────────────────────────────────────────────────────────
#  8. Flows
# ───────────────────────────────────────────────────────────────

def step_flows(base_url: str, api_key: str) -> None:
    phase("8. import flows")
    environment = {**os.environ, "LANGFLOW_URL": base_url, "LANGFLOW_API_KEY": api_key}
    result = run([sys.executable, "scripts/import_flows.py"], env=environment)
    if result.returncode != 0:
        fail("import_flows.py failed", (result.stderr or result.stdout).strip()[:300])
        return
    for line in result.stdout.strip().splitlines():
        if line.strip():
            print(f"         {line}")
    report("ok", "flows imported")


# ───────────────────────────────────────────────────────────────
#  9. Smoke test
# ───────────────────────────────────────────────────────────────

def step_smoke(base_url: str) -> None:
    phase("9. smoke test")
    result = run([sys.executable, "scripts/smoke.py", "--langflow-url", base_url])
    for line in result.stdout.strip().splitlines():
        print(f"         {line}")
    if result.returncode != 0:
        fail("smoke test reported failures", (result.stderr or "").strip()[:300])
    else:
        report("ok", "smoke test passed")


# ───────────────────────────────────────────────────────────────
#  10. Summary
# ───────────────────────────────────────────────────────────────

def step_summary(base_url: str | None) -> None:
    env = read_env(ENV_PATH)
    print("\n" + "─" * 70)
    print("edu_ai is up." if not _failed else "bootstrap finished WITH ERRORS.")
    print("─" * 70)

    print("\nURLs")
    for label, url in [
        ("Langflow UI", base_url or "http://127.0.0.1:7860"),
        ("Ingestion API", "http://127.0.0.1:8080/"),
        ("API health", "http://127.0.0.1:8080/health"),
        ("pgAdmin", "http://localhost:8443/pgadmin/"),
        ("Web chat", "http://localhost/chat/"),
    ]:
        print(f"  {label:16s} {url}")

    print("\nCredentials")
    print(f"  {'Langflow':16s} {env.get('LANGFLOW_SUPERUSER', 'admin')} : "
          f"{env.get('LANGFLOW_SUPERUSER_PASSWORD', '?')}")
    print(f"  {'pgAdmin':16s} {env.get('PGADMIN_DEFAULT_EMAIL', '?')} : "
          f"{env.get('PGADMIN_DEFAULT_PASSWORD', '?')}")

    if _summary:
        print("\nShown once — store these now")
        for line in _summary:
            print(f"  {line}")

    print("\nThese values live in .env, which is gitignored and never leaves this machine.")


# ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bring a clean edu_ai checkout up to a running stack.")
    parser.add_argument("--langflow-url", default=None,
                        help="Langflow base URL, when 7860 is not published on the host")
    parser.add_argument("--no-input", action="store_true",
                        help="never prompt; fail instead (for CI)")
    parser.add_argument("--skip-smoke", action="store_true", help="skip the smoke test")
    parser.add_argument("--build", action="store_true",
                        help="rebuild images even if they already exist (after a local code change)")
    args = parser.parse_args()

    print("edu_ai bootstrap")
    print(f"repo: {ROOT}")

    step_preflight()
    if _failed:
        return 1

    step_env()
    step_dirs()
    step_nginx()
    if _failed:
        print("\nFix the errors above and re-run — every step is idempotent.")
        return 1

    # Postgres and Langflow first: the API key cannot be minted before Langflow
    # has created its superuser, and the rest of the stack wants that key. nginx
    # joins them because where 7860 is unpublished it is the only route to
    # Langflow from the host, and step 6 needs one.
    # bifrost is named rather than left to Langflow's depends_on: `--wait` gates
    # on it either way, so spelling it out makes the failure legible. It needs
    # working internet to boot at all — it exits on its pricing fetch otherwise.
    if not step_up(["postgres", "bifrost", "edu_ai", "nginx"],
                   "5. start postgres + bifrost + langflow + nginx", build=args.build):
        return 1

    api_key = step_api_key(args.langflow_url, allow_prompt=not args.no_input)
    if api_key is None:
        return 1

    # The only `up` that names every service, so it is where a pending
    # --force-recreate can actually reach nginx and edu_ai_api.
    if not step_up([], "7. start the remaining services", build=args.build,
                   apply_pending_recreate=True):
        return 1

    base_url = langflow_url(args.langflow_url) or "http://127.0.0.1:7860"
    step_langflow_vars(base_url, api_key)
    step_flows(base_url, api_key)

    if not args.skip_smoke:
        step_smoke(base_url)

    step_summary(base_url)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
