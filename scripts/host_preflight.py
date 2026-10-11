#!/usr/bin/env python3
"""B06-2: host preflight and safe handoff to bootstrap for a FIRST install. Standard library only.

    host_preflight.py check   LAYOUT [--min-free-gb N] [--min-compose X.Y.Z] [--reserved CIDR ...] [--accept-unmeasured-network-pools]
    host_preflight.py handoff LAYOUT [same options] [--env-link-only] [--exec]
    host_preflight.py pin-env LAYOUT [--tunnel-subnet CIDR | --allocate-tunnel-subnet] [--set NAME=VALUE ...]

LAYOUT is --source-root --config-dir --state-dir --project (scripts/eduai_layout.py; EDU_AI_* variables work too).
`check` only reads: every command goes through ONE Host object that accepts a whitelist of read-only commands and
nothing else. What cannot be read or parsed is UNVERIFIED, never "ok"; a proven conflict is REFUSED. Output lines are
`CODE: LEVEL detail`, then `RESULT:` and always `INSTALLATION_NOT_STARTED`. Names, paths and counts only: no value
of .env, no key and no token is ever printed, and no child output is relayed.
`handoff` runs `check` and, only if it is clean, creates the MISSING links and state directories (never replacing or
removing anything), then prints the bootstrap command (or runs it with --exec). `pin-env` appends only ABSENT keys.
Exit status: 0 nothing found | 2 REFUSED (a proven conflict) | 3 UNVERIFIED (could not prove it).
"""
from __future__ import annotations

import argparse
import dataclasses
import ipaddress
import json
import os
import pathlib
import posixpath
import re
import shlex
import stat
import subprocess
import sys
from typing import NamedTuple

SCRIPTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import bootstrap  # noqa: E402  (DATA_DIRS: the mode of each state directory)
import eduai_layout as L  # noqa: E402
import envfile  # noqa: E402
import install  # noqa: E402  (record schema, .env parser, static Compose scan: pure functions only)
import tunnel_subnet  # noqa: E402  (parse_candidate, model_subnet: pure functions only)

TUNNEL_SCRIPT = SCRIPTS / "tunnel_subnet.py"
OK, NOTE, REFUSED, UNVERIFIED = "OK", "NOTE", "REFUSED", "UNVERIFIED"
EXIT = {OK: 0, REFUSED: 2, UNVERIFIED: 3}
MARKER = "INSTALLATION_NOT_STARTED"
DEFAULT_PROJECT = "edu_ai"
COMPOSE_BASE, COMPOSE_OVERRIDE = "docker-compose.yml", "docker-compose.override.yml"
TUNNEL_VAR = "EDU_AI_TUNNEL_SUBNET"
PROJECT_LABEL = "com.docker.compose.project"
DEFAULT_SOCKET = "unix:///var/run/docker.sock"
# PROVISIONAL, not measured: Compose features the file uses (configs.content) and a disk guess; verified on Compose 5.3.1 only.
MIN_COMPOSE = (2, 24, 0)
MIN_FREE_GB = 20.0
X86 = ("x86_64", "amd64")
MAX_ENV = 1 << 20
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SECRET_PARTS = ("PASSWORD", "SECRET", "TOKEN", "KEY", "CREDENTIAL")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_KEEP_ENV = ("PATH", "HOME", "USER", "LOGNAME", "TERM", "XDG_RUNTIME_DIR", "SYSTEMROOT", "TMPDIR", "TEMP")
_FIXED_ENV = {"LC_ALL": "C", "LANG": "C", "NO_COLOR": "1", "DOCKER_CLI_HINTS": "false"}
_WATCHED = ("DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY", "DOCKER_TLS", "DOCKER_API_VERSION", "EDU_AI_TUNNEL_SUBNET")


# ── the one door to the machine ──────────────────────────────────────────────
class Forbidden(Exception):
    """A command that is not on the read-only whitelist. A bug in this tool, never swallowed."""


class ProbeFailed(Exception):
    """A command could not run or timed out; the message is an exception type name, never output."""


class Proc(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


# Words that must never be an argument of anything this tool runs (defence in depth behind the whitelist).
FORBIDDEN_WORDS = frozenset({
    "down", "rm", "prune", "-v", "--volumes", "--remove-orphans", "up", "build", "run", "exec", "create", "start", "stop",
    "restart", "kill", "pause", "pull", "push", "rmi", "cp", "commit", "load", "import", "tag", "login", "logout",
    "--force", "--force-recreate"})
_FIXED_COMMANDS = frozenset({
    ("docker", "info", "--format", "{{json .}}"),
    ("docker", "compose", "version", "--short"),
    ("docker", "buildx", "version"),
    ("docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"),
    ("docker", "volume", "ls", "--format", "{{json .}}"),
    ("docker", "network", "ls", "--no-trunc", "--format", "{{json .}}"),
    ("docker", "image", "ls", "--format", "{{json .}}"),
    ("ss", "-H", "-ltn"),
    ("uname", "-m"),
})


def _network(text: str) -> bool:
    try:
        ipaddress.ip_network(text, strict=False)
        return True
    except ValueError:
        return False


def _is_compose_config(argv: list[str]) -> bool:
    if argv[:2] != ["docker", "compose"] or argv[-3:] != ["config", "--format", "json"]:
        return False
    middle, seen, i = argv[2:-3], {}, 0
    while i < len(middle):
        flag = middle[i]
        if flag not in ("-p", "--project-directory", "--env-file") or flag in seen or i + 1 >= len(middle):
            return False
        value = middle[i + 1]
        if value.startswith("-") or (flag == "-p" and not _PROJECT_RE.match(value)):
            return False
        seen[flag] = value
        i += 2
    return "--project-directory" in seen and "--env-file" in seen


def _is_tunnel_subnet(argv: list[str]) -> bool:
    if len(argv) < 4 or argv[0] != sys.executable or argv[1] != str(TUNNEL_SCRIPT) or argv[2] not in ("check", "allocate"):
        return False
    rest, i, project, positional = argv[3:], 0, None, []
    while i < len(rest):
        token = rest[i]
        if token == "--project" and i + 1 < len(rest) and _PROJECT_RE.match(rest[i + 1]):
            project = rest[i + 1]
        elif token == "--routes" and i + 1 < len(rest) and rest[i + 1] == "host":    # `docker` would start a container
            pass
        elif token == "--reserved" and i + 1 < len(rest) and _network(rest[i + 1]):
            pass
        elif not token.startswith("-") and _network(token):
            positional.append(token)
            i += 1
            continue
        else:
            return False
        i += 2
    if argv[2] == "allocate":
        return project is not None and not positional
    return len(positional) == 1


def deny_reason(argv) -> str | None:
    """None when `argv` is on the whitelist, otherwise why not."""
    if not argv or not all(isinstance(a, str) and a and "\0" not in a for a in argv):
        return "empty or non-string argument"
    bad = FORBIDDEN_WORDS.intersection(argv)
    if bad:
        return f"forbidden word {sorted(bad)[0]}"
    command = list(argv)
    if tuple(command) in _FIXED_COMMANDS or _is_compose_config(command) or _is_tunnel_subnet(command):
        return None
    return "not on the read-only whitelist"


def child_env(parent) -> dict[str, str]:
    """A fixed environment: no inherited DOCKER_*, COMPOSE_* or model variable can change what is read."""
    env = {k: parent[k] for k in _KEEP_ENV if k in parent}
    env.update(_FIXED_ENV)
    return env


class Host:
    """Everything this tool learns about the machine. Read-only; tests replace _exec and the file reads."""

    def run(self, argv, *, cwd=None, timeout=120) -> Proc:
        reason = deny_reason(argv)
        if reason:
            raise Forbidden(reason)
        return self._exec(list(argv), cwd, timeout)

    def _exec(self, argv, cwd, timeout) -> Proc:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                                  env=child_env(os.environ), stdin=subprocess.DEVNULL, cwd=cwd)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProbeFailed(type(exc).__name__) from None
        return Proc(done.returncode, done.stdout or "", done.stderr or "")

    def platform(self) -> str:
        return sys.platform

    def environ(self) -> dict[str, str]:
        return dict(os.environ)

    def uid(self) -> int | None:
        return os.geteuid() if hasattr(os, "geteuid") else None

    def posix(self) -> bool:
        return os.name == "posix"

    def lstat(self, path) -> os.stat_result:
        return os.lstat(path)

    def listdir(self, path) -> list[str]:
        return sorted(os.listdir(path))

    def read_bytes(self, path, limit: int) -> bytes:
        """A bounded regular file, never through a symlink."""
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError("not a bounded regular file")
            chunks = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(fd)

    def disk_free(self, path) -> int:
        if hasattr(os, "statvfs"):
            info = os.statvfs(path)
            return info.f_bavail * info.f_frsize
        import shutil
        return shutil.disk_usage(path).free


class Actions:
    """The only writers: handoff creates names that do not exist, pin-env appends. Nothing here removes or replaces."""

    def mkdir(self, path, mode: int) -> None:
        os.mkdir(path, mode)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fchmod(fd, mode)             # the umask must not narrow it (the ownership step of bootstrap sets uid/gid)
        finally:
            os.close(fd)

    def symlink(self, target, path) -> None:
        os.symlink(target, path)            # fails with FileExistsError if the name exists, even as a dangling link

    def write_env(self, path, data: bytes) -> None:
        envfile.write_atomic(path, data)

    def bootstrap(self, argv: list[str], cwd, env) -> int:
        check_bootstrap_argv(argv)
        return subprocess.run(argv, cwd=cwd, env=env).returncode


def check_bootstrap_argv(argv: list[str]) -> None:
    """Only `python bootstrap.py` with the four layout flags may be launched; anything else is a bug."""
    allowed = {names[0] for names in L.FLAGS.values()}
    flags_ok = all(argv[i] in allowed for i in range(2, len(argv), 2))
    if len(argv) < 2 or len(argv) % 2 or argv[0] != sys.executable or pathlib.Path(argv[1]).name != "bootstrap.py" or not flags_ok:
        raise Forbidden("not the bootstrap command of this layout")


# ── report ───────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class Item:
    code: str
    level: str
    detail: str = ""
    needs_model: bool = False       # UNVERIFIED only because the Compose model was not rendered


class Report:
    def __init__(self, mode: str = "first-install") -> None:
        self.mode = mode
        self.items: list[Item] = []

    def add(self, code: str, level: str, detail: str = "", *, needs_model: bool = False) -> None:
        self.items.append(Item(code, level, _CONTROL.sub(" ", str(detail))[:400], needs_model))

    def count(self, level: str) -> int:
        return sum(1 for item in self.items if item.level == level)

    def exit_code(self) -> int:
        return 2 if self.count(REFUSED) else 3 if self.count(UNVERIFIED) else 0

    def lines(self) -> list[str]:
        out = [f"MODE: {self.mode}"]
        out += [f"{i.code}: {i.level} {i.detail}".rstrip() for i in self.items]
        rc = self.exit_code()
        name = {0: "CHECKS_PASSED", 2: "REFUSED", 3: "UNVERIFIED"}[rc]
        out.append(f"RESULT: {name} exit={rc} refused={self.count(REFUSED)} unverified={self.count(UNVERIFIED)} "
                   f"ok={self.count(OK)} notes={self.count(NOTE)}")
        out.append(MARKER)
        return out

    def emit(self, out=None) -> None:
        out = sys.stdout if out is None else out
        try:
            out.reconfigure(errors="backslashreplace")      # an odd path must not cut the result short
        except (AttributeError, ValueError, OSError):
            pass
        try:
            for line in self.lines():
                print(line, file=out)
            out.flush()
        except (OSError, ValueError, UnicodeError):     # a dead pipe must not change the exit status
            pass


def _names(values, cap: int = 12) -> str:
    values = sorted(values)
    return ", ".join(values[:cap]) + (f" (+{len(values) - cap} more)" if len(values) > cap else "")


# ── the Compose model: names, images and ports come from it, never from constants here ──
@dataclasses.dataclass
class Port:
    number: int
    host_ip: str
    service: str


@dataclasses.dataclass
class Model:
    project: str
    containers: dict          # container_name -> service
    images: dict              # reference -> built here (True) or pulled (False)
    networks: dict            # real name -> key
    volumes: dict
    ports: list
    dynamic_or_udp: list      # services whose ports this tool cannot list
    tunnel_subnet: str | None


def _port_numbers(published) -> list[int]:
    text = str(published).strip()
    if m := re.fullmatch(r"(\d{1,5})", text):
        nums = [int(m.group(1))]
    elif m := re.fullmatch(r"(\d{1,5})-(\d{1,5})", text):
        low, high = int(m.group(1)), int(m.group(2))
        if not low <= high or high - low > 1000:
            raise ValueError("port range")
        nums = list(range(low, high + 1))
    else:
        raise ValueError("port")
    if any(not 0 < n < 65536 for n in nums):
        raise ValueError("port range")
    return nums


def parse_model(data, project: str) -> Model:
    """The rendered model as far as this tool reads it. ValueError (a fixed code) when its shape is not what is expected."""
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict) or not data["services"]:
        raise ValueError("MODEL_SHAPE")
    if data.get("name") != project:
        raise ValueError("MODEL_NAME")
    containers, images, ports, odd = {}, {}, [], []
    for service, spec in data["services"].items():
        if not isinstance(service, str) or not isinstance(spec, dict):
            raise ValueError("MODEL_SHAPE")
        cname = spec.get("container_name")
        if cname is not None:
            if not isinstance(cname, str) or not _NAME_RE.match(cname):
                raise ValueError("MODEL_SHAPE")
            containers[cname] = service
        image = spec.get("image")
        if image is not None and not isinstance(image, str):
            raise ValueError("MODEL_SHAPE")
        ref = image or f"{project}-{service}:latest"
        images[ref] = images.get(ref, False) or bool(spec.get("build"))
        for entry in spec.get("ports") or []:
            if not isinstance(entry, dict):
                raise ValueError("MODEL_SHAPE")
            published = entry.get("published")
            if published in (None, ""):
                continue                                # a dynamic host port cannot collide
            if entry.get("protocol", "tcp") != "tcp":
                odd.append(service)
                continue
            host_ip = entry.get("host_ip") or ""
            if not isinstance(host_ip, str):
                raise ValueError("MODEL_SHAPE")
            ports += [Port(n, host_ip, service) for n in _port_numbers(published)]
    named: dict[str, dict] = {}
    for section in ("networks", "volumes"):
        found = {}
        for key, spec in (data.get(section) or {}).items():
            spec = spec or {}
            if not isinstance(key, str) or not isinstance(spec, dict):
                raise ValueError("MODEL_SHAPE")
            if spec.get("external"):
                continue                                # not created by this installation
            real = spec.get("name") or f"{project}_{key}"
            if not isinstance(real, str) or not _NAME_RE.match(real):
                raise ValueError("MODEL_SHAPE")
            found[real] = key
        named[section] = found
    try:
        subnet = tunnel_subnet.model_subnet(data)
    except ValueError:
        subnet = None
    return Model(project, containers, images, named["networks"], named["volumes"], ports, sorted(set(odd)), subnet)


def parse_ss(text: str) -> set[int] | None:
    """Ports with a TCP listener on ANY address (v4 and v6). None when a line is not understood: nothing is guessed."""
    ports = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 5 or parts[0] != "LISTEN":
            return None
        m = re.fullmatch(r"(?:\[[^\]]+\]|[^:\s\[\]]*):(\d{1,5})", parts[3])
        if not m or not 0 < int(m.group(1)) < 65536:
            return None
        ports.add(int(m.group(1)))
    return ports


def _json_lines(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("row")
            rows.append(row)
    return rows


def _split_image(ref: str) -> tuple[str, str] | None:
    if "@" in ref:
        return None
    name, tag = ref, "latest"
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = ref.rsplit(":", 1)
    for prefix in ("docker.io/library/", "docker.io/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name, tag


# ── the check ────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class Options:
    min_free_gb: float = MIN_FREE_GB
    min_compose: tuple = MIN_COMPOSE
    reserved: tuple = ()
    refuse_pulled_images: bool = False
    accept_unmeasured_pools: bool = False


class Preflight:
    def __init__(self, layout: L.Layout, host: Host, opts: Options | None = None) -> None:
        self.layout, self.host, self.opts = layout, host, opts or Options()
        self.report = Report()
        self.env: dict | None = None            # the external .env, in memory only, never printed
        self.project: str | None = None
        self.info: dict | None = None
        self.model: Model | None = None
        self.subnet_text: str | None = None

    def add(self, code, level, detail="", **kw) -> None:
        self.report.add(code, level, detail, **kw)

    def run(self) -> Report:
        steps = (("LAYOUT", self.step_layout), ("PLATFORM", self.step_platform), ("ARCH_HOST", self.step_arch_host),
                 ("DOCKER_TARGET", self.step_docker_target), ("INHERITED_ENV", self.step_inherited_env),
                 ("DOCKER_DAEMON", self.step_daemon), ("COMPOSE_VERSION", self.step_compose_version),
                 ("BUILDX", self.step_buildx), ("CONFIG_DIR", self.step_config_set), ("ENV_PINS", self.step_pins), ("MODEL", self.step_model),
                 ("EXISTING_CONTAINERS", self.step_containers), ("EXISTING_VOLUMES", self.step_volumes),
                 ("EXISTING_NETWORKS", self.step_networks), ("EXISTING_IMAGES", self.step_images),
                 ("STATE_DIR", self.step_state), ("PORTS", self.step_ports), ("TUNNEL_SUBNET", self.step_tunnel),
                 ("NETWORK_POOLS", self.step_pools), ("DISK_DOCKER_ROOT", self.step_disk_docker),
                 ("DISK_STATE_DIR", self.step_disk_state), ("PERIMETER", self.step_notes))
        for code, step in steps:
            try:
                step()
            except Forbidden:
                raise
            except Exception as exc:  # noqa: BLE001 - a probe that blew up is unverified, never "ok"
                self.add(code, UNVERIFIED, f"probe failed ({type(exc).__name__})")
        return self.report

    def call(self, argv, cwd=None):
        """(Proc, None) or (None, reason): a command that cannot run is a reason, never output."""
        try:
            return self.host.run(argv, cwd=cwd), None
        except ProbeFailed as exc:
            return None, str(exc)

    def private(self, code: str, path, info, mode: int, what: str) -> None:
        uid = self.host.uid()
        if uid is None or not self.host.posix():
            self.add(code, UNVERIFIED, f"{what}: mode and owner cannot be proven on this platform")
        elif stat.S_IMODE(info.st_mode) != mode:
            self.add(code, REFUSED, f"{what} has mode {stat.S_IMODE(info.st_mode):04o}, expected {mode:04o}: {path}")
        elif info.st_uid != uid:
            self.add(code, REFUSED, f"{what} is owned by another user: {path}")

    # layout, host, docker
    def step_layout(self) -> None:
        findings = L.validate(self.layout, links="allow-missing")
        for f in findings:
            self.add(f"LAYOUT_{f.code}", REFUSED if f.level == L.REFUSE else UNVERIFIED, f.detail)
        if not findings:
            self.add("LAYOUT", OK, "paths and links are consistent (links that are missing are created by handoff)")
        if not self.layout.external_config:
            self.add("CONFIG_NOT_EXTERNAL", REFUSED, "the configuration set must be outside the checkout (--config-dir)")

    def step_platform(self) -> None:
        plat = self.host.platform()
        self.add("PLATFORM", OK if plat.startswith("linux") else REFUSED, plat)

    def step_arch_host(self) -> None:
        proc, why = self.call(["uname", "-m"])
        if proc is None or proc.returncode != 0:
            return self.add("ARCH_HOST", UNVERIFIED, f"uname -m could not be read ({why or 'rc=' + str(proc.returncode)})")
        value = proc.stdout.strip()
        if value in X86:
            self.add("ARCH_HOST", OK, value)
        elif re.fullmatch(r"[A-Za-z0-9_.-]{1,24}", value):
            self.add("ARCH_HOST", REFUSED, f"{value}: edu_ai is built for linux/amd64 only")
        else:
            self.add("ARCH_HOST", UNVERIFIED, "uname -m printed something unparsable")

    def step_docker_target(self) -> None:
        env = self.host.environ()
        names = []
        if env.get("DOCKER_HOST", "") not in ("", DEFAULT_SOCKET):
            names.append("DOCKER_HOST")
        if env.get("DOCKER_CONTEXT", "") not in ("", "default"):
            names.append("DOCKER_CONTEXT")
        if names:
            self.add("DOCKER_TARGET", UNVERIFIED, f"{', '.join(names)} set: this check reads the default daemon only, "
                                                  "while bootstrap would use the other target")

    def step_inherited_env(self) -> None:
        env = self.host.environ()
        try:
            referenced = set(install.scan_compose(self.host.read_bytes(self.layout.source_root / COMPOSE_BASE, MAX_ENV)
                                                  .decode("utf-8")).kinds)
        except (OSError, ValueError):
            return self.add("INHERITED_ENV", UNVERIFIED, "the Compose file could not be read, so shadowing variables are unknown")
        names = sorted(n for n in env if n.startswith("COMPOSE_") or n in _WATCHED or n in referenced)
        if names:
            self.add("INHERITED_ENV", UNVERIFIED, f"set in the environment and stronger than the external .env for Compose "
                                                  f"(bootstrap would inherit them, this check ignores them): {_names(names)}")

    def step_daemon(self) -> None:
        proc, why = self.call(["docker", "info", "--format", "{{json .}}"])
        if proc is None or proc.returncode != 0:
            return self.add("DOCKER_DAEMON", UNVERIFIED, f"docker info failed ({why or 'rc=' + str(proc.returncode)})")
        try:
            info = json.loads(proc.stdout)
            if not isinstance(info, dict) or not isinstance(info.get("ServerVersion"), str):
                raise ValueError("shape")
        except ValueError:
            return self.add("DOCKER_DAEMON", UNVERIFIED, "docker info printed something unparsable")
        self.info = info
        self.add("DOCKER_DAEMON", OK, f"reachable, server {info['ServerVersion']}")
        client = info.get("ClientInfo")
        if isinstance(client, dict) and client.get("Context") == "default":
            self.add("DOCKER_CONTEXT", OK, "default")
        else:
            self.add("DOCKER_CONTEXT", UNVERIFIED, "the client context is not proven to be the default")
        os_type = info.get("OSType")
        self.add("DOCKER_OS", OK if os_type == "linux" else REFUSED if isinstance(os_type, str) else UNVERIFIED,
                 os_type if isinstance(os_type, str) else "OSType missing")
        arch = info.get("Architecture")
        if arch in X86:
            self.add("ARCH_DOCKER", OK, arch)
        elif isinstance(arch, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,24}", arch):
            self.add("ARCH_DOCKER", REFUSED, f"{arch}: edu_ai is built for linux/amd64 only")
        else:
            self.add("ARCH_DOCKER", UNVERIFIED, "Architecture missing or unparsable")

    def step_compose_version(self) -> None:
        proc, why = self.call(["docker", "compose", "version", "--short"])
        if proc is None or proc.returncode != 0:
            return self.add("COMPOSE_VERSION", UNVERIFIED, f"docker compose version failed ({why or 'rc=' + str(proc.returncode)})")
        m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)\S*", proc.stdout.strip())
        if not m:
            return self.add("COMPOSE_VERSION", UNVERIFIED, "the version is unparsable")
        version = tuple(int(g) for g in m.groups())
        text = ".".join(map(str, version))
        minimum = ".".join(map(str, self.opts.min_compose))
        self.add("COMPOSE_VERSION", OK if version >= self.opts.min_compose else REFUSED,
                 f"{text}, minimum {minimum}" + ("" if version >= self.opts.min_compose else " (provisional)"))
        self.add("COMPOSE_MINIMUM", NOTE, "the minimum is PROVISIONAL (derived from the features used, not measured); verified: 5.3.1 only")

    def step_buildx(self) -> None:
        # Without buildx Compose uses the classic builder: TARGETARCH stays empty and the pg_search .deb URL is a 404 (lab 2026-10-07).
        proc, why = self.call(["docker", "buildx", "version"])
        if proc is None:
            return self.add("BUILDX", UNVERIFIED, f"docker buildx version could not be run ({why})")
        if proc.returncode != 0:
            return self.add("BUILDX", REFUSED, "docker buildx is missing: the images are built here and the build needs it")
        self.add("BUILDX", OK, "present")

    # the external configuration set (G2, K2)
    def step_config_set(self) -> None:
        lay, host = self.layout, self.host
        if not lay.external_config:
            return
        try:
            info = host.lstat(lay.config_dir)
        except FileNotFoundError:
            return                                      # LAYOUT_CONFIG_DIR_MISSING says it
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return self.add("CONFIG_DIR", REFUSED, f"not a real directory: {lay.config_dir}")
        before = len(self.report.items)
        self.private("CONFIG_DIR", lay.config_dir, info, 0o700, "the configuration directory")
        names = set(host.listdir(lay.config_dir))
        expected = {install.ENV_NAME, install.RECORD_NAME}
        if expected - names:
            self.add("CONFIG_FILES", REFUSED, f"incomplete set, missing: {_names(expected - names)}")
        if names - expected:
            self.add("CONFIG_FILES", REFUSED, f"unexpected entries: {_names(names - expected)}")
        contents = {}
        for name in sorted(expected & names):
            path = lay.config_dir / name
            finfo = host.lstat(path)
            if not stat.S_ISREG(finfo.st_mode):
                self.add("CONFIG_FILES", REFUSED, f"{name} is not a regular file")
                continue
            self.private("CONFIG_FILES", path, finfo, 0o600, name)
            try:
                contents[name] = host.read_bytes(path, MAX_ENV)
            except (OSError, ValueError) as exc:
                self.add("CONFIG_FILES", UNVERIFIED, f"{name} cannot be read ({type(exc).__name__})")
        if len(self.report.items) == before:
            self.add("CONFIG_DIR", OK, "directory 0700 and both files 0600, owned by the current user")
        self.step_fingerprint(contents)

    def step_fingerprint(self, contents: dict) -> None:
        env_raw, rec_raw = contents.get(install.ENV_NAME), contents.get(install.RECORD_NAME)
        values = record = None
        try:
            if env_raw is not None:
                values, _, _ = install.parse_assignments(env_raw.decode("utf-8"))
                self.env = values
            if rec_raw is not None:
                record = json.loads(rec_raw)
        except (ValueError, UnicodeError):
            pass
        state = "UNVERIFIABLE"
        try:
            wanted = record["langflow_secret_key_sha256"]
            if record.get("schema") == install.RECORD_SCHEMA and re.fullmatch(r"[0-9a-f]{64}", wanted):
                state = "MATCH" if install.sha256_hex(values[install.KEY_NAME]) == wanted else "MISMATCH"
        except (KeyError, TypeError, AttributeError):
            pass
        level = {"MATCH": OK, "MISMATCH": REFUSED}.get(state, UNVERIFIED)
        self.add("KEY_FINGERPRINT", level, state + (" (the recorded SHA-256 of LANGFLOW_SECRET_KEY)" if state == "MATCH" else ""))
        blockers = record.get("blockers") if isinstance(record, dict) else None
        if isinstance(blockers, list):
            shown = [b for b in blockers if isinstance(b, str) and re.fullmatch(r"[A-Za-z_]+(:[A-Za-z0-9_]+)?", b)]
            if shown:
                self.add("RECORD_BLOCKERS", NOTE, _names(shown))

    # pins in the external .env (G3, G4)
    def step_pins(self) -> None:
        env, lay = self.env, self.layout
        if env is None:
            self.project = lay.project
            return self.add("ENV_PINS", UNVERIFIED, "the external .env could not be read, so COMPOSE_FILE, the project and the tunnel subnet are unknown")
        raw = env.get("COMPOSE_FILE", "").strip()
        if not raw:
            self.add("COMPOSE_FILE", REFUSED, f"not pinned in the external .env: {COMPOSE_OVERRIDE} would be loaded automatically and "
                                              "publish the development ports on loopback; run pin-env")
        else:
            files = [pathlib.PurePosixPath(p).name for p in raw.split(env.get("COMPOSE_PATH_SEPARATOR") or ":") if p]
            if COMPOSE_OVERRIDE in files:
                self.add("COMPOSE_FILE", REFUSED, f"pinned, but it names {COMPOSE_OVERRIDE}")
            elif COMPOSE_BASE not in files:
                self.add("COMPOSE_FILE", REFUSED, f"pinned, but it does not name {COMPOSE_BASE}")
            else:
                self.add("COMPOSE_FILE", OK, f"pinned to {_names(files)}")
        in_env = env.get("COMPOSE_PROJECT_NAME", "").strip()
        if in_env and not _PROJECT_RE.match(in_env):
            self.add("PROJECT", REFUSED, "COMPOSE_PROJECT_NAME in the external .env is not a valid project name")
        elif in_env and lay.project and in_env != lay.project:
            self.add("PROJECT", REFUSED, f"the layout says {lay.project} but COMPOSE_PROJECT_NAME in the external .env says {in_env}")
        elif in_env or lay.project:
            self.project = lay.project or in_env
            self.add("PROJECT", OK, f"{self.project} ({'layout' if lay.project else '.env'})")
        else:
            self.add("PROJECT", UNVERIFIED, "no project name in the layout or the external .env; Compose would derive it from the directory name")
        self.subnet_text = env.get(TUNNEL_VAR, "").strip() or None
        if self.subnet_text is None:
            self.add("TUNNEL_SUBNET_PIN", UNVERIFIED, f"{TUNNEL_VAR} is not set in the external .env and the model of this SHA cannot "
                                                      "render without it; run pin-env --allocate-tunnel-subnet")
        else:
            try:
                tunnel_subnet.parse_candidate(self.subnet_text)
                self.add("TUNNEL_SUBNET_PIN", OK, "set and a valid private /28 or /29")
            except ValueError as exc:
                self.add("TUNNEL_SUBNET_PIN", REFUSED, str(exc).split(":", 1)[0])

    # the rendered model
    def step_model(self) -> None:
        if self.project is None:
            return self.add("MODEL", UNVERIFIED, "not rendered: the project name is unknown")
        if self.env is not None and self.subnet_text is None:
            return self.add("MODEL", UNVERIFIED, f"not rendered: {TUNNEL_VAR} is not set")
        lay = self.layout
        argv = ["docker", "compose", "-p", self.project, "--project-directory", str(lay.source_root), "--env-file", str(lay.env_path),
                "config", "--format", "json"]
        proc, why = self.call(argv, cwd=str(lay.source_root))
        if proc is None:
            return self.add("MODEL", UNVERIFIED, f"docker compose could not run ({why})")
        if proc.returncode != 0:
            causes = [f"variable {m} is missing" for m in re.findall(r"required variable (\w+) is missing", proc.stderr)]
            if re.search(r"env file .*not found", proc.stderr):
                causes.append("an env file is missing (the .env link of the checkout?)")
            return self.add("MODEL", UNVERIFIED, f"docker compose config failed (rc={proc.returncode})" + (": " + "; ".join(causes) if causes else ""))
        try:
            self.model = parse_model(json.loads(proc.stdout), self.project)
        except ValueError as exc:
            code = str(exc) if str(exc) in ("MODEL_SHAPE", "MODEL_NAME") else "MODEL_SHAPE"
            return self.add("MODEL", UNVERIFIED, f"{code}: the rendered model is not what this tool expects")
        m = self.model
        self.add("MODEL", OK, f"{len(m.images)} images, {len(m.networks)} networks, {len(m.volumes)} volumes, "
                              f"{len(m.ports)} fixed published ports")
        if m.tunnel_subnet and self.subnet_text and m.tunnel_subnet != self.subnet_text:
            self.add("TUNNEL_SUBNET_MODEL", REFUSED, "the rendered subnet of `tunnel` differs from the one pinned in the external .env")
        if m.dynamic_or_udp:
            self.add("PORTS_UNLISTED", UNVERIFIED, f"UDP or dynamic ports of {_names(m.dynamic_or_udp)} cannot be checked by a TCP listener list")
        self.step_override_in_model()

    def step_override_in_model(self) -> None:
        """If COMPOSE_FILE were not honoured by --env-file, the override would be in the model: its ports would show up."""
        try:
            text = self.host.read_bytes(self.layout.source_root / COMPOSE_OVERRIDE, MAX_ENV).decode("utf-8")
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeError) as exc:
            return self.add("MODEL_OVERRIDE", UNVERIFIED, f"{COMPOSE_OVERRIDE} cannot be read ({type(exc).__name__})")
        override = {int(p) for p in re.findall(r'^\s*-\s*"?(?:[\d.]+:)?(\d{1,5}):\d{1,5}"?\s*$', text, re.M)}
        leaked = sorted(override & {p.number for p in self.model.ports})
        if leaked:
            self.add("MODEL_OVERRIDE", REFUSED, f"the model publishes ports of {COMPOSE_OVERRIDE}: {', '.join(map(str, leaked))}")
        else:
            self.add("MODEL_OVERRIDE", OK, f"no port of {COMPOSE_OVERRIDE} is in the model")

    # G1/G3: nothing of this installation may exist yet
    def _existing(self, code: str, argv: list[str], name_field: str, wanted: dict | None, split: bool = False) -> None:
        proc, why = self.call(argv)
        if proc is None or proc.returncode != 0:
            return self.add(code, UNVERIFIED, f"listing failed ({why or 'rc=' + str(proc.returncode)})")
        try:
            rows = _json_lines(proc.stdout)
            hits = set()
            for row in rows:
                raw = row[name_field]
                labels = row.get("Labels") or ""
                if not isinstance(raw, str) or not isinstance(labels, str):
                    raise ValueError("row")
                names = [n.lstrip("/") for n in raw.split(",")] if split else [raw]
                labelled = self.project is not None and f"{PROJECT_LABEL}={self.project}" in labels.split(",")
                hits.update(n for n in names if (wanted is not None and n in wanted) or labelled)
        except (ValueError, KeyError):
            return self.add(code, UNVERIFIED, "the listing is unparsable")
        if hits:
            self.add(code, REFUSED, f"already exist (collisions only are listed): {_names(hits)}")
        elif wanted is None or self.project is None:
            self.add(code, UNVERIFIED, "none carries the project label, but the names of the model are unknown", needs_model=True)
        else:
            self.add(code, OK, "none by model name or by project label")

    def _wanted(self, attr: str) -> dict | None:
        return getattr(self.model, attr) if self.model else None

    def step_containers(self) -> None:
        self._existing("EXISTING_CONTAINERS", ["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"], "Names",
                       self._wanted("containers"), split=True)

    def step_volumes(self) -> None:
        self._existing("EXISTING_VOLUMES", ["docker", "volume", "ls", "--format", "{{json .}}"], "Name", self._wanted("volumes"))

    def step_networks(self) -> None:
        self._existing("EXISTING_NETWORKS", ["docker", "network", "ls", "--no-trunc", "--format", "{{json .}}"], "Name",
                       self._wanted("networks"))

    def step_images(self) -> None:
        if self.model is None:
            return self.add("EXISTING_IMAGES", UNVERIFIED, "image tags of the model are unknown", needs_model=True)
        proc, why = self.call(["docker", "image", "ls", "--format", "{{json .}}"])
        if proc is None or proc.returncode != 0:
            return self.add("EXISTING_IMAGES", UNVERIFIED, f"listing failed ({why or 'rc=' + str(proc.returncode)})")
        try:
            have = set()
            for row in _json_lines(proc.stdout):
                repo, tag = row["Repository"], row["Tag"]
                if not isinstance(repo, str) or not isinstance(tag, str):
                    raise ValueError("row")
                if repo != "<none>" and tag != "<none>":
                    have.add(_split_image(f"{repo}:{tag}"))
            have.discard(None)
        except (ValueError, KeyError):
            return self.add("EXISTING_IMAGES", UNVERIFIED, "the listing is unparsable")
        built, pulled = [], []
        for ref, is_built in self.model.images.items():
            if _split_image(ref) in have:
                (built if is_built or self.opts.refuse_pulled_images else pulled).append(ref)
        if built:
            self.add("EXISTING_IMAGES", REFUSED, f"tags that this installation builds already exist: {_names(built)}")
        else:
            self.add("EXISTING_IMAGES", OK, "no tag that this installation builds exists")
        if pulled:
            self.add("IMAGES_PRESENT_PULLED", NOTE, f"public images already on the host will be reused: {_names(pulled)}")

    def step_state(self) -> None:
        lay, host = self.layout, self.host
        empty, problems = [], []
        if lay.external_state:
            try:
                info = host.lstat(lay.state_dir)
            except FileNotFoundError:
                info = None                              # handoff creates it, so its parent must be a directory
                try:
                    if not stat.S_ISDIR(host.lstat(lay.state_dir.parent).st_mode):
                        problems.append("the parent of the state directory is not a directory")
                except FileNotFoundError:
                    problems.append("the parent of the state directory does not exist")
            if info is not None:
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    problems.append("the state directory is not a real directory")
                else:
                    extra = [n for n in host.listdir(lay.state_dir) if n not in L.STATE_DIRS]
                    if extra:
                        problems.append(f"unexpected entries in the state directory: {_names(extra)}")
        for name in L.STATE_DIRS:
            path = lay.state_path(name)
            try:
                info = host.lstat(path)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) and not lay.external_state:
                problems.append(f"{name} is a symlink in the checkout")
            elif stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                problems.append(f"{name} is not a real directory")
            elif host.listdir(path):
                problems.append(f"{name} is not empty")
            else:
                empty.append(name)
        if problems:
            self.add("STATE_DIR", REFUSED, "; ".join(problems))
        else:
            self.add("STATE_DIR", OK, f"holds no data ({len(empty)} empty directories already exist)")

    # G4: ports from the model against every TCP listener
    def step_ports(self) -> None:
        if self.model is None:
            return self.add("PORTS", UNVERIFIED, "published ports are unknown: the model was not rendered", needs_model=True)
        proc, why = self.call(["ss", "-H", "-ltn"])
        if proc is None or proc.returncode != 0:
            return self.add("PORTS", UNVERIFIED, f"ss could not be read ({why or 'rc=' + str(proc.returncode)})")
        listening = parse_ss(proc.stdout)
        if listening is None:
            return self.add("PORTS", UNVERIFIED, "ss printed a line this tool does not understand")
        busy = sorted({(p.number, p.service) for p in self.model.ports if p.number in listening})
        if busy:
            self.add("PORTS", REFUSED, "a listener already holds: " + ", ".join(f"{n} ({svc})" for n, svc in busy))
        else:
            self.add("PORTS", OK, f"no listener on any of {len({p.number for p in self.model.ports})} published ports (all addresses, v4 and v6)")

    # G5
    def step_tunnel(self) -> None:
        cidr = (self.model.tunnel_subnet if self.model else None) or self.subnet_text
        if cidr is None or self.project is None:
            return                                          # TUNNEL_SUBNET_PIN or PROJECT already said so
        argv = [sys.executable, str(TUNNEL_SCRIPT), "check", cidr, "--project", self.project, "--routes", "host"]
        for r in self.opts.reserved:
            argv += ["--reserved", r]
        proc, why = self.call(argv)
        if proc is None:
            return self.add("TUNNEL_SUBNET", UNVERIFIED, f"tunnel_subnet.py could not run ({why})")
        if proc.returncode == 0:
            self.add("TUNNEL_SUBNET", OK, "tunnel_subnet.py check found no conflict in what it can see (repeat before every start)")
        elif proc.returncode == 2:
            kinds = sorted(set(re.findall(r"^REFUSED:? ?([A-Z_]{3,}):?", proc.stdout, re.M)))
            self.add("TUNNEL_SUBNET", REFUSED, "conflict: " + (", ".join(kinds) if kinds else "see tunnel_subnet.py check") )
        else:
            self.add("TUNNEL_SUBNET", UNVERIFIED, f"tunnel_subnet.py could not inspect the host (rc={proc.returncode})")

    def step_pools(self) -> None:
        if self.info is None:
            return self.add("NETWORK_POOLS", UNVERIFIED, "the address pools of the daemon are unknown (docker info failed)")
        pools = self.info.get("DefaultAddressPools")
        text = _names(f"{p.get('Base')}/{p.get('Size')}" for p in pools if isinstance(p, dict)) if isinstance(pools, list) and pools \
            else "the built-in defaults"
        auto = sorted(k for k in (self.model.networks.values() if self.model else []) if k != "tunnel")
        which = f"the {len(auto)} auto-allocated networks ({_names(auto)})" if self.model else "the auto-allocated networks (model not rendered)"
        detail = (f"{which} get their addresses from {text}; whether those blocks "
                  "collide with host routes, LAN or VPN ranges is NOT measured and Docker's behaviour on a collision is unmeasured")
        if self.opts.accept_unmeasured_pools:
            self.add("NETWORK_POOLS", NOTE, "accepted by the operator: " + detail)
        else:
            self.add("NETWORK_POOLS", UNVERIFIED, detail + " (--accept-unmeasured-network-pools records the decision)")

    def _disk(self, code: str, path, what: str) -> None:
        try:
            free = self.host.disk_free(path)
        except OSError as exc:
            return self.add(code, UNVERIFIED, f"{what} cannot be measured from here ({type(exc).__name__})")
        gib, need = free / 1024 ** 3, self.opts.min_free_gb
        self.add(code, OK if gib >= need else REFUSED,
                 f"{gib:.1f} GiB available at {path}, provisional minimum {need:g} GiB" + ("" if gib >= need else " (unmeasured threshold)"))

    def step_disk_docker(self) -> None:
        if self.info is None:
            return self.add("DISK_DOCKER_ROOT", UNVERIFIED, "DockerRootDir is unknown (docker info failed)")
        root = self.info.get("DockerRootDir")
        if not isinstance(root, str) or not posixpath.isabs(root):
            return self.add("DISK_DOCKER_ROOT", UNVERIFIED, "DockerRootDir missing or not an absolute path")
        if "Docker Desktop" in str(self.info.get("OperatingSystem", "")):
            return self.add("DISK_DOCKER_ROOT", UNVERIFIED, "the Docker root lives inside a VM that the host cannot measure")
        self._disk("DISK_DOCKER_ROOT", root, "the Docker root")

    def step_disk_state(self) -> None:
        path = self.layout.state_dir
        try:
            self.host.lstat(path)
        except FileNotFoundError:
            path = path.parent                                  # it will be created next to its parent
        self._disk("DISK_STATE_DIR", path, "the state directory")

    def step_notes(self) -> None:
        self.add("DISK_THRESHOLD", NOTE, f"the disk minimum of {self.opts.min_free_gb:g} GiB is PROVISIONAL (unmeasured); see --help")
        self.add("PERIMETER", NOTE, "firewall and public exposure are not checked by this tool (G6)")


# ── pin-env ──────────────────────────────────────────────────────────────────
HEADER = b"# -- Added by scripts/host_preflight.py pin-env --\n"


def _valid_pair(spec: str) -> tuple[str, str] | None:
    name, sep, value = spec.partition("=")
    if (not sep or not _VAR_RE.match(name) or any(p in name for p in _SECRET_PARTS) or value != value.strip() or " #" in value
            or any(c in value for c in "$\r\n\0")):
        return None
    return name, value


def pin_env(layout: L.Layout, host: Host, actions: Actions, *, tunnel_subnet_value: str | None = None,
            allocate: bool = False, extra: tuple = (), reserved: tuple = ()) -> Report:
    report = Report("pin-env")
    path = layout.env_path
    if not layout.external_config:
        report.add("PIN_ENV", REFUSED, "the configuration set must be outside the checkout (--config-dir)")
        return report
    try:
        info = host.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            report.add("PIN_ENV", REFUSED, f"not a regular file: {path}")
            return report
        uid = host.uid()
        if uid is None or not host.posix():
            report.add("PIN_ENV", UNVERIFIED, "mode and owner of the .env cannot be proven on this platform")
            return report
        if stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != uid:
            report.add("PIN_ENV", REFUSED, "the .env is not private (mode 0600, current user) or is owned by another user")
            return report
        old = host.read_bytes(path, MAX_ENV)
        values, _, _ = install.parse_assignments(old.decode("utf-8"))
    except FileNotFoundError:
        report.add("PIN_ENV", REFUSED, f"the external .env does not exist: {path}")
        return report
    except (OSError, ValueError, UnicodeError) as exc:
        report.add("PIN_ENV", UNVERIFIED, f".env could not be read ({type(exc).__name__}); nothing was written")
        return report
    if b"\r" in old:
        report.add("PIN_ENV", REFUSED, "the .env has CR characters; Compose would keep them in values. Nothing was written")
        return report
    wanted: dict[str, str] = {"COMPOSE_FILE": COMPOSE_BASE, "COMPOSE_PROJECT_NAME": layout.project or DEFAULT_PROJECT}
    if tunnel_subnet_value is not None:
        try:
            tunnel_subnet.parse_candidate(tunnel_subnet_value)
        except ValueError as exc:
            report.add("PIN_ENV", REFUSED, f"--tunnel-subnet is not usable: {str(exc).split(':', 1)[0]}")
            return report
        wanted[TUNNEL_VAR] = tunnel_subnet_value.strip()
    elif allocate and TUNNEL_VAR not in values:
        project = layout.project or values.get("COMPOSE_PROJECT_NAME") or DEFAULT_PROJECT
        argv = [sys.executable, str(TUNNEL_SCRIPT), "allocate", "--project", project, "--routes", "host"]
        for r in reserved:
            argv += ["--reserved", r]
        try:
            proc = host.run(argv)
        except ProbeFailed as exc:
            report.add("TUNNEL_SUBNET", UNVERIFIED, f"tunnel_subnet.py could not run ({exc}); nothing was written")
            return report
        found = re.fullmatch(rf"{TUNNEL_VAR}=(\S+)", proc.stdout.strip())
        if proc.returncode == 0 and found:
            try:
                tunnel_subnet.parse_candidate(found.group(1))
                wanted[TUNNEL_VAR] = found.group(1)
            except ValueError:
                pass
        if TUNNEL_VAR not in wanted:
            report.add("TUNNEL_SUBNET", REFUSED if proc.returncode == 2 else UNVERIFIED,
                       f"tunnel_subnet.py allocate gave no usable subnet (rc={proc.returncode}); nothing was written")
            return report
    for spec in extra:
        pair = _valid_pair(spec)
        if pair is None:
            report.add("PIN_ENV", REFUSED, "--set takes NAME=VALUE with a plain upper-case NAME that is not secret-like and a value without $ or line breaks")
            return report
        wanted[pair[0]] = pair[1]
    add = {k: v for k, v in wanted.items() if k not in values}
    kept = sorted(k for k in wanted if k in values)
    differs = sorted(k for k in kept if values[k] != wanted[k])
    if kept:
        report.add("PIN_ENV_KEPT", NOTE, f"already present, not touched: {_names(kept)}")
    if differs:
        report.add("PIN_ENV_DIFFERS", NOTE, f"present with another value than the one this tool would pin (not changed): {_names(differs)}")
    if TUNNEL_VAR not in values and TUNNEL_VAR not in wanted:
        report.add("PIN_ENV_ABSENT", NOTE, f"{TUNNEL_VAR} is still absent: pass --tunnel-subnet or --allocate-tunnel-subnet")
    if not add:
        report.add("PIN_ENV", OK, "nothing to add")
        return report
    head = old if not old or old.endswith(b"\n") else old + b"\n"
    new = head + (b"\n" if head.strip() else b"") + HEADER + "".join(f"{k}={v}\n" for k, v in add.items()).encode("utf-8")
    try:
        if host.read_bytes(path, MAX_ENV) != old:
            report.add("PIN_ENV", UNVERIFIED, "the .env changed while this ran; nothing was written")
            return report
        actions.write_env(path, new)
    except BaseException as exc:  # noqa: BLE001 - the disk, not the exception, decides what happened
        try:
            now = host.read_bytes(path, MAX_ENV)
        except (OSError, ValueError):
            now = None
        if now == old:
            report.add("PIN_ENV", UNVERIFIED, f"the write failed ({type(exc).__name__}); the .env is byte-identical to before")
        elif now == new:
            report.add("PIN_ENV_ADDED", OK, f"{_names(add)} (the write completed before the {type(exc).__name__})")
        else:
            report.add("PIN_ENV", UNVERIFIED, f"the write failed ({type(exc).__name__}) and the state of the .env is unknown")
        return report
    try:
        ok = host.read_bytes(path, MAX_ENV) == new
    except (OSError, ValueError):
        ok = False
    report.add("PIN_ENV_ADDED" if ok else "PIN_ENV", OK if ok else UNVERIFIED,
               _names(add) if ok else "the .env could not be read back after the write")
    return report


# ── handoff ──────────────────────────────────────────────────────────────────
def bootstrap_command(layout: L.Layout) -> list[str]:
    return ["python3", str(layout.source_root / "scripts" / "bootstrap.py"), *layout.argv()]


def _create(layout: L.Layout, host: Host, actions: Actions, report: Report, only_env_link: bool) -> bool:
    """Create what is missing; a name that exists is never touched. True when every step worked."""
    made_dirs, made_links = [], []
    try:
        if layout.external_state and not only_env_link:
            if not _exists(host, layout.state_dir):
                actions.mkdir(layout.state_dir, 0o755)
                made_dirs.append(layout.state_dir.name)
            for name in L.STATE_DIRS:
                path = layout.state_dir / name
                if not _exists(host, path):
                    actions.mkdir(path, bootstrap.DATA_DIRS[name][2])
                    made_dirs.append(name)
        for link in layout.links():
            if link.state == "MISSING" and (not only_env_link or link.name == install.ENV_NAME):
                actions.symlink(str(link.target), str(link.path))
                made_links.append(link.name)
    except OSError as exc:
        report.add("HANDOFF", REFUSED if isinstance(exc, FileExistsError) else UNVERIFIED,
                   f"{type(exc).__name__} while creating; what was made stays and is valid, nothing was replaced or removed "
                   f"(made: {_names(made_dirs + made_links) or 'nothing'})")
        return False
    report.add("HANDOFF_STATE_DIRS", OK if made_dirs else NOTE, f"created: {_names(made_dirs)}" if made_dirs else "none to create")
    report.add("HANDOFF_LINKS", OK if made_links else NOTE, f"created: {_names(made_links)}" if made_links else "none to create")
    return True


def _exists(host: Host, path) -> bool:
    try:
        host.lstat(path)
        return True
    except FileNotFoundError:
        return False


def handoff(layout: L.Layout, host: Host, actions: Actions, opts: Options, *, env_link_only: bool = False,
            do_exec: bool = False, out=None) -> int:
    first = Preflight(layout, host, opts).run()
    report = first
    env_link_missing = any(l.name == install.ENV_NAME and l.state == "MISSING" for l in layout.links())
    if env_link_only:
        link_first = ("MODEL", "NETWORK_POOLS")          # unverified until the link exists, or unrelated to it
        blockers = [i for i in first.items if i.level == REFUSED or (i.level == UNVERIFIED and not i.needs_model and i.code not in link_first)]
        if blockers or not env_link_missing:
            first.add("HANDOFF", NOTE, "not performed: --env-link-only needs no refusal, only the model unverified, and a missing .env link")
        else:
            made = Report("handoff")
            if _create(layout, host, actions, made, only_env_link=True):
                report = Preflight(layout, host, opts).run()
                report.items = made.items + report.items
                report.add("HANDOFF", NOTE, "only the .env link was created; run handoff again for the rest")
            else:
                report = made
    elif report.exit_code() != 0:
        report.add("HANDOFF", NOTE, "not performed: the check above is not clean, so nothing was created")
    else:
        made = Report("handoff")
        if _create(layout, host, actions, made, only_env_link=False):
            findings = L.validate(layout, links="require")
            for f in findings:
                made.add(f"HANDOFF_{f.code}", REFUSED if f.level == L.REFUSE else UNVERIFIED, f.detail)
            if not findings:
                made.add("HANDOFF_VERIFY", OK, "links and state directories verified against the layout (links required)")
                made.add("NEXT_COMMAND", NOTE, shlex.join(bootstrap_command(layout)))
        report.items += made.items
    report.emit(out)
    code = report.exit_code()
    if do_exec and code == 0 and not env_link_only and any(i.code == "NEXT_COMMAND" for i in report.items):
        return _exec_bootstrap(layout, host, actions, out)
    return code


def _exec_bootstrap(layout: L.Layout, host: Host, actions: Actions, out) -> int:
    out = sys.stdout if out is None else out
    env = child_env(host.environ())
    for name in (*install._CI_FLAGS, *bootstrap._CI_VARS):      # the CI rule that hides generated secrets must survive
        if name in host.environ():
            env[name] = host.environ()[name]
    argv = [sys.executable, *bootstrap_command(layout)[1:]]
    print("BOOTSTRAP_LAUNCHED: NOTE the installation starts now; its own exit status follows (it is not a verification)", file=out)
    out.flush()
    rc = actions.bootstrap(argv, str(layout.source_root), env)
    meaning = {0: "finished", 3: "a key could not be verified, nothing changed there, repeat later"}.get(
        rc, "failed, possibly after partial changes; this is not 'nothing changed'")
    print(f"BOOTSTRAP_EXIT: {rc} {meaning}", file=out)
    return 0 if rc == 0 else 3


# ── command line ─────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    L.add_arguments(common)
    check = argparse.ArgumentParser(add_help=False)
    check.add_argument("--min-free-gb", type=float, default=MIN_FREE_GB, metavar="GiB",
                       help=f"PROVISIONAL (unmeasured) minimum of available space in the Docker root and the state dir (default {MIN_FREE_GB:g})")
    check.add_argument("--min-compose", default=".".join(map(str, MIN_COMPOSE)), metavar="X.Y.Z",
                       help="PROVISIONAL minimum Docker Compose version (verified: 5.3.1)")
    check.add_argument("--reserved", action="append", default=[], metavar="CIDR", help="a LAN, VPN or VPC range the tunnel subnet must avoid")
    check.add_argument("--refuse-pulled-images", action="store_true", help="also refuse when a public image of the model is already on the host")
    check.add_argument("--accept-unmeasured-network-pools", action="store_true",
                       help="record that the operator accepts that the auto-allocated networks are not proven free of host ranges")
    sub.add_parser("check", parents=[common, check], help="read-only host preflight")
    hand = sub.add_parser("handoff", parents=[common, check], help="check, then create missing links and print the bootstrap command")
    hand.add_argument("--env-link-only", action="store_true", help="create only the .env link (the model needs it to render), then check again")
    hand.add_argument("--exec", dest="do_exec", action="store_true", help="run bootstrap with the layout flags (default: print the command only)")
    pin = sub.add_parser("pin-env", parents=[common], help="append ONLY absent keys to the external .env")
    pin.add_argument("--tunnel-subnet", default=None, metavar="CIDR")
    pin.add_argument("--allocate-tunnel-subnet", action="store_true", help="ask tunnel_subnet.py allocate (read-only) for a subnet")
    pin.add_argument("--reserved", action="append", default=[], metavar="CIDR")
    pin.add_argument("--set", dest="extra", action="append", default=[], metavar="NAME=VALUE",
                     help="a non-secret key the model needs; secret-like names are refused")
    return parser


def _options(args) -> Options | None:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", args.min_compose)
    if not m or not args.min_free_gb >= 0:
        return None
    return Options(args.min_free_gb, tuple(int(g) for g in m.groups()), tuple(args.reserved), args.refuse_pulled_images,
                   args.accept_unmeasured_network_pools)


def main(argv=None, *, host: Host | None = None, actions: Actions | None = None, environ=None, out=None) -> int:
    host, actions = host or Host(), actions or Actions()
    try:
        args = build_parser().parse_args(argv)
        report = Report()
        try:
            layout = L.resolve(args, os.environ if environ is None else environ, SCRIPTS.parent)
        except L.LayoutError as exc:
            report.add("LAYOUT", REFUSED, f"{exc.code} ({exc.field})")
            report.emit(out)
            return report.exit_code()
        if args.cmd == "pin-env":
            report = pin_env(layout, host, actions, tunnel_subnet_value=args.tunnel_subnet, allocate=args.allocate_tunnel_subnet,
                             extra=tuple(args.extra), reserved=tuple(args.reserved))
        else:
            opts = _options(args)
            if opts is None:
                report.add("OPTIONS", REFUSED, "--min-compose must be X.Y.Z and --min-free-gb must not be negative")
            elif args.cmd == "handoff":
                return handoff(layout, host, actions, opts, env_link_only=args.env_link_only, do_exec=args.do_exec, out=out)
            else:
                report = Preflight(layout, host, opts).run()
    except Forbidden as exc:
        report = Report()
        report.add("INTERNAL_ERROR", UNVERIFIED, f"a command outside the whitelist was refused: {exc}")
    except KeyboardInterrupt:
        report = Report()
        report.add("INTERRUPTED", UNVERIFIED, "interrupted; the state of anything this tool was writing is decided by the disk")
    except Exception as exc:  # noqa: BLE001 - a bug must not print a traceback that holds values
        report = Report()
        report.add("INTERNAL_ERROR", UNVERIFIED, type(exc).__name__)
    report.emit(out)
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
