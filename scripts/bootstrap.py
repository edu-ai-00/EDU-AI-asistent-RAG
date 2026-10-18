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

Secrets stay in .env, which remains their permanent source. Progress lines and
error messages never carry a password, an API key or an encryption key: they are
written from status codes and fixed phrases, and every line passes through
redact(). The one exception is the closing overview of a SUCCESSFUL run at an
interactive terminal, which shows once the local secrets THIS run created, so
they can be put in a password vault. It never shows values that already existed
(a re-run), values supplied from outside (OPENAI_API_KEY, tokens, provider keys,
a pasted key), or anything when output is redirected, in CI or under --no-input;
there the summary gives only the path to .env.

A key Langflow cannot be asked about (timeout, 5xx, odd answer) is left alone and
the run stops with exit status 3 — run it again later.

Standard library only; everything else needs just Docker.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import secrets
import shlex
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from hashlib import sha256

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from create_api_key import (  # noqa: E402
    EXIT_RETRY_LATER, KeyUnverifiable, describe_error, display_url, ensure_api_key,
)
import db_roles  # noqa: E402
import eduai_layout  # noqa: E402
import smoke  # noqa: E402
from envfile import create_private, is_placeholder, read_env, set_env, write_atomic  # noqa: E402
from http_safe import open_url  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
STATE_DIR = ROOT                       # where uploads, langflow_data, ... really live
_layout: "eduai_layout.Layout | None" = None   # set by main() only when a layout flag or variable was given

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
# reported at the end of the run. No /health endpoint reports it: edu_ai_search
# answers a fixed {"status": "ok"}, and edu_ai_api's missing_config lists only the
# Langflow variables.
GENERATED_SECRETS: dict[str, "callable"] = {
    "POSTGRES_PASSWORD": _password,
    "PGADMIN_DEFAULT_PASSWORD": _password,
    "LANGFLOW_SUPERUSER_PASSWORD": _password,
    "LANGFLOW_SECRET_KEY": _fernet_key,
    "POSTGRES_ADMIN_PASSWORD": _password,
    "POSTGRES_SEARCH_PASSWORD": _password,
    "POSTGRES_LANGFLOW_PASSWORD": _password,
    "POSTGRES_MIGRATOR_PASSWORD": _password,
    "BIFROST_SETUP_TOKEN": _password,
    "BIFROST_ADMIN_PASSWORD": _password,
}

_summary: list[str] = []
_failed = False
_needs_recreate = False
_key_unverifiable = False  # the Langflow key in .env could not be checked: retry later
# Names (never values) of the local secrets THIS run created, in the order they were
# made. Only these may appear in the closing overview; values are read back from
# .env when shown, so .env stays the one source of truth.
_created_secrets: list[str] = []


# ───────────────────────────────────────────────────────────────
#  Output
# ───────────────────────────────────────────────────────────────
# Secrets are kept out of the output twice over: messages are written from status
# codes and fixed phrases, and everything printed also passes through redact(),
# which masks any value that .env marks as a secret. The second layer is for the
# message nobody thought to check.

_SECRET_NAME_PARTS = ("PASSWORD", "SECRET", "TOKEN", "KEY")
_MIN_SECRET_LEN = 6        # shorter values would mask ordinary words in the output
_runtime_secrets: set[str] = set()  # e.g. a key just minted or pasted
_last_env_secrets: set[str] = set()  # every secret value .env has held during this run: kept when .env cannot be read
_MAX_RELAYED_LINE = 160


def redact(text: str) -> str:
    """`text` with every known secret value replaced by [redacted]."""
    try:
        from_env = read_env(ENV_PATH)
    except (OSError, ValueError):
        from_env = {}               # unreadable now (a bad byte, a lock): keep masking what was known, never fail open
    # Only ever added to: a file that vanished or lost a line must not switch the masking off.
    _last_env_secrets.update(value for name, value in from_env.items()
                             if any(part in name.upper() for part in _SECRET_NAME_PARTS)
                             and not is_placeholder(value))
    values = _last_env_secrets | _runtime_secrets
    for value in sorted((v for v in values if len(v) >= _MIN_SECRET_LEN), key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return text


def report(status: str, message: str) -> None:
    print(f"  [{status:4s}] {redact(message)}")


def fail(message: str, hint: str = "") -> None:
    global _failed
    _failed = True
    report("fail", message)
    if hint:
        print(f"         → {redact(hint)}")


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")                 # terminal escapes, CR, NUL
_USERINFO = re.compile(r"(://)[^/\s@]+@")                           # user:password@ inside a URL
_CHILD_MARKER = re.compile(r"^\s*\[(pass|skip|FAIL|note)\]")           # smoke.py result lines and notes
_CHILD_LINE = re.compile(r"^\s*(\d+\.\s|\d+ passed|Skipped checks|edu_ai smoke|[+~] |Importing |Done\.)")


def relay(text: str) -> None:
    """Show a child script's report, indented.

    smoke.py and import_flows.py echo up to 300 characters of an HTTP error body
    on failure, and a message can span lines. Only lines of the child's own shapes
    (a result marker, a numbered heading, a total, an import line) are shown: the
    rest of a multi-line message after a marker is a body and is dropped. A [FAIL]
    line is also cut at its first ':' — the label and status stay, whatever followed
    does not — control characters become '?', credentials inside a URL are removed,
    and every line is redacted before it is shortened, so a cut never leaves half a secret.
    """
    after_marker = False
    for raw in text.splitlines():
        line = _USERINFO.sub(r"\1", _CONTROL.sub("?", raw.rstrip()))
        if not line.strip():
            after_marker = False
            continue
        if _CHILD_MARKER.match(line):
            after_marker = True
        elif after_marker and not _CHILD_LINE.match(line):
            continue                # the rest of a multi-line message (a body): never relayed
        else:
            after_marker = False
        if "[FAIL]" in line:
            head, separator, _ = line.partition(":")
            if separator:
                line = f"{head} (details withheld)"
        print(f"         {redact(line)[:_MAX_RELAYED_LINE]}")


def phase(title: str) -> None:
    print(f"\n{title}")


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command in the repo root, capturing output."""
    if _layout is not None and args[:2] == ["docker", "compose"]:
        args = _layout.compose_prefix() + args[2:]
    return subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", **kwargs
    )


def compose(*args: str, stream: bool = False, timeout: int | None = None):
    """Invoke `docker compose` in the repo root (pinned to the layout when one was given)."""
    command = ["docker", "compose", *args]
    if stream:
        if _layout is not None:
            command = _layout.compose_prefix() + list(args)
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
    _check_pg_major()


# The extension file the ParadeDB .deb installs; Dockerfile.postgres puts it there.
# PG_MAJOR is set by the postgres base image; single quotes keep it for the container's shell.
PG_SEARCH_CONTROL = "/usr/share/postgresql/$PG_MAJOR/extension/pg_search.control"
PG_DATA_TARGET = "/var/lib/postgresql/data"


def _dockerfile_pg_major() -> str | None:
    """The PostgreSQL major version of this checkout's Dockerfile.postgres base image (…-pgNN@…)."""
    return eduai_layout.postgres_major(ROOT)


def _check_pg_major() -> None:
    """Refuse when the data volume holds another PostgreSQL major version than the image.

    A major version cannot start on old data files: postgres restarts in a loop and `up --wait`
    only gives up after many minutes. The data must be moved (dump, clean installation, load).
    """
    want = _dockerfile_pg_major()
    result = compose("config", "--format", "json")
    try:
        config = json.loads(result.stdout) if result.returncode == 0 else None
        mounts = config["services"]["postgres"]["volumes"]
        source = next(m["source"] for m in mounts if m.get("target") == PG_DATA_TARGET)
        volume = (config.get("volumes") or {}).get(source, {}).get("name", source)
    except (TypeError, ValueError, KeyError, StopIteration):
        report("warn", "could not resolve the postgres data volume — skipped the major version check")
        return
    if want is None:
        report("warn", "could not read the PostgreSQL major version of Dockerfile.postgres — skipped the check")
        return
    if run(["docker", "volume", "inspect", volume]).returncode != 0:
        report("ok", f"no data volume yet; it will be initialised for PostgreSQL {want}")
        return
    probe = run(["docker", "run", "--rm", "--network", "none", "-v", f"{volume}:/data:ro",
                 "alpine:3", "sh", "-c", "cat /data/PG_VERSION 2>/dev/null || true"])
    have = probe.stdout.strip() if probe.returncode == 0 else ""
    if probe.returncode != 0:
        report("warn", f"could not read {volume} — skipped the major version check")
    elif not have:
        report("ok", f"{volume} is empty; it will be initialised for PostgreSQL {want}")
    elif have == want:
        report("ok", f"{volume} holds PostgreSQL {have}, as the image")
    else:
        fail(f"{volume} holds PostgreSQL {have} data, this checkout's image is PostgreSQL {want}; "
             "postgres would not start on it. Nothing was changed.",
             "move the data: dump it with the current installation, then install cleanly and load it "
             "(docs/OPERATIONS_LINUX.md, 'A new PostgreSQL major version')")


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
                 "-c", f'test -f "{PG_SEARCH_CONTROL}"'])
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
        # Private from the first byte and complete or not at all: the file is about to
        # hold every secret, so there is no moment at which it is readable by others.
        try:
            create_private(ENV_PATH, ENV_EXAMPLE.read_bytes())
        except FileExistsError:
            created = False                    # appeared since the check: fill it, never replace it
        except OSError as exc:
            fail(f".env could not be created ({type(exc).__name__}); nothing was written",
                 "check the permissions of the checkout directory")
            return
    if created:
        report("ok", f"created .env from {ENV_EXAMPLE.name}" + (" (mode 0600)" if os.name == "posix" else ""))
    else:
        report("skip", ".env already exists — filling gaps only")

    example = read_env(ENV_EXAMPLE)
    current = read_env(ENV_PATH)
    updates: dict[str, str] = {}

    # Carry over keys added to .env.example since this .env was written, so an
    # upgrade never needs a manual diff of the two files.
    added = [key for key in example if key not in current]
    for key in added:
        updates[key] = example[key]

    generated: list[str] = []
    for key, generate in GENERATED_SECRETS.items():
        if is_placeholder(updates.get(key, current.get(key))):
            updates[key] = generate()
            generated.append(key)

    if updates:
        # Atomic: if this fails .env is exactly as it was, and nothing generated above
        # is reported or offered to the operator, because it was never stored.
        try:
            set_env(ENV_PATH, updates)
        except OSError as exc:
            fail(f".env could not be updated ({type(exc).__name__}); it was left unchanged",
                 "check the file's permissions and the free space, then re-run")
            return
    for key in added:
        report("ok", f"added missing key {key}")
    for key in GENERATED_SECRETS:
        if key in generated:
            report("ok", f"generated {key}")
        else:
            report("skip", f"{key} already set")
    # Recorded only once .env holds them: a value that never reached the file must
    # not be offered to the operator as something to keep.
    _created_secrets.extend(generated)

    if os.name == "posix" and stat.S_IMODE(ENV_PATH.stat().st_mode) & 0o077:
        report("warn", f".env is accessible to other users (mode {stat.S_IMODE(ENV_PATH.stat().st_mode):04o}) "
                       "and holds every secret — run `chmod 600 .env`")

    final = read_env(ENV_PATH)
    if is_placeholder(final.get("OPENAI_API_KEY")):
        report("warn", "OPENAI_API_KEY is still CHANGE_ME — containers will start, "
                       "but ingestion cannot reach OpenAI")
        _summary.append(
            "OPENAI_API_KEY is not set. Put a real key in .env, then `docker compose up -d`. "
            "No /health endpoint reports a missing OPENAI_API_KEY (the search service answers a "
            "fixed status and the API's missing_config lists only Langflow variables), so "
            "this line is the only warning."
        )

    if is_placeholder(final.get("CHAT_LEGAL_ENTITY")):
        if (final.get("CHAT_ENTITY_PICKER") or "").strip().lower() in {"1", "true", "yes", "on"}:
            # Browser selection: no school is configured on purpose. Nothing to do
            # in .env, SQL or a CLI — the visitor picks it in the chat.
            report("ok", "no fixed school: the visitor chooses it in the chat (CHAT_ENTITY_PICKER is on)")
            _summary.append(
                "Web chat: open it, choose the school from the menu and start writing. "
                "Nothing needs to be configured first."
            )
        else:
            report("warn", "CHAT_LEGAL_ENTITY is still CHANGE_ME — the chat cannot search until it names "
                           "one of your legal entities")
            _summary.append(
                "CHAT_LEGAL_ENTITY is not set. Put the code of one of your legal entities into .env "
                "(SELECT legal_entity_code FROM edu_ai.legal_entity WHERE is_active), then "
                "`docker compose up -d edu_ai_web_chat`. The smoke test fails until it is a valid active entity."
            )


# ───────────────────────────────────────────────────────────────
#  3. Directories
# ───────────────────────────────────────────────────────────────

def step_dirs() -> None:
    phase("3. directories")
    for name in DATA_DIRS:
        path = _data_path(name)
        if path.is_dir():
            report("skip", f"{name}/")
        else:
            path.mkdir(parents=True, exist_ok=True)
            report("ok", f"created {name}/")
    _fix_data_dir_ownership()
    _place_bifrost_config()


def _data_path(name: str) -> pathlib.Path:
    """The real directory behind a bind-mount name: STATE_DIR for the state dirs, else the checkout."""
    return (STATE_DIR if name in eduai_layout.STATE_DIRS else ROOT) / name


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

    # One throwaway container per base directory: a symlink out of ROOT would not resolve inside the mount.
    wrong: dict[pathlib.Path, list[str]] = {}
    for name, (uid, gid, mode) in DATA_DIRS.items():
        path = _data_path(name)
        info = path.stat()
        if info.st_uid != uid or info.st_gid != gid or (info.st_mode & 0o7777) != mode:
            wrong.setdefault(path.parent, []).append(
                f"chown {uid}:{gid} '/mnt/{name}' && chmod {mode:o} '/mnt/{name}'")

    if not wrong:
        report("skip", "ownership (already correct)")
        return

    for base, commands in wrong.items():
        result = run(["docker", "run", "--rm", "-v", f"{base}:/mnt",
                      "alpine:3", "sh", "-c", " && ".join(commands)])
        if result.returncode == 0:
            report("ok", f"set ownership on {len(commands)} director{'y' if len(commands) == 1 else 'ies'}")
        else:
            fail(f"could not set ownership: {(result.stderr or result.stdout).strip()[:200]}",
                 "the containers will start but cannot write to their bind mounts. "
                 f"Fix by hand: sudo chown <uid>:<gid> {' '.join(DATA_DIRS)}")


def _place_bifrost_config() -> None:
    """Copy bifrost/config.json into bifrost_data, the only place Bifrost reads it from (-app-dir).

    v2 needs it: without it a fresh gateway enforces auth on inference and every
    caller here gets 401. It holds no secret (env. references only). Replaced
    only when the checkout's copy differs, so its mtime marks a real change;
    _bifrost_config_newer decides on a restart from that.
    """
    source = ROOT / "bifrost" / "config.json"             # ROOT is the layout's source root once main() resolved it
    try:
        want = source.read_bytes()
    except OSError as exc:
        fail(f"bifrost/config.json is not readable ({type(exc).__name__})", "the checkout is incomplete")
        return
    target = _data_path("bifrost_data") / "config.json"
    try:
        current = target.read_bytes()
    except FileNotFoundError:
        current = None
    except OSError:
        current = b""                          # not readable by this user: copy, the content is unknown
    if current == want:
        report("skip", "bifrost_data/config.json (already the checkout's)")
        return
    if sys.platform != "linux":
        try:
            write_atomic(target, want)
        except OSError as exc:
            fail(f"could not write bifrost_data/config.json ({type(exc).__name__})",
                 "without it Bifrost v2 answers 401 to every caller; copy bifrost/config.json there by hand")
            return
        report("ok", "placed bifrost_data/config.json")
        return
    uid, gid, _ = DATA_DIRS["bifrost_data"]
    tmp = "/mnt/.config.json.edu_ai"
    # Compared inside the container too: an operator who cannot read the directory must not rewrite it every run.
    result = run(["docker", "run", "--rm", "-v", f"{target.parent}:/mnt", "-v", f"{source.parent}:/src:ro",
                  "alpine:3", "sh", "-c",
                  f"if [ -d /mnt/config.json ]; then echo ISDIR; exit 3; fi; "
                  f"if cmp -s /src/config.json /mnt/config.json; then echo SAME; else cp /src/config.json {tmp} && "
                  f"chown {uid}:{gid} {tmp} && chmod 0640 {tmp} && mv {tmp} /mnt/config.json && echo COPIED; fi"])
    if result.returncode == 0 and (result.stdout or "").strip().endswith("SAME"):
        report("skip", "bifrost_data/config.json (already the checkout's)")
    elif result.returncode == 0:
        report("ok", "placed bifrost_data/config.json")
    elif "ISDIR" in (result.stdout or ""):
        fail("bifrost_data/config.json is a directory, not a file; nothing was placed",
             "remove that directory by hand after checking it, then re-run")
    else:
        fail(f"could not place bifrost_data/config.json: {(result.stderr or result.stdout).strip()[:200]}",
             "without it Bifrost v2 answers 401 to every caller; copy bifrost/config.json there by hand (owner 1000:1000)")


def _bifrost_config_newer() -> bool:
    """True when bifrost_data/config.json changed after the running Bifrost started; it reads the file only at start.

    Stateless on purpose: a run that stopped between the copy and the restart is caught by the next one, and a fresh
    container (created after the copy) is not restarted for nothing. Unknown (no container, no answer) is False
    with a warning; the smoke test still checks the effect that matters most, whether inference is open.
    """
    def unknown(why: str) -> bool:
        report("warn", f"could not tell whether bifrost must reload config.json ({why}); "
                       "after a change of bifrost/config.json run `docker compose restart bifrost`")
        return False

    try:
        ids = compose("ps", "-q", "bifrost")
        cid = (ids.stdout or "").split()
        if ids.returncode != 0 or not cid:
            return unknown("no bifrost container")
        started = run(["docker", "inspect", "-f", "{{.State.StartedAt}}", cid[0]])
        mtime = run(["docker", "exec", cid[0], "stat", "-c", "%Y", "/app/data/config.json"])
    except (OSError, subprocess.SubprocessError) as exc:
        return unknown(type(exc).__name__)
    if started.returncode != 0 or mtime.returncode != 0:
        return unknown("docker inspect or stat failed")
    stamp = re.sub(r"(\.\d{6})\d+", r"\1", (started.stdout or "").strip()).replace("Z", "+00:00")
    try:
        return int((mtime.stdout or "").strip()) > datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return unknown("unreadable start time or mtime")


def _restart_bifrost() -> bool:
    """Bifrost reads config.json only at start; `up` keeps a running container whose image and model did not change."""
    result = compose("restart", "bifrost", timeout=300)
    if result.returncode != 0:
        fail("bifrost did not restart after its config.json changed",
             "run `docker compose restart bifrost` and check its log")
        return False
    report("ok", "restarted bifrost to read the new config.json")
    return True


# ───────────────────────────────────────────────────────────────
#  4./6. Bringing the stack up
# ───────────────────────────────────────────────────────────────

def _check_optional_cloudflare() -> bool:
    """Use the effective Compose model: inactive profiles need no provider token."""
    try:
        result = run(["docker", "compose", "config", "--format", "json"])
        if result.returncode != 0:
            raise ValueError("config failed")
        model = json.loads(result.stdout)
        services = model["services"]
        if not isinstance(services, dict) or not services:
            raise ValueError("invalid services")
        if "cloudflared" not in services:
            report("skip", "Cloudflare Tunnel disabled (Compose profile)")
            return True
        token = services["cloudflared"]["environment"].get("TUNNEL_TOKEN")
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, AttributeError):
        fail("could not verify optional Cloudflare configuration",
             "check docker compose config; no services were started by this step")
        return False
    if not isinstance(token, str) or is_placeholder(token.strip()):
        fail("Cloudflare Tunnel enabled without a usable token",
             "set CLOUDFLARE_TUNNEL_TOKEN or disable the cloudflare Compose profile")
        return False
    report("ok", "Cloudflare Tunnel explicitly enabled; token configured")
    return True


# Images built from this checkout. `up` reuses an existing tag, so after `git pull` the old code would keep running
# behind a green smoke. Each image gets a second tag, <repo>:src-<fingerprint of what its Dockerfile copies>; a
# missing or different one means a rebuild of that service only. The previous image stays as <image>-prev.
_COPY = re.compile(r"^\s*(?:COPY|ADD)\s+(.+)$", re.IGNORECASE)
_built_this_run: set[str] = set()        # --build rebuilds once per run, not again in step 6


def _copy_sources(dockerfile: str) -> list[str] | None:
    """Context-relative COPY/ADD sources; None when one cannot be pinned (whole context then)."""
    text = re.sub(r"\\\r?\n", " ", dockerfile)
    if re.search(r"--mount", text):
        return None
    sources: list[str] = []
    for line in text.splitlines():
        m = _COPY.match(line)
        if not m:
            continue
        try:
            words = shlex.split(m.group(1), comments=False, posix=True)
        except ValueError:
            return None
        if any(w.startswith("--from") for w in words):
            continue                                   # copies from another stage, not from the context
        words = [w for w in words if not w.startswith("--")]
        if len(words) < 2 or words[0].startswith("[") or words[0].startswith("<<"):
            return None
        for source in words[:-1]:
            if any(ch in source for ch in "*?[") or "://" in source:
                return None
            sources.append(source)
    return sources


def _image_fingerprint(spec: dict) -> str | None:
    """12 hex of the build section and the git objects of the Dockerfile and its COPY sources; None if not pinned in git."""
    build = dict(spec["build"])
    context = pathlib.Path(build.pop("context"))
    relative = pathlib.Path(os.path.relpath(context, ROOT))
    if relative.parts[:1] == ("..",):
        return None
    dockerfile = context / build.get("dockerfile", "Dockerfile")
    sources = _copy_sources(dockerfile.read_text(encoding="utf-8"))
    inputs = {os.path.relpath(dockerfile, ROOT)}
    if sources is None or any(os.path.normpath(s) == "." for s in sources):
        inputs.add(str(relative))
    else:
        inputs.update(os.path.normpath(relative / s) for s in sources)
    paths = sorted(p.replace(os.sep, "/") for p in inputs)
    dirty = run(["git", "-C", str(ROOT), "status", "--porcelain", "--", *paths])
    if dirty.returncode != 0 or dirty.stdout.strip():
        return None
    oids = []
    for path in paths:
        done = run(["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", f"HEAD:{'' if path == '.' else path}"])
        if done.returncode != 0:
            return None
        oids.append(f"{path} {done.stdout.strip()}")
    payload = json.dumps({"build": build, "context": str(relative).replace(os.sep, "/"), "inputs": oids}, sort_keys=True)
    return sha256(payload.encode("utf-8")).hexdigest()[:12]


def _image_id(ref: str) -> str | None:
    done = run(["docker", "image", "inspect", "--format", "{{.Id}}", ref])
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None


def _refresh_images(services: list[str], rebuild_all: bool = False) -> bool:
    """Rebuild the images of these services (all when empty) whose source fingerprint changed; False on a failed build."""
    try:
        result = run(["docker", "compose", "config", "--format", "json"])
        model = json.loads(result.stdout) if result.returncode == 0 else {}
        specs = model.get("services") or {}
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        specs = {}
    built = [(name, spec) for name, spec in specs.items()
             if isinstance(spec, dict) and spec.get("build") and spec.get("image") and (not services or name in services)]
    if not built:
        return True
    inside = run(["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        report("warn", "not a git checkout: images are built only when missing; after a code change run with --build")
        return True
    seen: set[str] = set()
    for name, spec in built:
        image = spec["image"]
        if image in seen or image in _built_this_run:
            continue
        seen.add(image)
        repo = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
        try:
            fingerprint = _image_fingerprint(spec)
        except (OSError, ValueError, KeyError, TypeError):
            fingerprint = None
        current = _image_id(image)
        marker = f"{repo}:src-{fingerprint}" if fingerprint else None
        marked = _image_id(marker) if marker else None
        if current and marked == current and not rebuild_all:
            report("skip", f"{name}: image matches its source ({fingerprint})")
            continue
        unmarked = not any(tag.startswith("src-") for tag in
                           (run(["docker", "image", "ls", repo, "--format", "{{.Tag}}"]).stdout or "").split())
        why = ("rebuild requested" if rebuild_all else "image missing" if not current
               else "source not committed or not in git" if not fingerprint
               else "no source tag yet" if unmarked else "source changed")
        if current:
            run(["docker", "tag", current, f"{image}-prev"])
        result = compose("build", name, stream=True, timeout=3600)
        if result.returncode != 0:
            fail(f"building {name} failed (exit {result.returncode})", "check the build output above; the previous image is kept")
            return False
        _built_this_run.add(image)
        new = _image_id(image)
        if marker and new:
            run(["docker", "tag", new, marker])
            tags = run(["docker", "image", "ls", repo, "--format", "{{.Tag}}"])
            for tag in (tags.stdout or "").split():
                if tag.startswith("src-") and tag != f"src-{fingerprint}":
                    run(["docker", "image", "rm", f"{repo}:{tag}"])       # untag only; -prev keeps the previous image
        report("ok", f"{name}: built ({why})" + (f", source {fingerprint}" if fingerprint else ""))
    return True


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
    if not _check_optional_cloudflare():
        return False
    if not _refresh_images(services, rebuild_all=build):
        return False

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
    if (not services or "bifrost" in services) and _bifrost_config_newer():
        return _restart_bifrost()
    return True


# ───────────────────────────────────────────────────────────────
#  5. Langflow API key
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
    """Make sure .env holds a key Langflow accepts; return it for the later steps.

    None means "stop here". `_key_unverifiable` then says whether that is a plain
    failure or a key Langflow could not be asked about — the second changed
    nothing and is worth repeating later. No `force`: replacing a key Langflow
    has not rejected is the operator's call (create_api_key.py --force), never
    this script's.
    """
    global _needs_recreate, _key_unverifiable
    phase("5. Langflow API key")

    base_url = langflow_url(url_override)
    if base_url is None:
        fail("Langflow is not reachable from the host",
             "7860 is published only by docker-compose.override.yml (dev). Where "
             "that override is not loaded, set LANGFLOW_URL=http://127.0.0.1:8444 "
             "in .env — nginx proxies the same API — or pass --langflow-url. A redirect "
             "is never followed (the API key must not leave that address): use the final one.")
        return None
    report("ok", f"Langflow reachable at {display_url(base_url)}")

    try:
        api_key, outcome = ensure_api_key(base_url, ENV_PATH, allow_prompt=allow_prompt)
    except KeyUnverifiable as exc:
        _key_unverifiable = True
        fail(str(exc), "re-run scripts/bootstrap.py later; every step is idempotent "
                       "(exit status 3 means \"try again\", not \"broken\").")
        return None
    except RuntimeError as exc:
        fail(str(exc))
        return None

    _runtime_secrets.add(api_key)
    report({"skip": "skip"}.get(outcome, "ok"), {
        "skip": "LANGFLOW_API_KEY in .env already works",
        "created": "created a new API key and wrote it to .env",
        "replaced": "Langflow rejected the API key in .env — created a new one and wrote it to .env",
        "manual": "stored the pasted key in .env",
    }[outcome])

    if outcome != "skip":
        # env is read at container creation, so the API must be recreated to see it.
        _needs_recreate = True
    # "manual" is the operator's own key, not something this run made.
    if outcome in ("created", "replaced") and "LANGFLOW_API_KEY" not in _created_secrets:
        _created_secrets.append("LANGFLOW_API_KEY")
    return api_key


# ───────────────────────────────────────────────────────────────
#  6b. Langflow connectivity variables
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
    phase("6b. Langflow connectivity variables")

    values = read_env(ENV_PATH)
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    try:
        request = urllib.request.Request(f"{base_url}/api/v1/variables/",
                                         headers={"x-api-key": api_key})
        with open_url(request, timeout=15) as response:
            items = {item["name"]: item for item in json.loads(response.read())}
            existing = set(items)
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
        fail("could not list Langflow variables", describe_error(exc))
        return

    created, skipped, missing, updated = [], [], [], []
    for name in CONNECTIVITY_VARS:
        if name in existing:
            # An existing Generic variable wins over .env, so a changed .env value (the DB role after the
            # role contract) has to be carried over here, or the flows keep connecting as the old one.
            item, wanted = items[name], values.get(name)
            if item.get("type") == "Generic" and "value" in item and item.get("id") and wanted and not is_placeholder(wanted) and item["value"] != wanted:
                payload = json.dumps({"id": item["id"], "name": name, "value": wanted, "type": "Generic", "default_fields": []}).encode()
                try:
                    request = urllib.request.Request(f"{base_url}/api/v1/variables/{item['id']}", data=payload, headers=headers, method="PATCH")
                    with open_url(request, timeout=15):
                        updated.append(name)
                except (urllib.error.URLError, OSError) as exc:
                    fail(f"could not update Langflow variable {name}", describe_error(exc))
                    return
                continue
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
            with open_url(request, timeout=15):
                created.append(name)
        except (urllib.error.URLError, OSError) as exc:
            fail(f"could not create Langflow variable {name}", describe_error(exc))
            return

    if created:
        report("ok", f"created {len(created)} Generic variable(s): {', '.join(created)}")
    if updated:
        report("ok", f"updated {len(updated)} Generic variable(s) to the .env value: {', '.join(updated)}")
    if skipped:
        report("skip", f"{len(skipped)} already present")
    if missing:
        report("fail", f"not in .env: {', '.join(missing)} — flows will fail in the web UI")


# ───────────────────────────────────────────────────────────────
#  7. Flows
# ───────────────────────────────────────────────────────────────

def step_flows(base_url: str, api_key: str) -> None:
    phase("7. fill missing flow aliases")
    environment = {**os.environ, "LANGFLOW_URL": base_url, "LANGFLOW_API_KEY": api_key}
    result = run([sys.executable, "scripts/import_flows.py", "--missing-only"], env=environment)
    if result.returncode != 0:
        # import_flows.py puts the HTTP error body into its exit message, so its
        # output is not echoed here.
        fail(f"import_flows.py failed (exit {result.returncode})",
             "its output is not echoed (it can carry an HTTP response body); "
             "run `python3 scripts/import_flows.py --missing-only` to see it")
        return
    relay(result.stdout)
    report("ok", "required flow aliases present; existing flows preserved")


# ───────────────────────────────────────────────────────────────
#  8. Smoke test
# ───────────────────────────────────────────────────────────────

def step_smoke(base_url: str) -> None:
    phase("8. smoke test")
    result = run([sys.executable, "scripts/smoke.py", "--langflow-url", base_url,
                  *(_layout.argv() if _layout is not None else [])])
    relay(result.stdout)
    if result.returncode != 0:
        fail("smoke test reported failures",
             "failure details are not echoed (they can carry an HTTP response body); "
             "run `python3 scripts/smoke.py` to see them")
    else:
        report("ok", "smoke test passed")


# ───────────────────────────────────────────────────────────────
#  8b. Database extensions
# ───────────────────────────────────────────────────────────────

# A new postgres image brings newer extension binaries; the catalog keeps the old
# version until ALTER EXTENSION UPDATE. That is one-way, so it runs only after a
# passing smoke test: until then the previous image (<image>-prev) still fits the data.
_EXT_SQL = ("SELECT e.extname, e.extversion, a.default_version FROM pg_extension e "
            "JOIN pg_available_extensions a ON a.name = e.extname "
            "WHERE e.extname IN ('vector', 'pg_search') ORDER BY 1;\n")
_EXT_LOGS = "docker logs --tail 50 edu_ai_postgres"


def _ext_version(text: str) -> tuple[int, ...] | None:
    return tuple(int(p) for p in text.split(".")) if re.fullmatch(r"\d+(\.\d+)*", text) else None


_EXT_TIMED_OUT = 124


def _ext_psql(sql: str) -> subprocess.CompletedProcess:
    """psql in the postgres container as its superuser (POSTGRES_USER is read inside the container)."""
    try:
        return run(["docker", "compose", "exec", "-T", "postgres", "sh", "-c",
                    'psql -X -q -A -t -F " " -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'],
                   input=sql, timeout=300)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([], _EXT_TIMED_OUT, "", "")


def _search_probe_passes() -> bool:
    """The smoke test's BM25 + vector probe, as the search role, after the update."""
    try:
        database = read_env(ENV_PATH).get("POSTGRES_DB") or "edu_ai"
    except (OSError, ValueError):
        database = "edu_ai"
    try:
        result = run(["docker", "compose", "exec", "-T", "postgres", *smoke.search_index_probe_args(database)],
                     timeout=120)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def _ext_rows() -> list[tuple[str, str, str]] | None:
    result = _ext_psql(_EXT_SQL)
    if result.returncode != 0:
        return None
    return [tuple(line.split()) for line in result.stdout.splitlines() if len(line.split()) == 3]


def step_extensions(smoke_passed: bool) -> None:
    phase("8b. database extensions")
    if not smoke_passed:
        report("skip", "extension update needs a passing smoke test in this run (the update is one-way)")
        return
    rows = _ext_rows()
    if rows is None:
        fail("could not read the extension versions", "check that the postgres service is running")
        return
    pending = []
    for name, have, available in rows:
        h, a = _ext_version(have), _ext_version(available)
        if h is None or a is None:
            report("warn", f"{name}: cannot compare {have} with {available}; left as it is")
        elif h < a:
            pending.append((name, have, available))
        elif h > a:
            report("warn", f"{name} {have} in the database is newer than {available} in the image "
                           "(an older image?); left as it is")
    if not pending:
        report("ok", "extensions match the image: " + ", ".join(f"{n} {v}" for n, v, _ in rows))
        return
    try:
        privileges = db_roles.extension_privileges(db_roles.load_matrix())
    except (OSError, ValueError, KeyError, db_roles.ContractError) as exc:
        fail(f"extension update skipped: the role contract matrix cannot be read ({exc})")
        return
    # The functions an update adds start with EXECUTE for PUBLIC: the contract's privileges go in the same transaction.
    # lock_timeout as in the role contract: the application is connected, never wait behind it.
    sql = ("BEGIN;\nSET LOCAL lock_timeout = '5s';\n"
           + "".join(f'ALTER EXTENSION "{n}" UPDATE;\n' for n, _, _ in pending)
           + "\n".join(privileges) + "\nCOMMIT;\n")
    rc = _ext_psql(sql).returncode
    if rc == _EXT_TIMED_OUT:
        fail("extension update timed out; its outcome is unknown",
             "run the installation again: this step re-reads the versions")
        return
    if rc != 0:
        fail("extension update failed; nothing was changed (one transaction)",
             f"the error is in the postgres log: {_EXT_LOGS}")
        return
    after = {n: v for n, v, _ in (_ext_rows() or [])}
    for name, have, available in pending:
        if after.get(name) == available:
            report("ok", f"{name} {have} -> {available} (a return to the previous image now needs a backup restore)")
        else:
            fail(f"{name} is {after.get(name, 'unknown')} after the update, expected {available}",
                 f"the error is in the postgres log: {_EXT_LOGS}")
    if _search_probe_passes():
        report("ok", f"BM25 and vector search work for {smoke.SEARCH_USER} after the update")
    else:
        fail(f"BM25/vector search as {smoke.SEARCH_USER} fails after the update",
             f"the error is in the postgres log: {_EXT_LOGS}")


# ───────────────────────────────────────────────────────────────
#  9. Summary
# ───────────────────────────────────────────────────────────────

_CI_VARS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE", "TF_BUILD")


def interactive_terminal(no_input: bool) -> bool:
    """True only for a person sitting at a terminal.

    Not under --no-input, not in CI, and not when either end is redirected: a log
    file or a pipe keeps whatever is printed, a terminal's scrollback is the
    operator's own.
    """
    if no_input:
        return False
    for name in _CI_VARS:
        if os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no"):
            return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):  # a closed or replaced stream
        return False


def _secrets_to_show(env: dict[str, str]) -> list[tuple[str, str]]:
    """(name, value) of what THIS run created and .env still holds, in creation order.

    The allow-list is _created_secrets and nothing else: values that existed
    before, tokens and provider keys from outside, and a pasted key never get here.
    """
    return [(name, env[name]) for name in _created_secrets
            if not is_placeholder(env.get(name))]


def step_summary(base_url: str | None, no_input: bool = False) -> None:
    """Closing overview. Whether it may show the secrets this run created is decided
    here and nowhere else: an interactive terminal AND a run without errors."""
    env = read_env(ENV_PATH)
    reveal = interactive_terminal(no_input) and not _failed
    to_show = _secrets_to_show(env) if reveal else []
    print("\n" + "─" * 70)
    print("edu_ai is up." if not _failed else "bootstrap finished WITH ERRORS.")
    print("─" * 70)

    print("\nURLs")
    for label, url in [
        ("Langflow UI", display_url(base_url or "http://127.0.0.1:7860")),
        ("Ingestion API", "http://127.0.0.1:8080/"),
        ("API health", "http://127.0.0.1:8080/health"),
        ("pgAdmin", "http://localhost:8443/pgadmin/"),
        ("Web chat", "http://localhost/chat/"),
    ]:
        print(f"  {label:16s} {url}")

    # Logins are names, not secrets. Names of what this run created are not either.
    print("\nConfiguration")
    print(f"  {'File':16s} {ENV_PATH}")
    print(f"  {'Langflow login':16s} {env.get('LANGFLOW_SUPERUSER', 'admin')}"
          "   (LANGFLOW_SUPERUSER_PASSWORD)")
    print(f"  {'pgAdmin login':16s} {env.get('PGADMIN_DEFAULT_EMAIL', '?')}"
          "   (PGADMIN_DEFAULT_PASSWORD)")
    print(f"  {'Bifrost login':16s} {env.get('BIFROST_ADMIN_USERNAME', '?')}"
          "   (BIFROST_ADMIN_PASSWORD; UI on 127.0.0.1:8099 over an SSH tunnel)")
    if _created_secrets:
        print(f"  {'Created now':16s} {', '.join(_created_secrets)}")
    if to_show:
        print("  The same values stay in that file, the permanent source; they are "
              "not printed again.")
    elif _created_secrets:
        print("  The values are in that file only: this output is not an interactive "
              "terminal (CI, --no-input or redirected) or the run had errors, so "
              "they are not printed.")
    else:
        print("  The passwords and keys are in that file only; a re-run never prints them.")

    print("\nNext steps")
    for line in _summary:
        print(f"  - {line}")
    print("  - Keep .env (gitignored) off shared storage and back it up apart from "
          "database backups. LANGFLOW_SECRET_KEY in it must never change.")

    # Last on purpose, and printed directly: redact() would mask exactly these.
    if to_show:
        print("\nShown once — put these in your password vault now")
        for name, value in to_show:
            print(f"  {name:30s} {value}")
        print("  Clear this terminal's scrollback afterwards.")


# ───────────────────────────────────────────────────────────────

def _apply_layout(args) -> bool:
    """Take the external layout (flags or EDU_AI_* variables) or keep the legacy in-checkout one.

    The checkout must already carry the links (.env and the state dirs): bootstrap creates none and never
    an external .env, and a conflicting, wrong or unreadable name stops the run before anything else.
    """
    global ROOT, ENV_PATH, ENV_EXAMPLE, STATE_DIR, _layout
    try:
        chosen = eduai_layout.resolve(args, os.environ, ROOT)
    except eduai_layout.LayoutError as exc:
        fail(f"invalid layout ({exc.code}: {exc.field})")
        return False
    if all(origin == "default" for origin in chosen.origin.values()):
        return True
    findings = eduai_layout.validate(chosen, links="require")
    for finding in findings:
        fail(f"layout: {finding.code} ({finding.level}) {finding.detail}")
    if findings:
        return False
    ROOT, ENV_PATH, STATE_DIR = chosen.source_root, chosen.env_path, chosen.state_dir
    ENV_EXAMPLE = ROOT / ".env.example"
    _layout = chosen
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bring a clean edu_ai checkout up to a running stack.",
        epilog="Exit status: 0 done, 1 error, 3 the Langflow API key in .env could not be verified "
               "(it was left as it is; run again later: every step is idempotent).")
    parser.add_argument("--langflow-url", default=None,
                        help="Langflow base URL, when 7860 is not published on the host")
    parser.add_argument("--no-input", action="store_true",
                        help="never prompt; fail instead (for CI)")
    parser.add_argument("--skip-smoke", action="store_true", help="skip the smoke test")
    parser.add_argument("--build", action="store_true",
                        help="rebuild images even if they already exist (after a local code change)")
    eduai_layout.add_arguments(parser)
    args = parser.parse_args()

    if not _apply_layout(args):
        return 1

    print("edu_ai bootstrap")
    print(f"repo: {ROOT}")

    step_preflight()
    if _failed:
        return 1

    step_env()
    step_dirs()
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
                   "4. start postgres + bifrost + langflow + nginx", build=args.build):
        return 1

    api_key = step_api_key(args.langflow_url, allow_prompt=not args.no_input)
    if api_key is None:
        return EXIT_RETRY_LATER if _key_unverifiable else 1

    # The only `up` that names every service, so it is where a pending
    # --force-recreate can actually reach nginx and edu_ai_api.
    if not step_up([], "6. start the remaining services", build=args.build,
                   apply_pending_recreate=True):
        return 1

    base_url = langflow_url(args.langflow_url) or "http://127.0.0.1:7860"
    step_langflow_vars(base_url, api_key)
    step_flows(base_url, api_key)

    if not args.skip_smoke:
        step_smoke(base_url)
    step_extensions(smoke_passed=not args.skip_smoke and not _failed)

    step_summary(base_url, no_input=args.no_input)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
