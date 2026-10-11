#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-07-27).
"""Read and edit `.env` in place, preserving comments and ordering.

Shared by bootstrap.py, create_api_key.py and smoke.py. Standard library only.

The point of editing in place rather than rewriting from a template: `.env`
holds every secret for the deployment, and a regenerating writer would silently
drop anything an operator added by hand. Only the requested keys change.

Writes are atomic: the new content is written to a temporary file in the same directory,
flushed to disk and then renamed over the target, so a failure at any point leaves the
previous file exactly as it was. A symlinked .env stays a symlink (the file it points to
is the one replaced); an existing file keeps its mode, a new one is created 0600 from its
first byte. On Windows the 0600 request is not evidence of a private ACL.
"""

from __future__ import annotations

import contextlib
import errno
import os
import pathlib
import re
import stat
import tempfile

# Values .env.example ships as a stand-in for a real secret. Anything still
# carrying this prefix counts as "not configured" everywhere in the tooling.
PLACEHOLDER_PREFIX = "CHANGE_ME"

_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def is_placeholder(value: str | None) -> bool:
    """True when a value is absent or still the .env.example placeholder."""
    return not value or value.strip().startswith(PLACEHOLDER_PREFIX)


def read_env(path: str | pathlib.Path) -> dict[str, str]:
    """Parse a .env file into a dict. Missing file → empty dict.

    Deliberately minimal: no interpolation, no multi-line values. Note that
    Compose does NOT treat .env the same way — it expands ${OTHER_VAR} there, so
    the two parsers disagree by design. That is exactly why .env must only ever
    hold literal values: a reference would reach the containers expanded and this
    reader unexpanded, and the tooling would silently disagree with the running
    stack about what is configured. Stated in the header of .env.example too.
    """
    path = pathlib.Path(path)
    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ASSIGN.match(line)
        if not match:
            continue
        key, raw = match.group(1), match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        values[key] = raw
    return values


PRIVATE_MODE = 0o600
_HAS_MODES = os.name == "posix"
# link() may be refused by a filesystem; these mean "use the exclusive-create fallback", not "failed".
_NO_LINK = (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.ENOSYS, errno.EMLINK)


def _real_path(path: str | pathlib.Path) -> pathlib.Path:
    """`path` with symlinks resolved, so the link itself survives a write (an external config set)."""
    return pathlib.Path(os.path.realpath(path))


def _fsync_dir(directory: pathlib.Path) -> None:
    """Make a rename or link durable. Best effort: a platform that cannot open a directory skips it."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _temp_file(directory: pathlib.Path, name: str, data: bytes, mode: int) -> str:
    """A fully written, flushed temporary file in `directory`; mkstemp makes it exclusively and 0600."""
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            if _HAS_MODES:
                os.fchmod(handle.fileno(), mode)        # before the first byte: never wider than the target will be
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return tmp


def write_atomic(path: str | pathlib.Path, data: bytes) -> None:
    """Replace `path` with `data` all at once. Any failure leaves the previous file untouched and no temporary file behind.

    A symlink is followed, so the link stays and its target is replaced. The mode of an existing file is kept;
    a file that does not exist yet is created 0600.
    """
    target = _real_path(path)
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        mode = PRIVATE_MODE
    tmp = _temp_file(target.parent, target.name, data, mode)
    try:
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(target.parent)


def create_private(path: str | pathlib.Path, data: bytes) -> None:
    """Create `path` holding `data`, mode 0600 from its first byte, complete or not at all. Never replaces: FileExistsError.

    The content is written to a private temporary file and linked into place; where the filesystem refuses a
    link, an exclusive create (O_EXCL, still 0600) is used instead and a partial file is removed on failure.
    On Linux the mode is checked afterwards. A symlink is followed (the file at its target is created).
    """
    target = _real_path(path)
    tmp = _temp_file(target.parent, target.name, data, PRIVATE_MODE)
    try:
        try:
            os.link(tmp, target)
        except FileExistsError:
            raise
        except OSError as exc:
            if exc.errno not in _NO_LINK:
                raise
            _create_exclusive(target, data)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    _fsync_dir(target.parent)
    if _HAS_MODES and stat.S_IMODE(target.stat().st_mode) != PRIVATE_MODE:
        raise OSError(errno.EPERM, "the new file is not private (mode is not 0600)")


def _create_exclusive(target: pathlib.Path, data: bytes) -> None:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), PRIVATE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            if _HAS_MODES:
                os.fchmod(handle.fileno(), PRIVATE_MODE)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(target)                           # ours: created exclusively a moment ago
        raise


def set_env(path: str | pathlib.Path, updates: dict[str, str]) -> None:
    """Apply `updates` to a .env file, keeping comments, order and unknown keys.

    Keys already present are rewritten where they stand (every occurrence); new ones are appended.
    Always written with LF endings — Compose keeps a trailing CR as part of the
    value, which turns a password into a password-plus-invisible-character.
    """
    if not updates:
        return

    path = pathlib.Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(updates)

    # Every occurrence is rewritten: with a duplicated line the LAST one wins (here and in Compose),
    # so updating only the first would leave the old value in force.
    for index, line in enumerate(lines):
        match = _ASSIGN.match(line)
        if match and match.group(1) in updates:
            key = match.group(1)
            lines[index] = f"{key}={updates[key]}"
            remaining.pop(key, None)

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# ── Added by scripts/bootstrap.py ─────────────")
        lines.extend(f"{key}={value}" for key, value in remaining.items())

    write_atomic(path, ("\n".join(lines) + "\n").encode("utf-8"))
