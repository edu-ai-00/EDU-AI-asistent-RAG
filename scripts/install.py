#!/usr/bin/env python3
"""B06-1: offline preparation of an external EDU-AI configuration set.

    install.py preflight   --source-sha SHA40 --target-dir ABS_DIR [--repo DIR]
    install.py plan        --source-sha SHA40 --target-dir ABS_DIR [--repo DIR]
    install.py init-config --source-sha SHA40 --target-dir ABS_DIR [--repo DIR]

Reads docker-compose.yml and .env.example from the given commit (local git objects only) and,
for init-config, publishes ONE new directory with .env and install-record.json. It starts,
builds and inspects nothing: every result says HOST_NOT_CHECKED and INSTALLATION_NOT_STARTED,
and a written set is CONFIG_PREPARED, not an installation.
Secrets: the protected .env stays the permanent source. Only the final overview of a successful init-config, and only in an
interactive terminal (stdout and stdin are ttys, no CI environment), shows the four values THIS run generated, once; anywhere else
the output carries the path alone. No other line, error or log ever holds a value.
Exit status: 0 ok | 2 refused | 3 could not verify or complete. Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import hashlib
import json
import os
import pathlib
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from envfile import is_placeholder  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHA_RE = re.compile(r"[0-9a-f]{40}")
COMPOSE_PATH, TEMPLATE_PATH = "docker-compose.yml", ".env.example"
ENV_NAME, RECORD_NAME = ".env", "install-record.json"
RECORD_SCHEMA = "edu-ai-config-record/1"
MAX_BLOB = 1 << 20
IS_POSIX = os.name == "posix"
CAN_PUBLISH = sys.platform.startswith("linux")   # init-config needs renameat2(RENAME_NOREPLACE)

# The secrets scripts/bootstrap.py already generates (GENERATED_SECRETS); nothing else is generated here.
GENERATED = ("POSTGRES_PASSWORD", "PGADMIN_DEFAULT_PASSWORD", "LANGFLOW_SUPERUSER_PASSWORD", "LANGFLOW_SECRET_KEY",
             "POSTGRES_ADMIN_PASSWORD", "POSTGRES_SEARCH_PASSWORD", "POSTGRES_LANGFLOW_PASSWORD", "POSTGRES_MIGRATOR_PASSWORD")
KEY_NAME = "LANGFLOW_SECRET_KEY"
SERVICE_ISSUED = ("LANGFLOW_API_KEY",)  # minted by Langflow in a later stage
MARKERS = ("HOST_NOT_CHECKED", "INSTALLATION_NOT_STARTED")

_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")  # same as envfile.py
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PASSWORD = re.compile(r"[A-Za-z0-9_-]{32}")
_FERNET = re.compile(r"[A-Za-z0-9_-]{43}=")
_HEX64 = re.compile(r"[0-9a-f]{64}")


def gen_password() -> str:
    return secrets.token_urlsafe(24)


def gen_fernet() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


GENERATORS = {"POSTGRES_PASSWORD": gen_password, "PGADMIN_DEFAULT_PASSWORD": gen_password,
              "LANGFLOW_SUPERUSER_PASSWORD": gen_password, "LANGFLOW_SECRET_KEY": gen_fernet,
              "POSTGRES_ADMIN_PASSWORD": gen_password, "POSTGRES_SEARCH_PASSWORD": gen_password,
              "POSTGRES_LANGFLOW_PASSWORD": gen_password, "POSTGRES_MIGRATOR_PASSWORD": gen_password}


def sha256_hex(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


# Environments that mean "not a person at a terminal": the one-time secret overview is never shown there.
_CI_FLAGS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "JENKINS_URL", "TF_BUILD", "TEAMCITY_VERSION", "CIRCLECI",
             "TRAVIS", "APPVEYOR", "DRONE", "CODEBUILD_BUILD_ID", "BITBUCKET_BUILD_NUMBER")


def interactive_terminal(stdout=None, stdin=None, env=None) -> bool:
    """True only for a person at a terminal: stdout and stdin are both ttys and no CI environment is set."""
    stdout = sys.stdout if stdout is None else stdout
    stdin = sys.stdin if stdin is None else stdin
    env = os.environ if env is None else env

    def tty(stream) -> bool:
        try:
            return bool(stream.isatty())
        except (AttributeError, ValueError, OSError):
            return False

    if not (tty(stdout) and tty(stdin)):
        return False
    return not any(env.get(name, "").strip().lower() not in ("", "0", "false", "no") for name in _CI_FLAGS)


class Report:
    """Collected output. Only names, paths and fixed codes go in `lines`: never a value.

    `reveal` is the one exception and it is kept apart: the values a successful init-config generated, printed once by emit()
    in the final overview of an interactive terminal. It is never part of `lines`, so no error or log line can carry it.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.refused: list[str] = []
        self.unverified: list[str] = []
        self.reveal: dict[str, str] | None = None

    def say(self, line: str) -> None:
        self.lines.append(line)

    def refuse(self, code: str, detail: str = "") -> None:
        self.refused.append(code)
        self.lines.append(f"REFUSED {code}" + (f": {detail}" if detail else ""))

    def unverifiable(self, code: str, detail: str = "") -> None:
        self.unverified.append(code)
        self.lines.append(f"UNVERIFIED {code}" + (f": {detail}" if detail else ""))

    @property
    def failed(self) -> bool:
        return bool(self.refused or self.unverified)

    def exit_code(self) -> int:
        return 3 if self.unverified else 2 if self.refused else 0

    def emit(self, out=None, *, interactive: bool | None = None) -> None:
        """Print the report. The result always ends with the MARKERS; a one-time secret overview, when due, sits just before them."""
        out = sys.stdout if out is None else out
        values, self.reveal = self.reveal, None              # one-time: after this call the report no longer holds them
        for line in self.lines:
            print(line, file=out)
        if values and not self.failed:
            if interactive is None:
                interactive = interactive_terminal(out)
            if interactive:
                print("GENERATED_SECRETS: shown once, only on this terminal. Store them in your vault now; they are not shown "
                      "again and the protected .env stays the permanent source", file=out)
                for name in GENERATED:
                    if name in values:
                        print(f"{name}={values[name]}", file=out)
            else:
                print("GENERATED_SECRETS_NOT_SHOWN: this output is not an interactive terminal (or CI is set); "
                      "they are only in the protected configuration file", file=out)
        for line in MARKERS:
            print(line, file=out)


# ── source: two files from a local git commit ────────────────────────────────
_GIT_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
             "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_ALLOW_PROTOCOL",   # GIT_ALLOW_PROTOCOL overrides every protocol.* setting
             "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT")
# GIT_CONFIG_KEY_<n>/GIT_CONFIG_VALUE_<n> are dropped too. GIT_CONFIG_GLOBAL/SYSTEM/NOSYSTEM stay: every git command here reads the same files.
_GIT_DROP_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
# A per-protocol setting (protocol.file.allow=always in any config scope) beats protocol.allow, so each built-in transport is named.
_NO_TRANSPORT = ("-c", "protocol.allow=never", *(a for p in ("file", "git", "ssh", "http", "https", "ftp", "ftps", "ext", "fd")
                                                  for a in ("-c", f"protocol.{p}.allow=never")))


def _git_env(base=None) -> dict:
    """The environment of every git child. GIT_CONFIG would make `git config` read one foreign file and miss the repository's own
    promisor settings, and injected GIT_CONFIG_* values would compete with the -c protocol guard, so none of them is inherited."""
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if k.upper() not in _GIT_DROP and not k.upper().startswith(_GIT_DROP_PREFIXES)}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    return env


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    """One of a few read-only local git forms; anything else is a programming error."""
    allowed = (
        (args[:3] == ("rev-parse", "--verify", "--quiet") and len(args) == 4)
        or args == ("rev-parse", "--show-toplevel")
        or (args[:2] in (("cat-file", "blob"), ("cat-file", "commit"), ("cat-file", "tree")) and len(args) == 3)
        or (args[:2] in (("config", "--get"), ("config", "--get-regexp")) and len(args) == 3)
    )
    if not allowed:
        raise ValueError("git form not allowed")
    # No transport is allowed: even a missed promisor remote cannot be contacted; no replacement ref may change what a SHA reads.
    return subprocess.run(["git", "-C", repo, *_NO_TRANSPORT, *args], capture_output=True,
                          stdin=subprocess.DEVNULL, env=_git_env(), timeout=30, check=False)


class _GitFailed(Exception):
    pass


def _partial_clone_configured(repo: str) -> bool:
    """True for a promisor/partial clone. Reads configuration only, never an object (an object lookup may fetch).

    Any present key counts, whatever its value: git also takes remote.X.partialclonefilter alone, or a whitespace-only
    extensions.partialclone name, as a promisor remote, and refusing a harmless false positive costs nothing.
    """
    for args in (("config", "--get", "extensions.partialclone"),
                 ("config", "--get-regexp", r"^remote\..*\.(promisor|partialclonefilter)$")):
        probe = _git(repo, *args)
        if probe.returncode == 0:
            return True
        if probe.returncode != 1:
            raise _GitFailed(f"config exit {probe.returncode}")
    return False


def _git_oid(kind: str, data: bytes) -> str:
    return hashlib.sha1(b"%s %d\0" % (kind.encode(), len(data)) + data).hexdigest()


def _object(repo: str, kind: str, oid: str) -> bytes | None:
    """The raw object, accepted only if it hashes to its own name: a normal git read does not re-hash (a forged loose object,
    a replaced one or one in an alternate would otherwise be read as the pinned commit's)."""
    done = _git(repo, "cat-file", kind, oid)
    if done.returncode != 0 or _git_oid(kind, done.stdout) != oid:
        return None
    return done.stdout


def _binding(repo: str, sha: str, blobs: dict) -> tuple[str, str, str] | None:
    """None when the blobs are, by their own hashes, the regular files of commit `sha`; else (kind, code, detail) of the refusal."""
    commit = _object(repo, "commit", sha)
    head = commit.split(b"\n", 1)[0] if commit else b""
    tree_oid = head[5:].decode("ascii", "replace") if head.startswith(b"tree ") else ""
    tree = _object(repo, "tree", tree_oid) if re.fullmatch(r"[0-9a-f]{40}", tree_oid) else None
    if tree is None:
        return "unverified", "SOURCE_OBJECTS_FAILED_VERIFICATION", "an object of the commit does not hash to its own name"
    entries, i = {}, 0
    try:
        while i < len(tree):
            space = tree.index(b" ", i)
            nul = tree.index(b"\0", space)
            entries.setdefault(tree[space + 1:nul], (tree[i:space], tree[nul + 1:nul + 21].hex()))
            i = nul + 21
    except ValueError:
        return "unverified", "SOURCE_OBJECTS_FAILED_VERIFICATION", "an object of the commit does not hash to its own name"
    for path, data in blobs.items():
        mode, oid = entries.get(path.encode(), (b"", ""))
        if mode not in (b"100644", b"100755"):           # a symlink or gitlink would make the link text "the file"
            return "refused", "SOURCE_FILE_NOT_A_REGULAR_FILE", path
        if _git_oid("blob", data) != oid:
            return "unverified", "SOURCE_OBJECTS_FAILED_VERIFICATION", "an object of the commit does not hash to its own name"
    return None


@dataclass
class Sources:
    compose: str
    template: str
    compose_sha256: str
    template_sha256: str


def read_sources(repo: str, sha: str, report: Report) -> Sources | None:
    try:
        if _partial_clone_configured(repo):                  # first: before any object is looked up
            report.refuse("PARTIAL_CLONE_UNSUPPORTED", "a missing object could be fetched over the network")
            return None
        commit = _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
        if commit.returncode not in (0, 1):
            report.unverifiable("GIT_FAILED", f"rev-parse exit {commit.returncode}")
            return None
        if commit.returncode == 1 or commit.stdout.decode("ascii", "replace").strip() != sha:
            report.refuse("SOURCE_SHA_NOT_A_LOCAL_COMMIT", "needs the full SHA of a commit present in --repo")
            return None
        blobs = {}
        for path in (COMPOSE_PATH, TEMPLATE_PATH):
            blob = _git(repo, "cat-file", "blob", f"{sha}:{path}")
            if blob.returncode != 0:
                report.refuse("SOURCE_FILE_MISSING", f"{path} at the given commit")
                return None
            if len(blob.stdout) > MAX_BLOB:
                report.refuse("SOURCE_FILE_TOO_LARGE", path)
                return None
            blobs[path] = blob.stdout
        bad = _binding(repo, sha, blobs)
        if bad is not None:
            (report.refuse if bad[0] == "refused" else report.unverifiable)(bad[1], bad[2])
            return None
    except _GitFailed as exc:
        report.unverifiable("GIT_FAILED", str(exc))
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        report.unverifiable("GIT_UNAVAILABLE", type(exc).__name__)
        return None
    texts = {}
    for path, raw in blobs.items():
        try:
            texts[path] = raw.decode("utf-8").replace("\r\n", "\n")
        except UnicodeDecodeError:
            report.unverifiable("SOURCE_FILE_NOT_UTF8", path)
            return None
        if "\r" in texts[path] or "\x00" in texts[path]:
            report.unverifiable("SOURCE_FILE_BAD_CHARACTERS", path)
            return None
    return Sources(texts[COMPOSE_PATH], texts[TEMPLATE_PATH],
                   sha256_hex(blobs[COMPOSE_PATH]), sha256_hex(blobs[TEMPLATE_PATH]))


# ── static scan of the Compose file: which variables does it reference ───────
@dataclass
class Scan:
    kinds: dict = field(default_factory=dict)      # name -> set of plain | default | required | alt
    problems: list = field(default_factory=list)   # (line, code)

    def required(self) -> list[str]:
        return sorted(n for n, k in self.kinds.items() if "required" in k)

    def only_plain(self) -> list[str]:
        return sorted(n for n, k in self.kinds.items() if k == {"plain"})


_OPERATORS = ((":-", "default"), (":?", "required"), (":+", "alt"), ("-", "default"), ("?", "required"), ("+", "alt"))
_BLOCK_START = re.compile(r"(^|\s)[|>][-+0-9]*$")
_UNSUPPORTED_KEY = re.compile(r"^\s*(?:-\s+)?(include|extends)\s*:")


def _strip_comment(line: str) -> tuple[str, bool]:
    """Cut a YAML comment (# after whitespace, outside quotes). Returns (text, unterminated_quote)."""
    quote, i = None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"" and (i == 0 or line[i - 1] in " \t,[{:"):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i], False
        i += 1
    return line, quote is not None


def _interpolations(line: str, no: int, scan: Scan) -> None:
    i = 0
    while True:
        i = line.find("$", i)
        if i < 0:
            return
        nxt = line[i + 1:i + 2]
        if nxt == "$":
            i += 2
            continue
        if nxt == "{":
            m = _NAME.match(line, i + 2)
            if not m:
                scan.problems.append((no, "BAD_VARIABLE_NAME"))
                return
            j = m.end()
            if line[j:j + 1] == "}":
                scan.kinds.setdefault(m.group(0), set()).add("plain")
                i = j + 1
                continue
            found = next(((sym, kind) for sym, kind in _OPERATORS if line.startswith(sym, j)), None)
            if found is None:
                scan.problems.append((no, "UNSUPPORTED_OPERATOR"))
                return
            sym, op = found
            end = line.find("}", j + len(sym))
            if end < 0:
                scan.problems.append((no, "UNTERMINATED_INTERPOLATION"))
                return
            if "$" in line[j + len(sym):end] or "{" in line[j + len(sym):end]:
                scan.problems.append((no, "NESTED_INTERPOLATION"))
                return
            scan.kinds.setdefault(m.group(0), set()).add(op)
            i = end + 1
            continue
        m = _NAME.match(line, i + 1)
        if not m:
            scan.problems.append((no, "LONE_DOLLAR"))
            return
        scan.kinds.setdefault(m.group(0), set()).add("plain")
        i = m.end()


def scan_compose(text: str) -> Scan:
    """References Compose would interpolate. Comments are skipped, block-scalar text is not."""
    scan, block_indent = Scan(), None
    for no, raw in enumerate(text.split("\n"), 1):
        indent = len(raw) - len(raw.lstrip(" "))
        if block_indent is not None:
            if not raw.strip() or indent > block_indent:
                _interpolations(raw, no, scan)
                continue
            block_indent = None
        if raw.strip().startswith("#"):
            continue
        body, open_quote = _strip_comment(raw)
        if open_quote:
            scan.problems.append((no, "MULTILINE_QUOTE"))
        if _UNSUPPORTED_KEY.match(body):
            scan.problems.append((no, "UNSUPPORTED_KEY"))
        _interpolations(body, no, scan)
        if _BLOCK_START.search(body.rstrip()):
            block_indent = indent
    return scan


# ── the .env template ────────────────────────────────────────────────────────
def parse_assignments(text: str) -> tuple[dict, list, list]:
    """(values, duplicate keys, unparseable line numbers); values are read like envfile.read_env."""
    values: dict[str, str] = {}
    duplicates, bad = [], []
    for no, line in enumerate(text.split("\n"), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ASSIGN.match(line)
        if not m:
            bad.append(no)
            continue
        key, raw = m.group(1), m.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        if key in values:
            duplicates.append(key)
        values[key] = raw
    return values, duplicates, bad


@dataclass
class Inputs:
    entries: dict
    missing_manual: list
    service_issued: list
    unresolved_required: list
    undefined_no_default: list
    optional_not_in_template: list

    @property
    def gaps(self) -> list[str]:
        return sorted(set(self.missing_manual) | set(self.unresolved_required))


def classify(entries: dict, scan: Scan, report: Report) -> Inputs | None:
    problems = False
    for key in GENERATED:
        if key not in entries:
            report.refuse("TEMPLATE_LACKS_GENERATED_KEY", key)
            problems = True
        elif not is_placeholder(entries[key]):
            report.refuse("TEMPLATE_GENERATED_KEY_NOT_PLACEHOLDER", key)
            problems = True
    for key, value in entries.items():
        if "$" in value:
            report.refuse("TEMPLATE_VALUE_HAS_DOLLAR", key)
            problems = True
    if problems:
        return None
    placeholders = [k for k, v in entries.items() if is_placeholder(v)]
    return Inputs(
        entries=entries,
        missing_manual=sorted(k for k in placeholders if k not in GENERATED and k not in SERVICE_ISSUED),
        service_issued=sorted(k for k in SERVICE_ISSUED if k in placeholders),
        unresolved_required=[n for n in scan.required() if not entries.get(n)],
        undefined_no_default=[n for n in scan.only_plain() if n not in entries],
        optional_not_in_template=sorted(n for n, k in scan.kinds.items()
                                        if n not in entries and "required" not in k and k != {"plain"}),
    )


def render_env(template: str, values: dict) -> str:
    out = []
    for line in template.split("\n"):
        m = _ASSIGN.match(line)
        out.append(f"{m.group(1)}={values[m.group(1)]}" if m and m.group(1) in values else line)
    text = "\n".join(out)
    return text if text.endswith("\n") else text + "\n"


def valid_generated(key: str, value: str) -> bool:
    try:
        if key == KEY_NAME:
            return bool(_FERNET.fullmatch(value)) and len(base64.urlsafe_b64decode(value)) == 32
        return bool(_PASSWORD.fullmatch(value))
    except ValueError:
        return False


# ── the target directory ─────────────────────────────────────────────────────
@dataclass
class Target:
    path: str          # normalised, as given
    real: str          # parent resolved, name kept
    parent: str
    name: str
    state: str = "absent"
    parent_ident: tuple | None = None     # (st_dev, st_ino) of the parent as preflight saw it


def _parent_unchanged(target: Target) -> bool:
    """The parent is still the directory preflight checked: the same resolved path and the same inode. The tool works by path
    below it, so a swap of the parent would put the set somewhere nobody checked; this finds that out instead of reporting success."""
    if target.parent_ident is None:
        return True
    try:
        info = os.stat(target.parent)
        return os.path.realpath(target.parent) == target.parent and (info.st_dev, info.st_ino) == target.parent_ident
    except OSError:
        return False


def _read_regular(path: str, limit: int, dir_fd: int | None = None) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)   # O_NONBLOCK: a FIFO must not hang the open
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


def key_fingerprint_state(directory: str) -> str:
    """MATCH | MISMATCH | UNVERIFIABLE: the recorded SHA-256 of the Langflow key against the key in .env."""
    try:
        record = json.loads(_read_regular(os.path.join(directory, RECORD_NAME), 1 << 16))
        wanted = record["langflow_secret_key_sha256"]
        if record.get("schema") != RECORD_SCHEMA or not _HEX64.fullmatch(wanted):
            return "UNVERIFIABLE"
        values, _, _ = parse_assignments(_read_regular(os.path.join(directory, ENV_NAME), MAX_BLOB).decode("utf-8"))
        return "MATCH" if sha256_hex(values[KEY_NAME]) == wanted else "MISMATCH"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "UNVERIFIABLE"


def classify_target(path: str) -> tuple[str, str]:
    """(state, detail) of an existing path. Never follows a symlink, never opens a foreign file."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        return "symlink", "dangling" if not os.path.exists(path) else "to-existing"
    if not stat.S_ISDIR(info.st_mode):
        return "file", ""
    names = set(os.listdir(path))
    if not names:
        return "empty-directory", ""
    if names == {ENV_NAME, RECORD_NAME}:
        return "config-set", key_fingerprint_state(path)
    if names & {ENV_NAME, RECORD_NAME}:
        return "partial-config", ""
    return "foreign-directory", ""


def normalize_target(raw: str, report: Report) -> str | None:
    if not raw or not os.path.isabs(raw) or ".." in pathlib.PurePath(raw).parts:
        report.refuse("TARGET_NOT_ABSOLUTE_OR_HAS_DOTDOT")
        return None
    path = os.path.normpath(raw)
    if not os.path.basename(path) or os.path.dirname(path) == path:
        report.refuse("TARGET_IS_ROOT")
        return None
    return path


def check_target(path: str, repo: str, report: Report, for_write: bool) -> Target | None:
    parent, name = os.path.split(path)
    if not os.path.isdir(parent):
        report.refuse("TARGET_PARENT_MISSING", "created by the operator, not by this tool")
        return None
    real_parent = os.path.realpath(parent)
    target = Target(path, os.path.join(real_parent, name), real_parent, name)
    roots = {os.path.realpath(repo)}
    try:
        top = _git(repo, "rev-parse", "--show-toplevel")
        if top.returncode == 0 and top.stdout.strip():
            roots.add(os.path.realpath(top.stdout.decode("utf-8", "replace").strip()))
    except (OSError, subprocess.SubprocessError):
        pass
    for root in roots:
        if target.real == root or target.real.startswith(root.rstrip(os.sep) + os.sep):
            report.refuse("TARGET_INSIDE_REPOSITORY")
            return None
    if for_write and not CAN_PUBLISH:
        report.refuse("UNSUPPORTED_PLATFORM", "init-config needs Linux (renameat2 RENAME_NOREPLACE)")
        return None
    if IS_POSIX:
        info = os.stat(real_parent)
        target.parent_ident = (info.st_dev, info.st_ino)
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            report.refuse("TARGET_PARENT_WRITABLE_BY_OTHERS")
            return None
        if info.st_uid not in (os.geteuid(), 0):
            report.refuse("TARGET_PARENT_OWNED_BY_ANOTHER_USER")
            return None
    try:
        state, detail = classify_target(target.real)
    except FileNotFoundError:
        state, detail = "absent", ""
    except OSError as exc:
        report.unverifiable("TARGET_UNREADABLE", type(exc).__name__)
        return None
    target.state = state
    try:
        stale = sorted(e for e in os.listdir(real_parent) if e.startswith(f".{name}.staging-"))
    except OSError as exc:
        report.unverifiable("TARGET_PARENT_UNREADABLE", type(exc).__name__)
        return None
    if stale:
        held, empty = [], []
        for entry in stale:                  # read-only: only a directory with no entry at all is "empty"; an emptied leftover of _discard is not
            try:
                (held if os.listdir(os.path.join(real_parent, entry)) else empty).append(entry)
            except OSError:
                held.append(entry)
        if held:
            report.say("STALE_STAGING (not touched, may hold unpublished secrets; inspect, then remove): " + ", ".join(held))
        if empty:
            report.say("STALE_STAGING_EMPTY (not touched, holds nothing; remove it yourself with rmdir): " + ", ".join(empty))
    if state != "absent":
        refuse_existing(report, state, detail, "nothing is written, replaced or regenerated")
        return None
    return target


def refuse_existing(report: Report, state: str, detail: str, note: str) -> None:
    report.refuse("TARGET_EXISTS_" + state.upper().replace("-", "_"), note)
    if state == "config-set":
        report.say(f"EXISTING_KEY_FINGERPRINT: {detail}")


# ── preflight / plan ─────────────────────────────────────────────────────────
@dataclass
class Context:
    sha: str
    repo: str
    sources: Sources
    scan: Scan
    inputs: Inputs
    target: Target


def preflight(sha: str, target_arg: str, repo: str, report: Report, for_write: bool = False) -> Context | None:
    if not SHA_RE.fullmatch(sha):
        report.refuse("SOURCE_SHA_NOT_FULL_LOWERCASE_HEX", "no abbreviations, refs or HEAD")
        return None
    target_path = normalize_target(target_arg, report)
    if target_path is None:
        return None
    if not os.path.isdir(repo):
        report.refuse("REPO_NOT_A_DIRECTORY")
        return None
    sources = read_sources(repo, sha, report)
    if sources is None:
        return None
    scan = scan_compose(sources.compose)
    for no, code in scan.problems[:20]:
        report.unverifiable("UNSUPPORTED_COMPOSE_CONSTRUCT", f"{code} at line {no}")
    entries, duplicates, bad = parse_assignments(sources.template)
    for key in duplicates:
        report.unverifiable("TEMPLATE_DUPLICATE_KEY", key)
    for no in bad[:20]:
        report.unverifiable("TEMPLATE_UNPARSEABLE_LINE", str(no))
    if report.failed:
        return None
    inputs = classify(entries, scan, report)
    if inputs is None:
        return None
    target = check_target(target_path, repo, report, for_write)
    if target is None:
        return None
    report.say("OFFLINE_CHECKS: OK (sources and target only)")
    return Context(sha, repo, sources, scan, inputs, target)


def describe_inputs(inputs: Inputs, report: Report) -> None:
    for label, names in (("MISSING_MANUAL", inputs.missing_manual), ("SERVICE_ISSUED_LATER", inputs.service_issued),
                         ("UNRESOLVED_REQUIRED", inputs.unresolved_required)):
        if names:
            report.say(f"{label}: " + ", ".join(names))
    report.say(f"INPUT_GAPS: {len(inputs.gaps)} (static scan; the Compose model is not rendered)")


def plan(ctx: Context, report: Report) -> None:
    report.say(f"SOURCE_SHA: {ctx.sha}")
    report.say(f"WILL_CREATE: {ctx.target.path} (0700) with {ENV_NAME} (0600) and {RECORD_NAME} (0600)")
    report.say("GENERATED_ONCE: " + ", ".join(GENERATED))
    describe_inputs(ctx.inputs, report)
    if ctx.inputs.undefined_no_default:
        report.say("UNDEFINED_NO_DEFAULT: " + ", ".join(ctx.inputs.undefined_no_default))
    if ctx.inputs.optional_not_in_template:
        report.say("OPTIONAL_NOT_IN_TEMPLATE: " + ", ".join(ctx.inputs.optional_not_in_template))
    report.say("NOT_EVALUATED: docker, containers, volumes, networks, ports, subnet inventory, "
               f"Compose model rendering, override files, anything outside the given commit")


# ── init-config ──────────────────────────────────────────────────────────────
_AT_FDCWD, _RENAME_NOREPLACE = -100, 1
_NOREPLACE_UNSUPPORTED = (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP)


def _rename_noreplace(src: str, dst: str) -> None:
    """renameat2(RENAME_NOREPLACE): EEXIST instead of replacing anything. No fallback; plain rename replaces an empty directory."""
    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError, TypeError) as exc:
        raise OSError(errno.ENOSYS, "renameat2 is not available") from exc
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(_AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), _RENAME_NOREPLACE) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _write_private(name: str, data: bytes, dir_fd: int | None = None, held: dict | None = None) -> None:
    """Create `name` (0600): never replacing, never following a link. With dir_fd it is made inside that directory, whatever its name is now.
    With `held` the descriptor stays open and is registered before anything is written, so this run can empty exactly this inode later."""
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dir_fd)
    if held is not None:
        held[name] = fd                                      # first thing: the file is still empty here, and the run closes this at its end
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        if held is None:
            os.close(fd)


def _fsync_dir(path: str, fd: int | None = None) -> None:
    """fsync a directory: through the descriptor when the caller holds one (the name may lead elsewhere by now), else by path."""
    if fd is not None:
        os.fsync(fd)
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _locate(fd: int | None) -> str:
    """Where the directory behind a descriptor is now, by the kernel's own bookkeeping (Linux /proc)."""
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except (OSError, TypeError, ValueError):
        return "an unknown location"


@dataclass
class _Run:
    staging: str | None = None
    fd: int | None = None               # the staging directory this run created, held open: every write, check and removal goes through it
    ident: tuple | None = None          # (st_dev, st_ino) of that directory; set only after it passed the owner, mode and emptiness checks
    held: dict = field(default_factory=dict)      # file name -> descriptor of a file THIS run created in it, open until the run ends
    attempted: bool = False             # the publishing rename was started
    parent_changed: bool = False        # the parent was no longer the checked directory when the staging was to be made
    published: bool = False
    rename_error: OSError | None = None
    self_check_failed: bool = False
    staging_replaced: bool = False      # the staging name no longer showed our directory when it was time to publish
    durability: str = ""


def _create_staging(run: _Run, parent: str, name: str) -> None:
    """Make the private staging directory and pin it by descriptor. Only a directory that is ours is ever written to or removed."""
    old = os.umask(0o077)                                    # born 0700 and readable by its owner whatever umask was inherited
    try:
        run.staging = tempfile.mkdtemp(prefix=f".{name}.staging-", dir=parent)
    finally:
        os.umask(old)
    run.fd = os.open(run.staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)      # refuses a link or file swapped in since mkdtemp
    info = os.fstat(run.fd)
    # The NAME was opened, not the inode mkdtemp made, so a foreign directory swapped in between is indistinguishable by identity. mkdtemp
    # gives an empty 0700 directory (umask 0077 here): whatever else this is, it is not touched (no fchmod), not filled, not removed.
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700 or os.listdir(run.fd):
        raise PermissionError(errno.EPERM, "the staging directory is not the empty private directory of this user that this run created")
    run.ident = (info.st_dev, info.st_ino)                   # last: until here the directory is not vouched for


def _empty_held(run: _Run) -> tuple[int, list[str]]:
    """Destroy the content of every file this run created, through the descriptors it has held since creating them.

    Only those inodes are touched, wherever their names lead now, and no name is removed or opened. Returns (emptied, names not emptied).
    """
    emptied, failed = 0, []
    for name, fd in run.held.items():
        try:
            os.ftruncate(fd, 0)
            if os.fstat(fd).st_size != 0:                    # "emptied" is claimed only for a file seen at zero length
                raise OSError(errno.EIO, "not empty after the truncation")
        except OSError:
            failed.append(name)
            continue
        emptied += 1
        try:
            os.fsync(fd)                                     # best effort: the zero length should reach the disk too
        except OSError:
            pass
    return emptied, failed


def _count_others(run: _Run) -> int | None:
    """How many entries of the staging directory are not an inode this run created (stat only, nothing is opened); None if unknown."""
    try:
        mine = {(info.st_dev, info.st_ino) for info in map(os.fstat, run.held.values())}
        others = 0
        for entry in os.listdir(run.fd):
            try:
                info = os.stat(entry, dir_fd=run.fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (info.st_dev, info.st_ino) not in mine:
                others += 1
        return others
    except OSError:
        return None


def _discard(run: _Run, report: Report) -> None:
    """Destroy the secrets of a run that published nothing and announce the staging directory. No name is ever removed.

    The content goes through the descriptors held since the files were created, so no by-name step can reach a foreign entry. POSIX has no
    unlink or rmdir by descriptor, and a by-name removal can always hit whatever took the name in between: the directory stays, announced.
    """
    path = run.staging
    if run.fd is None or run.ident is None:                  # never vouched for: not ours to touch
        report.say(f"LEFT_IN_PLACE: {path} (this run could not vouch that it is the directory it created; nothing in it was read, "
                   "changed or removed; look at it, then remove it yourself if it is not wanted)")
        return
    emptied, failed = _empty_held(run)
    here = _locate(run.fd)
    where = f"the directory is now at {here}; " if here != path else ""
    others = _count_others(run)
    rest = "the other entries could not be counted" if others is None else \
        f"{others} other {'entry is' if others == 1 else 'entries are'} present and left untouched"
    if failed:
        still = ", ".join(f"{name} at {_locate(run.held[name])}" for name in failed)
        report.say(f"STAGING_LEFT_NOT_EMPTIED: {path} ({where}{len(failed)} of {len(run.held)} files created by this run could NOT be emptied "
                   f"and may still hold secrets: {still}; {rest}; remove the directory yourself after looking at it)")
    else:
        report.say(f"STAGING_LEFT_EMPTIED: {path} ({where}{emptied} of {len(run.held)} files created by this run were emptied; {rest}; "
                   "this tool removes no name: look at the directory, then remove it yourself)")


def _release(run: _Run) -> None:
    """Close every descriptor the run holds, on every exit path; one failing close never keeps the others open."""
    for fd in (*run.held.values(), run.fd):
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    run.held.clear()
    run.fd = None


def _verify_staged(staging: str, env_bytes: bytes, record_bytes: bytes, sha_key: str, dir_fd: int) -> bool:
    """The directory behind dir_fd is the set we meant: ours and 0700, exactly our two files with our bytes and 0600, a record naming the key."""
    info = os.fstat(dir_fd)
    if IS_POSIX and (stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid()):
        return False
    if sorted(os.listdir(dir_fd)) != sorted((ENV_NAME, RECORD_NAME)):
        return False
    for name, data in ((ENV_NAME, env_bytes), (RECORD_NAME, record_bytes)):
        if _read_regular(name, MAX_BLOB, dir_fd) != data:
            return False
        if IS_POSIX and stat.S_IMODE(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode) != 0o600:
            return False
    values, _, _ = parse_assignments(env_bytes.decode("utf-8"))
    return sha256_hex(values.get(KEY_NAME, "")) == sha_key and json.loads(record_bytes)["langflow_secret_key_sha256"] == sha_key


def _interrupt(*_) -> None:
    raise KeyboardInterrupt


def _publication_state(target: str, staging: str, ident: tuple) -> str:
    """published | not-published | unknown, from the disk and not from a flag: is our inode at the target, or still at the staging name?"""
    def ours(path):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        return stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == ident

    try:
        if ours(target):
            return "published"
        if ours(staging):
            return "not-published"
    except OSError:
        pass
    return "unknown"


def _stage_and_publish(run: _Run, ctx: Context, env_bytes: bytes, record_bytes: bytes, sha_key: str, at) -> None:
    at("begin")
    if not _parent_unchanged(ctx.target):
        run.parent_changed = True
        return
    _create_staging(run, ctx.target.parent, ctx.target.name)
    at("staged_dir")
    _write_private(ENV_NAME, env_bytes, run.fd, run.held)
    at("env_written")
    _write_private(RECORD_NAME, record_bytes, run.fd, run.held)
    at("record_written")
    _fsync_dir(run.staging, run.fd)
    if not _verify_staged(run.staging, env_bytes, record_bytes, sha_key, run.fd):
        run.self_check_failed = True
        return
    at("before_publish")
    if _publication_state(ctx.target.real, run.staging, run.ident) != "not-published":
        run.staging_replaced = True         # the rename takes a name: never hand it one that no longer shows our directory
        return
    run.attempted = True
    try:
        _rename_noreplace(run.staging, ctx.target.real)      # a signal may land right after this returns: init_config then asks the disk
        run.published = True
    except OSError as exc:
        run.rename_error = exc
        return
    at("renamed")
    try:
        _fsync_dir(ctx.target.parent)
    except OSError as exc:
        run.durability = errno.errorcode.get(exc.errno, type(exc).__name__)


def _errno_name(exc: BaseException | None) -> str:
    return errno.errorcode.get(getattr(exc, "errno", None), type(exc).__name__)


def _conclude(report: Report, ctx: Context, run: _Run, interrupted: bool, error: BaseException | None,
              env_bytes: bytes, record_bytes: bytes, sha_key: str) -> bool:
    """Say, from the disk and not from a flag, what this run did, and clean up after a failure. True: our verified set is at the target."""
    if run.attempted:                                        # asked after every rename, not only after an interrupt or error
        state = _publication_state(ctx.target.real, run.staging, run.ident)
        if state == "published":
            run.published = True
            error = error or run.rename_error
            if not _parent_unchanged(ctx.target):
                report.unverifiable("TARGET_PARENT_CHANGED", "the parent directory is no longer the one that was checked; the directory "
                                    f"this run created is now at {_locate(run.fd)}; nothing was removed or changed by this tool")
                return False
        elif state == "unknown" or run.published:
            report.unverifiable("PUBLICATION_STATE_UNKNOWN", "the disk does not show our directory at the target or at the staging name; "
                                f"the directory this run created is now at {_locate(run.fd)}; nothing was removed by this tool (if the target is not ours, "
                                "the publishing rename moved that object there, untouched)")
            return False
    if not run.published:
        if run.staging is not None:
            _discard(run, report)
        failed = run.rename_error
        if run.parent_changed:
            report.unverifiable("TARGET_PARENT_CHANGED", "nothing written; the parent directory is no longer the one that was checked")
        elif run.self_check_failed:
            report.unverifiable("STAGED_SET_FAILED_SELF_CHECK", "nothing published")
        elif run.staging_replaced:
            report.unverifiable("STAGING_REPLACED", "nothing published; the staging name no longer showed the directory this run created and was left alone")
        elif failed is not None and failed.errno == errno.EEXIST:
            try:
                state, detail = classify_target(ctx.target.real)
            except OSError:
                state, detail = "unknown", ""
            refuse_existing(report, state, detail, "appeared meanwhile; nothing written or replaced")
        elif failed is not None and failed.errno in _NOREPLACE_UNSUPPORTED:
            report.unverifiable("PUBLISH_NOREPLACE_UNSUPPORTED", "this filesystem or kernel cannot refuse to replace; nothing published")
        elif interrupted:
            report.unverifiable("INTERRUPTED", "nothing published")
        else:
            report.unverifiable("WRITE_FAILED", f"{_errno_name(failed or error)}; nothing published")
        return False
    if not _verify_staged(run.staging, env_bytes, record_bytes, sha_key, run.fd):      # our inode, read through the descriptor
        report.unverifiable("PUBLISHED_SET_FAILED_VERIFICATION", "the set was published but could not be confirmed complete")
        return False
    if interrupted:
        report.say("INTERRUPTED_AFTER_PUBLICATION: the complete set is on disk")
    elif isinstance(error, OSError):
        report.say(f"DURABILITY_UNCONFIRMED: {_errno_name(error)}")
    elif error is not None:
        report.say(f"ERROR_AFTER_PUBLICATION: {type(error).__name__}; the complete set is on disk")
    if run.durability:
        report.say(f"DURABILITY_UNCONFIRMED: {run.durability}")
    return True


def init_config(sha: str, target_arg: str, repo: str, *, gen: dict | None = None, clock=None, fault=None) -> Report:
    """Build the set in a private staging directory and publish it with one RENAME_NOREPLACE rename.

    The report follows the disk, not a flag. `gen`, `clock` and `fault` are test seams; the command line never passes them.
    """
    report = Report()
    ctx = preflight(sha, target_arg, repo, report, for_write=True)
    if ctx is None:
        return report
    generators = gen or GENERATORS
    values = {key: generators[key]() for key in GENERATED}
    if not all(valid_generated(key, value) for key, value in values.items()):
        report.unverifiable("GENERATED_VALUE_INVALID")
        return report
    env_text = render_env(ctx.sources.template, values)
    sha_key = sha256_hex(values[KEY_NAME])
    blockers = ["HOST_NOT_CHECKED"] + [f"MISSING_MANUAL:{n}" for n in ctx.inputs.missing_manual] \
        + [f"UNRESOLVED_REQUIRED:{n}" for n in ctx.inputs.unresolved_required]
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    record = {
        "schema": RECORD_SCHEMA, "status": "CONFIG_PREPARED", "created_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_sha": ctx.sha, "sources": {COMPOSE_PATH: ctx.sources.compose_sha256, TEMPLATE_PATH: ctx.sources.template_sha256},
        "generated_keys": list(GENERATED), "langflow_secret_key_sha256": sha_key,
        "env_sha256_at_creation": sha256_hex(env_text),
        "input_gaps": {"missing_manual": ctx.inputs.missing_manual, "service_issued_later": ctx.inputs.service_issued,
                       "unresolved_required": ctx.inputs.unresolved_required},
        "host_checked": False, "installation_started": False, "ready_for_install": False, "blockers": blockers,
    }
    env_bytes = env_text.encode("utf-8")
    record_bytes = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")

    run, interrupted, error, ok = _Run(), False, None, False
    previous = {}
    for signum in (signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)):      # SIGHUP: a closed terminal or SSH session
        try:
            previous[signum] = signal.signal(signum, _interrupt)
        except (ValueError, OSError):
            pass
    try:                                    # the SIGTERM handler stays until the result is known and the cleanup is done
        try:
            _stage_and_publish(run, ctx, env_bytes, record_bytes, sha_key, fault or (lambda step: None))
        except KeyboardInterrupt:
            interrupted = True
        except Exception as exc:  # noqa: BLE001 - a step never crashes the run: whatever happened is judged from the disk below
            error = exc
        try:
            ok = _conclude(report, ctx, run, interrupted, error, env_bytes, record_bytes, sha_key)
        except KeyboardInterrupt:           # a second interrupt while the result is determined or the cleanup runs: admit it, never hide it
            left = f"; staging directory {run.staging} may remain" if run.staging else ""
            if run.held and (not run.attempted or run.rename_error is not None):      # the rename never ran or failed: nothing worth keeping
                emptied, failed = _empty_held(run)
                left += f" ({emptied} of its {len(run.held)} files emptied" + (f", {len(failed)} could NOT be emptied and may still hold secrets)" if failed else ")")
            report.unverifiable("INTERRUPTED_WHILE_FINISHING", f"the result was not confirmed{left}; inspect {ctx.target.path} and the parent directory")
            return report
    finally:
        try:
            _release(run)
        finally:
            for signum, old in previous.items():
                signal.signal(signum, old)
    if not ok:
        return report
    report.say("CONFIG_PREPARED")
    report.say(f"CONFIG_FILE: {os.path.join(ctx.target.path, ENV_NAME)}")
    report.say(f"RECORD_FILE: {os.path.join(ctx.target.path, RECORD_NAME)}")
    report.say(f"SOURCE_SHA: {ctx.sha}")
    report.say("GENERATED_ONCE: " + ", ".join(GENERATED))
    report.say(f"LANGFLOW_SECRET_KEY_SHA256: {sha_key}")
    describe_inputs(ctx.inputs, report)
    if not interrupted and error is None and not run.durability and run.rename_error is None:     # a clean success only
        report.reveal = {key: values[key] for key in GENERATED}          # held apart from `lines`; emit() decides whether to show
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("preflight", "plan", "init-config"):
        p = sub.add_parser(name)
        p.add_argument("--source-sha", required=True, metavar="SHA40", help="full 40-hex commit SHA; there is no default")
        # --config-dir and --source-root are the names of scripts/eduai_layout.py (tests/test_layout.py keeps them in step).
        p.add_argument("--target-dir", "--config-dir", required=True, metavar="ABS_DIR", help="absolute path of the new configuration directory")
        p.add_argument("--repo", "--source-root", default=str(ROOT), metavar="DIR", help="git repository to read the commit from")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "init-config":
            report = init_config(args.source_sha, args.target_dir, args.repo)
        else:
            report = Report()
            ctx = preflight(args.source_sha, args.target_dir, args.repo, report)
            if ctx is not None and args.cmd == "plan":
                plan(ctx, report)
    except Exception as exc:  # noqa: BLE001 - a bug must not print values from a traceback
        report = Report()
        report.unverifiable("INTERNAL_ERROR", type(exc).__name__)
    try:
        sys.stdout.reconfigure(errors="backslashreplace")    # an odd path must not turn into a traceback instead of the result
    except (AttributeError, ValueError, OSError):
        pass
    try:
        report.emit()
        sys.stdout.flush()                                   # a closed pipe fails here, not silently at interpreter exit (exit 120)
    except (OSError, ValueError, UnicodeError):              # a dead pipe or terminal: the exit status stays the result's, no traceback
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())      # nothing is left to flush into the dead stream
        except (OSError, ValueError, AttributeError):
            pass
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
