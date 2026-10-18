"""Where an EDU-AI installation lives: checkout, external config, state data, Compose project.

One resolution for install, bootstrap, smoke, the host preflight and a future updater.
Precedence: command-line flag > EDU_AI_* variable > default (the legacy in-checkout layout).
Standard library only; it reads the file system with lstat/readlink and never writes, runs
a process or follows a link it was not asked about.

An unreadable or odd state is never "free" or "ready": it is a finding with level UNVERIFIED.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import stat
from collections.abc import Mapping

FLAGS = {
    "source_root": ("--source-root", "--repo"),
    "config_dir": ("--config-dir", "--target-dir"),
    "state_dir": ("--state-dir",),
    "project": ("--project",),
}
VARIABLES = {
    "source_root": "EDU_AI_SOURCE_ROOT",
    "config_dir": "EDU_AI_CONFIG_DIR",
    "state_dir": "EDU_AI_STATE_DIR",
    "project": "EDU_AI_COMPOSE_PROJECT",
}
# Bind-mount sources that move with state_dir. pgadmin_exports stays in the checkout:
# it holds a tracked .gitkeep and only exports.
STATE_DIRS = ("uploads", "langflow_data", "sqllite_export", "bifrost_data")
CHECKOUT_DIRS = ("pgadmin_exports",)
ENV_NAME = ".env"
RECORD_NAME = "install-record.json"
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")      # what Compose accepts as a project name

OK, REFUSE, UNVERIFIED = "OK", "REFUSE", "UNVERIFIED"


class LayoutError(ValueError):
    """An input that cannot be a layout; .code is a stable name, never a path or value."""

    def __init__(self, code: str, field: str):
        super().__init__(f"{code}: {field}")
        self.code, self.field = code, field


@dataclasses.dataclass(frozen=True)
class Finding:
    level: str          # REFUSE | UNVERIFIED
    code: str
    detail: str         # a path or a fixed phrase; never a secret


@dataclasses.dataclass(frozen=True)
class Link:
    name: str
    path: pathlib.Path      # the name inside the checkout
    target: pathlib.Path    # where it must point
    state: str              # OK MISSING CONFLICT WRONG_TARGET TARGET_MISSING UNVERIFIABLE


@dataclasses.dataclass(frozen=True)
class Layout:
    source_root: pathlib.Path
    config_dir: pathlib.Path
    state_dir: pathlib.Path
    project: str | None = None
    origin: Mapping[str, str] = dataclasses.field(default_factory=dict)   # field -> flag | env | default

    @property
    def env_path(self) -> pathlib.Path:
        return self.config_dir / ENV_NAME

    @property
    def record_path(self) -> pathlib.Path:
        return self.config_dir / RECORD_NAME

    @property
    def external_config(self) -> bool:
        return not _same(self.config_dir, self.source_root)

    @property
    def external_state(self) -> bool:
        return not _same(self.state_dir, self.source_root)

    @property
    def pinned(self) -> bool:
        """True when Compose must be told everything instead of finding it from the cwd."""
        return self.project is not None or self.external_config

    def state_path(self, name: str) -> pathlib.Path:
        """Where a data directory really lives (the checkout name is a link when external)."""
        return (self.state_dir if name in STATE_DIRS else self.source_root) / name

    def compose_prefix(self) -> list[str]:
        """`docker compose` plus the pins; the legacy layout stays the bare command."""
        command = ["docker", "compose"]
        if self.project is not None:
            command += ["-p", self.project]
        if self.pinned:
            command += ["--project-directory", str(self.source_root), "--env-file", str(self.env_path)]
        return command

    def argv(self) -> list[str]:
        """Flags that make a child script resolve the same layout; only what was given."""
        out: list[str] = []
        for field, names in FLAGS.items():
            if self.origin.get(field, "default") != "default":
                out += [names[0], str(getattr(self, field))]
        return out

    def describe(self) -> list[str]:
        return [f"{field}={getattr(self, field)} ({self.origin.get(field, 'default')})"
                for field in FLAGS if getattr(self, field) is not None]

    def links(self) -> list[Link]:
        """The names the checkout must carry so Compose (env_file: .env, relative mounts) finds everything."""
        wanted: list[tuple[str, pathlib.Path]] = []
        if self.external_config:
            wanted.append((ENV_NAME, self.env_path))
        if self.external_state:
            wanted += [(name, self.state_dir / name) for name in STATE_DIRS]
        return [Link(name, self.source_root / name, target, _link_state(self.source_root / name, target))
                for name, target in wanted]


def _same(a: pathlib.Path, b: pathlib.Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _link_state(path: pathlib.Path, target: pathlib.Path) -> str:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "MISSING"
    except OSError:
        return "UNVERIFIABLE"
    if not stat.S_ISLNK(info.st_mode):
        return "CONFLICT"                       # a real file or directory is never replaced
    try:
        if os.path.realpath(path) != os.path.realpath(target):
            return "WRONG_TARGET"
        return "OK" if os.path.exists(target) else "TARGET_MISSING"
    except OSError:
        return "UNVERIFIABLE"


def _path(value: str, field: str) -> pathlib.Path:
    if not isinstance(value, str) or not value or "\0" in value or "\n" in value or "\r" in value:
        raise LayoutError("LAYOUT_PATH_UNSAFE", field)
    p = pathlib.PurePath(value)
    if not (os.path.isabs(value) and p.is_absolute()):
        raise LayoutError("LAYOUT_PATH_NOT_ABSOLUTE", field)
    if ".." in p.parts:
        raise LayoutError("LAYOUT_PATH_UNSAFE", field)
    return pathlib.Path(os.path.normpath(value))


def add_arguments(parser, legacy_names: bool = False) -> None:
    """The four layout options; legacy_names adds install.py's older --repo and --target-dir."""
    help_text = {"source_root": "checkout holding docker-compose.yml", "config_dir": "directory holding .env",
                 "state_dir": "directory holding uploads, langflow_data, ...",
                 "project": "Compose project name"}
    for field, names in FLAGS.items():
        parser.add_argument(*(names if legacy_names else names[:1]), dest=f"layout_{field}", default=None,
                            metavar="VALUE", help=help_text[field])


def resolve(args=None, environ: Mapping[str, str] | None = None,
            default_root: pathlib.Path | str | None = None) -> Layout:
    """Flag > variable > default. Defaults: the checkout of this script, config and state inside it."""
    environ = os.environ if environ is None else environ
    root = pathlib.Path(default_root) if default_root else pathlib.Path(__file__).resolve().parent.parent
    chosen: dict[str, tuple[str, str]] = {}
    for field, variable in VARIABLES.items():
        flag = getattr(args, f"layout_{field}", None) if args is not None else None
        if flag is not None:
            chosen[field] = (flag, "flag")
        elif environ.get(variable, "") != "":
            chosen[field] = (environ[variable], "env")
    origin = {f: chosen[f][1] if f in chosen else "default" for f in FLAGS}
    source = _path(chosen["source_root"][0], "source_root") if "source_root" in chosen else root
    config = _path(chosen["config_dir"][0], "config_dir") if "config_dir" in chosen else source
    state = _path(chosen["state_dir"][0], "state_dir") if "state_dir" in chosen else source
    project = None
    if "project" in chosen:
        project = chosen["project"][0]
        if not _PROJECT_RE.match(project):
            raise LayoutError("LAYOUT_PROJECT_INVALID", "project")
    return Layout(source, config, state, project, origin)


def validate(layout: Layout, *, links: str = "require") -> list[Finding]:
    """Findings only; an empty list is the whole good answer.

    links="require": a missing link is a refusal (bootstrap, verify). links="allow-missing": the
    caller is about to create it (apply). A conflict, a wrong target or an unreadable name is never allowed.
    """
    findings: list[Finding] = []

    def need_file(path: pathlib.Path, code: str) -> None:
        try:
            if not path.is_file():
                findings.append(Finding(REFUSE, code, str(path)))
        except OSError:
            findings.append(Finding(UNVERIFIED, code + "_UNREADABLE", str(path)))

    need_file(layout.source_root / "docker-compose.yml", "SOURCE_NOT_A_CHECKOUT")
    for field in ("config_dir", "state_dir"):
        base = getattr(layout, field)
        if base != layout.source_root and _inside(base, layout.source_root):
            findings.append(Finding(REFUSE, f"{field.upper()}_INSIDE_CHECKOUT", str(base)))
    if layout.external_config:
        try:
            if not layout.config_dir.is_dir():
                findings.append(Finding(REFUSE, "CONFIG_DIR_MISSING", str(layout.config_dir)))
        except OSError:
            findings.append(Finding(UNVERIFIED, "CONFIG_DIR_UNREADABLE", str(layout.config_dir)))
    for link in layout.links():
        if link.state == "OK":
            continue
        if link.state == "MISSING" and links == "allow-missing":
            continue
        level = UNVERIFIED if link.state == "UNVERIFIABLE" else REFUSE
        findings.append(Finding(level, f"LINK_{link.state}", str(link.path)))
    return findings


def verdict(findings: list[Finding]) -> str:
    """REFUSE beats UNVERIFIED beats OK; there is no 'free' for anything that could not be read."""
    if any(f.level == REFUSE for f in findings):
        return REFUSE
    return UNVERIFIED if findings else OK


def _inside(child: pathlib.Path, parent: pathlib.Path) -> bool:
    c, p = os.path.realpath(child), os.path.realpath(parent)
    try:
        return c != p and os.path.commonpath([c, p]) == p
    except ValueError:                      # other drive
        return False


_PG_BASE = re.compile(r"^FROM\s+\S+?:[^@\s]*-pg(\d+)(?:-[a-z]+)?(?:@|\s|$)", re.M)


def postgres_major(source_root) -> str | None:
    """The PostgreSQL major version this checkout's Dockerfile.postgres builds (base tag …-pgNN)."""
    try:
        text = (pathlib.Path(source_root) / "Dockerfile.postgres").read_text(encoding="utf-8")
    except OSError:
        return None
    m = _PG_BASE.search(text)
    return m.group(1) if m else None
