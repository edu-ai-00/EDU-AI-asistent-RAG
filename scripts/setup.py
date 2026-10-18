#!/usr/bin/env python3
"""Guided clean install: one entry point over install.py, host_preflight.py, bootstrap.py and db_roles.py.

    setup.py plan    [--config-dir D] [--state-dir D] [--project NAME]       read-only overview
    setup.py install [same options] [--no-input] [--accept-unmeasured-network-pools]

The source is THIS checkout: its HEAD is read once, recorded in full and must stay the same until the end; nothing is
fetched. `plan` creates no config, link, container or package. `install` runs the existing tools in order: init-config,
pin-env, handoff (the .env link first, then the full check), bootstrap (with its own smoke test) and the role check.
It reuses the existing environment checks and proves ownership of a running stack before resuming. It adds no generator
or school-setting flag: the school is chosen later, in the browser. A new configuration
gets the existing CHAT_ENTITY_PICKER=true through pin-env; an existing one keeps what it has.

Nothing is ever deleted, replaced or forced. A repeated run continues only on proof that config, key, project and Compose
model are this installation's own; the commit itself may differ. Keys of this commit's .env.example that the .env
lacks are added (template value, or a generated secret). Otherwise it refuses and says how to look. Secrets never go through arguments or this tool's output:
the one-time overview of init-config appears only on the terminal that child inherits.
Exit status: 0 backend ready | 2 refused | 3 could not verify or complete | a failing child's own status otherwise.
Standard library only.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from typing import NamedTuple

# A cold plan must not create local import caches in the source checkout.
sys.dont_write_bytecode = True

SCRIPTS = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import eduai_layout as L  # noqa: E402
import install  # noqa: E402
import host_preflight as HP  # noqa: E402
from envfile import is_placeholder, set_env  # noqa: E402

DEFAULT_PROJECT = "edu_ai"
DEFAULT_BASE = "edu-ai"                      # ~/edu-ai/{config,state}: the layout of docs/CLEAN_INSTALL.md
REFUSED, UNVERIFIED = "REFUSED", "UNVERIFIED"
ENV_NAME, RECORD_NAME = install.ENV_NAME, install.RECORD_NAME
POSTGRES_CONTAINER = "edu_ai_postgres"       # container_name in docker-compose.yml
NGINX_CONTAINER = "edu_ai_nginx"
PICKER_VAR, ENTITY_VAR = "CHAT_ENTITY_PICKER", "CHAT_LEGAL_ENTITY"
LANGFLOW_URL_PIN = "LANGFLOW_URL=http://127.0.0.1:8444"
_ITEM = re.compile(r"^([A-Z][A-Z0-9_]*): (REFUSED|UNVERIFIED)(?: (.*))?$")      # a host_preflight line
_OFFLINE = re.compile(r"^(REFUSED|UNVERIFIED) ([A-Z][A-Z0-9_]*)(?:: (.*))?$")    # an install.py line
_YES = ("yes",)


class Proc(NamedTuple):
    rc: int
    out: str
    err: str


class Host:
    """Everything outside this process. Tests replace it; the real one only runs argv lists, never a shell."""

    def __init__(self) -> None:
        self.environ = dict(os.environ)

    def run(self, argv, *, capture: bool = True, timeout: int = 900) -> Proc:
        """capture=False lets the child use this terminal (needed for its one-time secret overview and its progress)."""
        try:
            environment = HP.child_env(self.environ)
            environment.update(DOCKER_HOST=HP.DEFAULT_SOCKET, DOCKER_CONTEXT="default", PYTHONDONTWRITEBYTECODE="1")
            if not capture:
                return Proc(subprocess.run([str(a) for a in argv], env=environment, timeout=timeout).returncode, "", "")
            done = subprocess.run([str(a) for a in argv], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  stdin=subprocess.DEVNULL, env=environment, timeout=timeout)
            return Proc(done.returncode, done.stdout, done.stderr)
        except FileNotFoundError:
            return Proc(127, "", "not found")
        except (OSError, subprocess.SubprocessError) as exc:
            return Proc(126, "", type(exc).__name__)

    def git(self, root, *args) -> Proc:
        try:
            done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", stdin=subprocess.DEVNULL, env=install._git_env(), timeout=60)
            return Proc(done.returncode, done.stdout, done.stderr)
        except FileNotFoundError:
            return Proc(127, "", "git not found")
        except (OSError, subprocess.SubprocessError) as exc:
            return Proc(126, "", type(exc).__name__)

    def container_file(self, container_id: str, target: str, limit: int = 1 << 16) -> bytes | None:
        """Bytes of ONE regular file inside a container (running or stopped), or None. Read-only; `docker cp` streams a tar."""
        try:
            environment = HP.child_env(self.environ)
            environment.update(DOCKER_HOST=HP.DEFAULT_SOCKET, DOCKER_CONTEXT="default", PYTHONDONTWRITEBYTECODE="1")
            done = subprocess.run(["docker", "cp", f"{container_id}:{target}", "-"], capture_output=True,
                                  stdin=subprocess.DEVNULL, env=environment, timeout=60)
            if done.returncode != 0 or len(done.stdout) > 4 * limit:
                return None
            with tarfile.open(fileobj=io.BytesIO(done.stdout), mode="r:") as archive:
                members = archive.getmembers()
                if len(members) != 1 or not members[0].isreg() or members[0].size > limit:
                    return None
                return archive.extractfile(members[0]).read()
        except (OSError, subprocess.SubprocessError, tarfile.TarError, AttributeError):
            return None

    def interactive(self) -> bool:
        return install.interactive_terminal()

    def ask(self, prompt: str) -> str:
        return input(prompt)

    def say(self, line: str = "") -> None:
        print(line, flush=True)

    def platform(self) -> str:
        return sys.platform

    def home(self) -> str:
        return self.environ.get("HOME") or os.path.expanduser("~")

    def lan_ip(self) -> str | None:
        """The address the host would use towards a private network. A UDP connect sends nothing."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("192.0.2.1", 9))
                ip = sock.getsockname()[0]
            return None if ip.startswith(("127.", "0.")) else ip
        except OSError:
            return None


# ── what is on the disk and the host (read-only) ─────────────────────────────
@dataclass
class State:
    layout: L.Layout | None = None
    sha: str | None = None
    config: str = "absent"                    # absent | own | refused
    resume_mode: str = "full"                 # full | bootstrap-only (own stack and links already there)
    env_values: dict | None = None
    notes: list = field(default_factory=list)
    findings: list = field(default_factory=list)    # (level, code, detail)
    new_parents: list = field(default_factory=list)
    missing_keys: list = field(default_factory=list)  # template keys the own .env lacks

    def refuse(self, code: str, detail: str = "") -> None:
        self.findings.append((REFUSED, code, detail))

    def unverified(self, code: str, detail: str = "") -> None:
        self.findings.append((UNVERIFIED, code, detail))

    def exit_code(self) -> int:
        levels = {f[0] for f in self.findings}
        return 2 if REFUSED in levels else 3 if UNVERIFIED in levels else 0


def default_paths(host: Host) -> tuple[str, str]:
    base = pathlib.Path(host.home()) / DEFAULT_BASE
    return str(base / "config"), str(base / "state")


def make_layout(config: str, state: str, project: str, root=ROOT) -> L.Layout:
    ns = argparse.Namespace(layout_source_root=str(root), layout_config_dir=config, layout_state_dir=state, layout_project=project)
    return L.resolve(ns, environ={}, default_root=root)           # flags only: inherited EDU_AI_* names never decide


def _nested(a, b) -> bool:
    ra, rb = os.path.realpath(a), os.path.realpath(b)
    try:
        return os.path.commonpath([ra, rb]) in (ra, rb)
    except ValueError:
        return False


def own_config(layout: L.Layout, sha: str | None = None, *, newly_created: bool = False) -> tuple[dict | None, str, str]:
    """(env values, '', '') when the config set is provably this installation's own; else (None, code, detail)."""
    cfg = str(layout.config_dir)
    try:
        kind, detail = install.classify_target(cfg)
    except FileNotFoundError:
        return None, "CONFIG_ABSENT", ""
    except OSError as exc:
        return None, "CONFIG_UNREADABLE", type(exc).__name__
    if kind != "config-set":
        return None, "TARGET_EXISTS_" + kind.upper().replace("-", "_"), "not a set of this tool; nothing is touched"
    if detail != "MATCH":
        return None, "CONFIG_KEY_FINGERPRINT_" + detail, "the recorded key does not match the .env"
    try:
        record = json.loads(install._read_regular(os.path.join(cfg, RECORD_NAME), 1 << 16))
        values, _, _ = install.parse_assignments(install._read_regular(os.path.join(cfg, ENV_NAME), install.MAX_BLOB).decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None, "CONFIG_UNREADABLE", "record or .env"
    # Bound to the checkout, not to a commit: another commit of THIS checkout continues, another clone is refused.
    # A record from before source_root existed keeps the old rule (same commit). History is not compared.
    recorded_root = record.get("source_root")
    if "source_root" in record:
        if not isinstance(recorded_root, str) or not os.path.isabs(recorded_root):
            return None, "CONFIG_FROM_ANOTHER_CHECKOUT", "the record's source_root is not an absolute path; nothing is touched"
        if os.path.realpath(recorded_root) != os.path.realpath(layout.source_root):
            return None, "CONFIG_FROM_ANOTHER_CHECKOUT", "the record names another source checkout; nothing is touched"
    elif sha is None or record.get("source_sha") != sha:
        return None, "CONFIG_FROM_ANOTHER_SOURCE", "the record predates checkout binding and names another commit"
    named = values.get("COMPOSE_PROJECT_NAME", "").strip()
    if named and named != layout.project and not newly_created:
        return None, "CONFIG_OTHER_PROJECT", "COMPOSE_PROJECT_NAME of the .env differs from the project asked for"
    compose_file = values.get("COMPOSE_FILE", "")
    if compose_file:
        files = compose_file.split(values.get("COMPOSE_PATH_SEPARATOR") or ":")
        if (len(files) != 1 or os.path.realpath(layout.source_root / files[0])
                != os.path.realpath(layout.source_root / HP.COMPOSE_BASE)):
            return None, "CONFIG_OTHER_COMPOSE_MODEL", "the stored Compose file differs from this checkout's base model; nothing is changed"
    return values, "", ""


def bind_new_project(layout: L.Layout, sha: str) -> tuple[dict | None, str, str]:
    """Only the set just published by successful init-config may change its template project."""
    values, code, detail = own_config(layout, sha, newly_created=True)
    if values is None:
        return values, code, detail
    if values.get("COMPOSE_PROJECT_NAME") != layout.project:
        set_env(layout.env_path, {"COMPOSE_PROJECT_NAME": layout.project})
    return own_config(layout, sha)


def want_picker(values: dict | None, created_now: bool) -> bool:
    """CHAT_ENTITY_PICKER=true only for a configuration made now, or an interrupted one that never had the key and
    has no fixed school. Anything the operator already decided stays as it is."""
    if values is None or created_now:
        return True
    return PICKER_VAR not in values and is_placeholder(values.get(ENTITY_VAR))


def chat_state(values: dict) -> str:
    picker = values.get(PICKER_VAR, "").strip().lower() in ("true", "1", "yes", "on")
    if is_placeholder(values.get(ENTITY_VAR)):
        return "CHAT_AWAITS_SCHOOL_SELECTION" if picker else "CHAT_NOT_CONFIGURED"
    return "CHAT_READY"


def _check_parent_environment(host: Host, st: State, layout: L.Layout) -> None:
    """Reuse first-install environment findings without weakening its resource refusals."""
    class ParentEnvironment(HP.Host):
        def environ(self):
            return dict(host.environ)
    proof = HP.Preflight(layout, ParentEnvironment())
    proof.step_docker_target()
    proof.step_inherited_env()
    for item in proof.report.items:
        if item.level in (HP.REFUSED, HP.UNVERIFIED):
            (st.refuse if item.level == HP.REFUSED else st.unverified)(item.code, item.detail)


def _local_docker_target(host: Host, st: State) -> bool:
    result = host.run(["docker", "context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}"], timeout=60)
    if result.rc == 127:
        st.refuse("DOCKER_MISSING", "run scripts/prepare_ubuntu.sh --apply once, then log in again")
    elif result.rc != 0:
        st.unverified("DOCKER_TARGET_UNVERIFIED", "the default context endpoint could not be read")
    elif result.out.strip() != HP.DEFAULT_SOCKET:
        st.refuse("DOCKER_TARGET_NOT_LOCAL", "the default context does not name the local Docker socket")
    return st.exit_code() == 0


_RESUME_FORMAT = ('{"id":{{json .Id}},"name":{{json .Name}},"labels":{{json .Config.Labels}},'
                  '"mounts":{{json .Mounts}},"networks":{{json .NetworkSettings.Networks}}}')


def _resume_mounts(spec: dict, actual: list, model: dict, layout: L.Layout, volume_labels: dict, read_injected=None) -> bool:
    """Compare mounted targets/sources and access modes to the pinned Compose model.

    An inline-content config is delivered one of two ways, depending on the Compose version: a read-only bind of a file
    Compose materialised (earlier versions), or a copy into the container's own filesystem with no mount at all (Compose
    5.6.0, seen on the lab VM). The first stays an exact mount check. The second is accepted only when its exact content
    is read back from the container through `read_injected(target)`; an unreadable or different file is a mismatch.
    """
    expected = {}
    injected = {}
    for mount in spec.get("volumes") or []:
        target, kind, source = mount["target"], mount["type"], mount.get("source")
        if kind == "bind":
            source = os.path.realpath(source)
        elif kind == "volume":
            definition = model.get("volumes", {}).get(source) or {}
            if not source or definition.get("external"):
                return False
            source = definition.get("name") or f"{layout.project}_{source}"
        else:
            return False
        expected[target] = (kind, source, bool(mount.get("read_only")), None)
    for config in spec.get("configs") or []:
        definition = model.get("configs", {}).get(config["source"]) or {}
        if definition.get("external"):
            return False
        content = definition.get("content")
        source = os.path.realpath(definition["file"]) if definition.get("file") else None
        if source is None and not isinstance(content, str):
            return False
        if source is None:
            injected[config["target"]] = content
        else:
            expected[config["target"]] = ("bind", source, True, content)
    if not isinstance(actual, list):
        return False
    mounted = {m.get("Destination") for m in actual}
    for target, content in injected.items():
        if target in mounted:
            expected[target] = ("bind", None, True, content)
        elif read_injected is None or read_injected(target) != content.encode("utf-8"):
            return False
    if mounted != set(expected):
        return False
    for mount in actual:
        kind, source, readonly, content = expected[mount["Destination"]]
        if mount.get("Type") != kind or mount.get("RW") is not (not readonly):
            return False
        if kind == "volume":
            if mount.get("Name") != source or volume_labels.get(source, {}).get(HP.PROJECT_LABEL) != layout.project:
                return False
        elif source is not None:
            if os.path.realpath(mount.get("Source", "")) != source:
                return False
        else:
            # Compose materialises inline configs outside the checkout. Their
            # destination/read-only mode and exact content must still match.
            body = HP.Host().read_bytes(mount.get("Source", ""), HP.MAX_ENV)
            if body != content.encode("utf-8"):
                return False
    return True


def _postgres_major_change(host: Host, layout: L.Layout, model: dict) -> tuple[str, str, str] | None:
    """(volume, data major, image major) when the postgres data volume holds another major version.

    Checked before the mount comparison, which a new major version's Compose model may also fail, so the
    operator gets the reason that matters. Anything unreadable is no finding here; the later checks stand.
    """
    want = L.postgres_major(layout.source_root)
    mounts = (model.get("services", {}).get("postgres") or {}).get("volumes") or []
    source = next((m.get("source") for m in mounts if m.get("target") == "/var/lib/postgresql/data"), None)
    if not want or not source:
        return None
    volume = (model.get("volumes", {}).get(source) or {}).get("name") or f"{layout.project}_{source}"
    probe = host.run(["docker", "run", "--rm", "--network", "none", "-v", f"{volume}:/data:ro", "alpine:3",
                      "sh", "-c", "cat /data/PG_VERSION 2>/dev/null || true"], timeout=120)
    have = probe.out.strip() if probe.rc == 0 else ""
    return (volume, have, want) if have and have != want else None


def _prove_resume(host: Host, st: State, layout: L.Layout, ids: list[str]) -> bool:
    """A project label alone never authorises restarting another checkout's containers."""
    if not ids or any(not re.fullmatch(r"[0-9a-f]{12,64}", cid) for cid in ids):
        st.unverified("RESUME_CONTAINERS_UNVERIFIED", "container IDs could not be read")
        return False
    render = host.run([*layout.compose_prefix(), "-f", str(layout.source_root / "docker-compose.yml"),
                       "config", "--format", "json"], timeout=120)
    scan = host.run(["docker", "inspect", "--format", _RESUME_FORMAT, *ids], timeout=60)
    if render.rc != 0 or scan.rc != 0:
        st.unverified("RESUME_INSPECTION_FAILED", "the model or container metadata could not be read")
        return False
    try:
        model = json.loads(render.out)
        HP.parse_model(model, layout.project)
        changed = _postgres_major_change(host, layout, model)
        if changed:
            volume, have, want = changed
            st.refuse("POSTGRES_MAJOR_CHANGED", f"{volume} holds PostgreSQL {have} data, this checkout builds PostgreSQL {want}; "
                                                "nothing is changed: follow docs/OPERATIONS_LINUX.md, 'A new PostgreSQL major version'")
            return False
        containers = [json.loads(line) for line in scan.out.splitlines() if line.strip()]
        if len(containers) != len(ids) or {c["id"][:12] for c in containers} != {cid[:12] for cid in ids}:
            raise ValueError("container set")
        volume_labels = {}
        for name in sorted({m.get("Name") for c in containers for m in c["mounts"] if m.get("Type") == "volume"}):
            labels = host.run(["docker", "volume", "inspect", name, "--format", "{{json .Labels}}"], timeout=60)
            if labels.rc != 0:
                raise ValueError("volume labels")
            volume_labels[name] = json.loads(labels.out)
        for container in containers:
            labels = container["labels"]
            service = labels.get("com.docker.compose.service")
            spec = model["services"].get(service)
            if (labels.get(HP.PROJECT_LABEL) != layout.project
                    or os.path.realpath(labels.get("com.docker.compose.project.working_dir", "")) != os.path.realpath(layout.source_root)
                    or not isinstance(spec, dict)
                    or container["name"].lstrip("/") != spec.get("container_name", f"{layout.project}_{service}_1")
                    or not _resume_mounts(spec, container["mounts"], model, layout, volume_labels,
                                          lambda target, cid=container["id"]: host.container_file(cid, target))):
                st.refuse("RESUME_STACK_OTHER_LAYOUT", "existing containers or mounts do not match this checkout/config/state/project")
                return False
            network_keys = spec.get("networks") or ["default"]
            networks = {((model.get("networks", {}).get(key) or {}).get("name") or f"{layout.project}_{key}")
                        for key in network_keys}
            if set(container["networks"]) != networks:
                st.refuse("RESUME_NETWORKS_MISMATCH", "existing containers use different networks than the pinned model")
                return False
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        st.unverified("RESUME_INSPECTION_UNVERIFIED", "container/model metadata or inline config could not be verified")
        return False
    return True


def inspect(host: Host, root, config: str, state_dir: str, project: str, *, offline_check: bool = True) -> State:
    st = State()
    try:
        st.layout = layout = make_layout(config, state_dir, project, root)
    except L.LayoutError as exc:
        st.refuse("LAYOUT_" + exc.code, exc.field)
        return st
    if not host.platform().startswith("linux"):
        st.refuse("UNSUPPORTED_PLATFORM", "the installation runs on Linux only")
    head = host.git(root, "rev-parse", "HEAD")
    sha = head.out.strip()
    if head.rc != 0 or not install.SHA_RE.fullmatch(sha):
        st.refuse("SOURCE_NOT_A_GIT_CHECKOUT", "needs a Git checkout of an exact commit, not an unpacked archive")
        return st
    st.sha = sha
    dirty = host.git(root, "status", "--porcelain")
    if dirty.rc != 0:
        st.unverified("SOURCE_STATUS_FAILED", "git status failed")
    elif dirty.out.strip():
        st.refuse("SOURCE_TREE_NOT_CLEAN", "commit or remove local changes; the installation is of the commit, not of the working tree")
    if not layout.external_config or not layout.external_state:
        st.refuse("LAYOUT_NOT_EXTERNAL", "config and state must be outside the checkout")
    elif _nested(layout.config_dir, layout.state_dir):
        st.refuse("CONFIG_AND_STATE_NESTED", "config and state are separate directories")
    else:
        for f in L.validate(layout, links="allow-missing"):
            if f.code == "CONFIG_DIR_MISSING":
                continue                                           # absent is expected before init-config
            (st.refuse if f.level == L.REFUSE else st.unverified)(f.code, f.detail)

    _check_parent_environment(host, st, layout)
    if st.exit_code() != 0 or not _local_docker_target(host, st):
        return st

    # docker: a missing or unreachable engine is the one-time host preparation, not something to work around here
    info = host.run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=60)
    if info.rc == 127:
        st.refuse("DOCKER_MISSING", "run scripts/prepare_ubuntu.sh --apply once, then log in again")
    elif info.rc != 0 and "permission denied" in (info.err + info.out).lower():
        st.refuse("DOCKER_PERMISSION", "the user is not in the docker group in this login: log out and in again over SSH after "
                                       "prepare_ubuntu.sh --apply; do not use sudo docker or the socket")
    elif info.rc != 0:
        st.unverified("DOCKER_UNAVAILABLE", "docker info failed; the engine is not running or not reachable")

    stack = None
    if info.rc == 0:
        ps = host.run(["docker", "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={project}"], timeout=60)
        stack = (bool(ps.out.strip()) if ps.rc == 0 else None)
        if ps.rc != 0:
            st.unverified("CONTAINERS_UNVERIFIED", "existing project containers could not be read")

    config_state = _classify_config(st, layout, sha)
    st.config = config_state
    if config_state == "own":
        links = layout.links()
        if stack and all(link.state == "OK" for link in links) and _prove_resume(host, st, layout, ps.out.split()):
            st.resume_mode = "bootstrap-only"
            st.notes.append("RESUME: local engine, container checkout, mounts, volumes and networks match; ownership is checked again before bootstrap")
        elif stack:
            if not st.findings:
                st.refuse("STACK_WITHOUT_LINKS", f"containers of project {project} exist but the checkout links are incomplete; "
                                                "inspect with docker compose ps and scripts/host_preflight.py check, nothing is changed")
        else:
            st.notes.append("RESUME: configuration of this installation found (same key, project and Compose model); steps already done are idempotent")
    elif config_state == "absent":
        if stack:
            st.refuse("STACK_WITHOUT_OWN_CONFIG", f"containers of project {project} exist but no configuration of this tool does; nothing is touched")
        for path in {layout.config_dir.parent, layout.state_dir.parent}:
            if path.is_dir():
                continue
            if path.exists() or not path.parent.is_dir():
                st.refuse("PARENT_NOT_USABLE", f"{path}: create it yourself (install -d -m 0755), this tool makes only one missing level")
            else:
                st.new_parents.append(str(path))
        if offline_check and not st.new_parents:
            _offline_preflight(host, st, root, sha, str(layout.config_dir))
    return st


def _record(layout: L.Layout) -> dict:
    try:
        data = json.loads(install._read_regular(os.path.join(str(layout.config_dir), RECORD_NAME), 1 << 16))
    except (OSError, ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _changed_sources(layout: L.Layout, record: dict) -> list[str]:
    """Install sources whose bytes differ from what the record says init-config used (the tree is clean, so it is HEAD)."""
    recorded = record.get("sources") if isinstance(record.get("sources"), dict) else {}
    changed = []
    for rel in (install.COMPOSE_PATH, install.TEMPLATE_PATH):
        try:
            now = install.sha256_hex((layout.source_root / rel).read_bytes())
        except OSError:
            now = ""
        if recorded.get(rel) != now:
            changed.append(rel)
    return changed


def _template_entries(layout: L.Layout) -> dict | None:
    """Key -> the value text exactly as the template line has it (quotes kept), like install.render_env copies the line."""
    try:
        text = (layout.source_root / install.TEMPLATE_PATH).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    raw = {}
    for line in text.split("\n"):
        m = install._ASSIGN.match(line)
        if m and not line.lstrip().startswith("#"):
            raw[m.group(1)] = m.group(2).strip()
    return raw


def add_template_keys(layout: L.Layout, keys: list[str]) -> list[str]:
    """Append keys a later .env.example added, as init-config would have written them: a generated secret for the
    keys it generates, else the template value. Only absent keys; no value is printed. Returns the generated names."""
    entries = _template_entries(layout) or {}
    updates, generated = {}, []
    for key in keys:
        if key in install.GENERATORS:
            updates[key] = install.GENERATORS[key]()
            generated.append(key)
        else:
            updates[key] = entries.get(key, "")
    set_env(layout.env_path, updates, origin="scripts/setup.py")
    return generated


def _classify_config(st: State, layout: L.Layout, sha: str) -> str:
    if not os.path.lexists(layout.config_dir):
        return "absent"
    values, code, detail = own_config(layout, sha)
    if values is None:
        st.refuse(code, detail + ("; to look: ls -la, scripts/host_preflight.py check; never overwritten or removed here" if detail else ""))
        return "refused"
    entries = _template_entries(layout)
    if entries is None:
        st.refuse("CONFIG_TEMPLATE_UNREADABLE", f"{install.TEMPLATE_PATH} of this checkout cannot be read")
        return "refused"
    st.missing_keys = sorted(k for k in entries if k not in values)
    dollar = [k for k in st.missing_keys if k not in install.GENERATORS and "$" in entries[k]]
    if dollar:                                          # init-config refuses the same (TEMPLATE_VALUE_HAS_DOLLAR)
        st.refuse("TEMPLATE_VALUE_HAS_DOLLAR", ", ".join(dollar) + ": Compose would read part of the value as a variable")
        return "refused"
    if st.missing_keys:
        made = [k for k in st.missing_keys if k in install.GENERATORS]
        st.notes.append("WILL_ADD_KEYS: " + ", ".join(st.missing_keys) + " (absent from the .env, present in this commit's template"
                        + (f"; generated: {', '.join(made)}" if made else "") + ")")
    st.env_values = values
    record = _record(layout)
    recorded = record.get("source_sha")
    if isinstance(recorded, str) and install.SHA_RE.fullmatch(recorded) and recorded != sha:
        st.notes.append(f"CONFIG_RECORDED_COMMIT: {recorded} (this checkout is {sha}); the .env keeps its values")
    changed = _changed_sources(layout, record)
    if changed:
        st.notes.append("CONFIG_SOURCES_CHANGED: " + ", ".join(changed) + " differ from the files the configuration was created from; "
                        "existing values and data are reused as they are")
    return "own"


def _offline_preflight(host: Host, st: State, root, sha: str, config: str) -> None:
    proc = host.run([sys.executable, SCRIPTS / "install.py", "preflight", "--source-sha", sha, "--target-dir", config, "--repo", root],
                    timeout=120)
    for line in proc.out.splitlines():
        m = _OFFLINE.match(line.strip())
        if m:
            (st.refuse if m.group(1) == REFUSED else st.unverified)(m.group(2), m.group(3) or "")
    if proc.rc != 0 and not any(f[0] in (REFUSED, UNVERIFIED) for f in st.findings):
        st.unverified("OFFLINE_PREFLIGHT_FAILED", f"install.py preflight exit {proc.rc}")


def show_plan(host: Host, st: State, project: str, *, writing: bool) -> None:
    say = host.say
    say("EDU-AI: clean installation " + ("(PLAN, nothing is written or started)" if not writing else "plan"))
    if st.sha:
        say(f"SOURCE_SHA: {st.sha}")
    if st.layout is not None:
        say(f"SOURCE_ROOT: {st.layout.source_root}")
        say(f"CONFIG_DIR: {st.layout.config_dir} ({'existing, this installation' if st.config == 'own' else 'new' if st.config == 'absent' else 'REFUSED'})")
        say(f"STATE_DIR: {st.layout.state_dir}")
        say(f"PROJECT: {project}")
    for parent in st.new_parents:
        say(f"WILL_CREATE_PARENT: {parent} (0755)")
    if st.config == "absent":
        say("WILL: create config (init-config), pin .env keys, create links, bootstrap with smoke test, check database roles")
        say(f"WILL_SET: {PICKER_VAR}=true in the NEW .env (the school is chosen in the browser; {ENTITY_VAR} stays unset)")
    elif st.config == "own":
        picker = want_picker(st.env_values, False)
        say("WILL: continue the same installation without regenerating any key; only absent keys are added")
        say(f"WILL_SET: {PICKER_VAR}=true" if picker else f"KEEP: {PICKER_VAR} and {ENTITY_VAR} stay as they are")
    for note in st.notes:
        say(note)
    for level, code, detail in st.findings:
        say(f"{level} {code}" + (f": {detail}" if detail else ""))
    say("NOT_TESTED: network isolation, firewall and public ingress; this wizard does not enable Cloudflare profiles")


# ── the steps ────────────────────────────────────────────────────────────────
def _items(text: str) -> list[tuple[str, str, str]]:
    """Read blocking findings, excluding the preflight's aggregate RESULT row."""
    out = []
    for line in text.splitlines():
        m = _ITEM.match(line.strip())
        if m and m.group(1) != "RESULT":
            out.append((m.group(1), m.group(2), m.group(3) or ""))
    return out


def only_pools(proc: Proc) -> bool:
    """Exit 3 whose single finding is NETWORK_POOLS: the known unmeasured-address-pool note, nothing else."""
    items = _items(proc.out)
    return proc.rc == 3 and bool(items) and all(code == "NETWORK_POOLS" and level == UNVERIFIED for code, level, _ in items)


class Wizard:
    def __init__(self, host: Host, args, root=ROOT) -> None:
        self.host, self.args, self.root = host, args, pathlib.Path(root)
        self.say = host.say

    def child(self, script: str, *args, capture: bool = True, timeout: int = 900) -> Proc:
        return self.host.run([sys.executable, SCRIPTS / script, *args], capture=capture, timeout=timeout)

    def echo(self, proc: Proc) -> None:
        for line in proc.out.splitlines():
            self.say("  " + line)

    def stop(self, step: str, rc: int, how: str) -> int:
        self.say(f"STEP_FAILED: {step} (exit {rc})")
        self.say(f"STATE_KEPT: nothing was deleted or rolled back. {how}")
        self.say("BACKEND_READY: no")
        return rc

    # questions ---------------------------------------------------------------
    def confirm_plan(self, st: State, config: str, state_dir: str, project: str) -> tuple[State, str, str, str] | None:
        for _ in range(3):
            show_plan(self.host, st, project, writing=True)
            if st.exit_code() != 0:
                return st, config, state_dir, project
            if self.args.no_input:
                return st, config, state_dir, project
            if not self.host.interactive():
                self.say("REFUSED NOT_INTERACTIVE: run in a terminal, or pass --no-input to run without questions")
                st.refuse("NOT_INTERACTIVE")
                return st, config, state_dir, project
            answer = self.host.ask("Use this plan and continue? [Y/n] ").strip().lower()
            if answer in ("", "y", "yes"):
                return st, config, state_dir, project
            config = self.host.ask(f"Configuration directory [{config}]: ").strip() or config
            state_dir = self.host.ask(f"Data directory [{state_dir}]: ").strip() or state_dir
            project = self.host.ask(f"Compose project name [{project}]: ").strip() or project
            st = inspect(self.host, self.root, config, state_dir, project)
        self.say("REFUSED PLAN_NOT_CONFIRMED: nothing was created")
        st.refuse("PLAN_NOT_CONFIRMED")
        return st, config, state_dir, project

    def accept_pools(self, detail: str) -> bool:
        if self.args.accept_unmeasured_network_pools:
            return True
        if self.args.no_input or not self.host.interactive():
            return False
        self.say("NETWORK_POOLS: the addresses of the automatically created Docker networks are not proven free of your LAN or VPN ranges.")
        self.say(f"  {detail}")
        self.say("  Accept only on an isolated VM. Network isolation and firewall are not tested by this tool.")
        return self.host.ask("Do you accept this limitation? Type 'yes': ").strip().lower() in _YES

    # the run -----------------------------------------------------------------
    def install(self) -> int:
        a = self.args
        config, state_dir = default_paths(self.host)
        config, state_dir, project = a.config_dir or config, a.state_dir or state_dir, a.project or DEFAULT_PROJECT
        st = inspect(self.host, self.root, config, state_dir, project)
        confirmed = self.confirm_plan(st, config, state_dir, project)
        st, config, state_dir, project = confirmed
        if st.exit_code() != 0:
            self.say("BACKEND_READY: no (nothing was started)")
            return st.exit_code()
        layout, sha = st.layout, st.sha
        created_now = st.config == "absent"
        for parent in st.new_parents:
            os.mkdir(parent, 0o755)
            os.chmod(parent, 0o755)                                # a restrictive umask must not narrow it
            self.say(f"CREATED_PARENT: {parent}")

        if created_now:
            # The child shows its one-time secret overview only on an inherited terminal: never under --no-input.
            quiet = self.args.no_input or not self.host.interactive()
            made = self.child("install.py", "init-config", "--source-sha", sha, "--target-dir", layout.config_dir,
                              "--repo", self.root, capture=quiet)
            if quiet:
                self.echo(made)
            rc = made.rc
            if rc != 0:
                return self.stop("init-config", rc, "Fix the reported cause and repeat the same command.")
            values, code, detail = bind_new_project(layout, sha)
            if values is None:
                return self.stop("init-config-verify", 3, f"The new set could not be confirmed ({code}); inspect {layout.config_dir}.")
            st.env_values = values
        if st.missing_keys:
            try:
                made = add_template_keys(layout, st.missing_keys)
            except OSError as exc:
                return self.stop("add-keys", 3, f"The .env could not be updated ({type(exc).__name__}); it was left unchanged.")
            self.say("CONFIG_KEYS_ADDED: " + ", ".join(st.missing_keys) + (f" (generated: {', '.join(made)})" if made else ""))
            values, code, detail = own_config(layout, sha)
            if values is None:
                return self.stop("add-keys-verify", 3, f"The configuration could not be confirmed after adding keys ({code}).")
            st.env_values = values
        pin = ["pin-env", *layout.argv(), "--allocate-tunnel-subnet", "--set", LANGFLOW_URL_PIN]
        if want_picker(st.env_values, created_now):
            pin += ["--set", f"{PICKER_VAR}=true"]
        proc = self.child("host_preflight.py", *pin)
        self.echo(proc)
        if proc.rc != 0:
            return self.stop("pin-env", proc.rc, "The .env was not changed by a failing step; repeat the same command.")

        if st.resume_mode != "bootstrap-only":
            rc = self.handoffs(layout)
            if rc != 0:
                return rc
        else:
            proof = State(layout=layout, sha=sha)
            if not _local_docker_target(self.host, proof):
                return self.stop("resume-proof", proof.exit_code(), "The local engine could not be confirmed; no bootstrap was started.")
            ids = self.host.run(["docker", "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={project}"], timeout=60)
            if ids.rc != 0 or not _prove_resume(self.host, proof, layout, ids.out.split()):
                for level, code, detail in proof.findings:
                    self.say(f"{level} {code}: {detail}")
                return self.stop("resume-proof", proof.exit_code() or 3, "Ownership changed or could not be confirmed; no bootstrap was started.")
        head_check = self.host.git(self.root, "rev-parse", "HEAD")
        status_check = self.host.git(self.root, "status", "--porcelain")
        if head_check.rc != 0 or status_check.rc != 0:
            self.say("UNVERIFIED SOURCE_STATUS_FAILED: the final HEAD/working tree could not be read; bootstrap was not started")
            return self.stop("source-check", 3, "Restore readable Git metadata and repeat.")
        again, dirty = head_check.out.strip(), status_check.out.strip()
        if again != sha or dirty:
            self.say("REFUSED SOURCE_CHANGED_DURING_INSTALL: HEAD or the working tree changed; bootstrap was not started")
            return self.stop("source-check", 2, "Restore the checkout to the recorded commit and repeat.")
        self.say(f"BOOTSTRAP_LAUNCHED: commit {sha}; its own progress and exit status follow")
        rc = self.child("bootstrap.py", "--no-input", *layout.argv(), capture=False, timeout=7200).rc
        if rc != 0:
            self.say("BOOTSTRAP_EXIT: " + str(rc))
            return self.stop("bootstrap", rc, "Repeat the same command: bootstrap steps are idempotent. A non-zero result is never "
                                              "relabelled as success here.")
        return self.finish(layout, sha)

    def handoffs(self, layout: L.Layout) -> int:
        flag = ["--accept-unmeasured-network-pools"] if self.args.accept_unmeasured_network_pools else []
        env_link_missing = any(link.name == ENV_NAME and link.state == "MISSING" for link in layout.links())
        if env_link_missing:
            p = self.child("host_preflight.py", "handoff", *layout.argv(), "--env-link-only", *flag)
            self.echo(p)
            if p.rc != 0 and not only_pools(p):
                return self.stop("handoff-env-link", p.rc, "A finding above other than NETWORK_POOLS stops the run; nothing was removed.")
            if any(link.name == ENV_NAME and link.state != "OK" for link in layout.links()):
                return self.stop("handoff-env-link", 3, "The .env link was not created; inspect the checkout.")
        p = self.child("host_preflight.py", "handoff", *layout.argv(), *flag)
        if p.rc == 0:
            self.echo(p)
            return 0
        if flag or not only_pools(p):
            self.echo(p)
            return self.stop("handoff", p.rc, "No bootstrap was started; fix the finding and repeat the same command.")
        detail = next((d for c, _, d in _items(p.out) if c == "NETWORK_POOLS"), "")
        if not self.accept_pools(detail):
            self.echo(p)
            self.say("NETWORK_POOLS_NOT_ACCEPTED: with --no-input only an explicitly passed --accept-unmeasured-network-pools counts")
            return self.stop("handoff", 3, "No bootstrap was started; repeat and accept, or fix the network first.")
        p = self.child("host_preflight.py", "handoff", *layout.argv(), "--accept-unmeasured-network-pools")
        self.echo(p)
        if p.rc != 0:
            return self.stop("handoff", p.rc, "No bootstrap was started; fix the finding and repeat the same command.")
        self.say("NETWORK_POOLS_ACCEPTED: recorded by the operator for this run")
        return 0

    def finish(self, layout: L.Layout, sha: str) -> int:
        roles = self.child("db_roles.py", "verify", "--container", POSTGRES_CONTAINER)
        self.echo(roles)
        if roles.rc != 0 or "DB_CONTRACT: MATCH" not in roles.out:
            return self.stop("db-roles", roles.rc or 1, "The database role contract is not MATCH; inspect the DEVIATION lines above.")
        head = self.host.git(self.root, "rev-parse", "HEAD")
        status = self.host.git(self.root, "status", "--porcelain")
        if head.rc != 0 or status.rc != 0:
            self.say("UNVERIFIED SOURCE_STATUS_FAILED: the final checkout could not be read after bootstrap")
            return self.stop("final-source-check", 3, "Services may be running; their state is kept, but readiness is not confirmed.")
        if head.out.strip() != sha or status.out.strip():
            self.say("REFUSED SOURCE_CHANGED_DURING_INSTALL: the checkout changed during bootstrap")
            return self.stop("final-source-check", 2, "Services may be running; restore the recorded checkout and inspect before repeating.")
        values, _, _ = install.parse_assignments(install._read_regular(str(layout.env_path), install.MAX_BLOB).decode("utf-8"))
        chat = chat_state(values)
        port = self.nginx_port()
        suffix = "" if port == 80 else f":{port}"
        self.say("")
        self.say(f"SOURCE_SHA: {sha}")
        self.say("BACKEND_READY: yes (bootstrap exit 0 with its smoke test, database roles MATCH)")
        self.say(f"{chat}: derived from the configuration, not from a chat query")
        self.say(f"CHAT_URL_LOCAL: http://localhost{suffix}/chat/")
        ip = self.host.lan_ip()
        if ip:
            self.say(f"CHAT_URL_LAN: http://{ip}{suffix}/chat/ (only where your network allows it)")
        if chat == "CHAT_AWAITS_SCHOOL_SELECTION":
            self.say("Open the chat and choose a school. Sending stays off until then; the choice belongs to this browser.")
            self.say("The school choice is the scope of a question, not a permission; permissions and the perimeter are the backend's.")
        elif chat == "CHAT_READY":
            self.say("The chat uses the fixed school from the configuration; this installation did not change it.")
        else:
            self.say("The chat has no school: turn the choice on or set a fixed school; see docs/CLEAN_INSTALL.md.")
        self.say("PROVIDER: the LLM provider is a separate input (Bifrost UI on 127.0.0.1:8099 over an SSH tunnel, "
                 "login BIFROST_ADMIN_USERNAME / BIFROST_ADMIN_PASSWORD from the config .env); "
                 "this run made no paid call and does not claim answers work")
        self.say("NOT_TESTED: network isolation and firewall (published ports 80, 8443, 8444); explicit existing Cloudflare profiles require separate verification")
        return 0

    def nginx_port(self) -> int:
        proc = self.host.run(["docker", "port", NGINX_CONTAINER, "80/tcp"], timeout=60)
        m = re.search(r":(\d{1,5})\s*$", proc.out.splitlines()[0].strip()) if proc.rc == 0 and proc.out.strip() else None
        return int(m.group(1)) if m else 80

    def plan(self) -> int:
        a = self.args
        config, state_dir = default_paths(self.host)
        project = a.project or DEFAULT_PROJECT
        st = inspect(self.host, self.root, a.config_dir or config, a.state_dir or state_dir, project)
        show_plan(self.host, st, project, writing=False)
        self.say("READ_ONLY: no configuration, link, directory, container or package was created")
        return st.exit_code()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, text in (("plan", "read-only overview; creates nothing"), ("install", "install, asking a few questions")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--config-dir", default=None, metavar="ABS_DIR", help=f"default ~/{DEFAULT_BASE}/config")
        p.add_argument("--state-dir", default=None, metavar="ABS_DIR", help=f"default ~/{DEFAULT_BASE}/state")
        p.add_argument("--project", default=None, metavar="NAME", help=f"Compose project (default {DEFAULT_PROJECT})")
        if name == "install":
            p.add_argument("--no-input", action="store_true", help="ask nothing; the pool finding needs --accept-unmeasured-network-pools")
            p.add_argument("--accept-unmeasured-network-pools", action="store_true",
                           help="record that you accept the unmeasured Docker address pools (isolated VM only)")
    return parser


def main(argv=None, *, host: Host | None = None, root=ROOT) -> int:
    args = build_parser().parse_args(argv)
    host = host or Host()
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError, OSError):
        pass
    wizard = Wizard(host, args, root)
    try:
        return wizard.plan() if args.cmd == "plan" else wizard.install()
    except KeyboardInterrupt:
        host.say("INTERRUPTED: nothing was deleted; the state on disk decides; repeat the same command")
        return 3
    except EOFError:
        host.say("INTERRUPTED: input closed; nothing was deleted; repeat the same command")
        return 3
    except Exception as exc:  # noqa: BLE001 - a bug must not print a traceback that could hold values
        host.say(f"INTERNAL_ERROR: {type(exc).__name__}; the state on disk decides; nothing was deleted")
        return 3


if __name__ == "__main__":
    sys.exit(main())
