#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-07-27).
"""Read and edit `.env` in place, preserving comments and ordering.

Shared by bootstrap.py, create_api_key.py and smoke.py. Standard library only.

The point of editing in place rather than rewriting from a template: `.env`
holds every secret for the deployment, and a regenerating writer would silently
drop anything an operator added by hand. Only the requested keys change.
"""

from __future__ import annotations

import pathlib
import re

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


def set_env(path: str | pathlib.Path, updates: dict[str, str]) -> None:
    """Apply `updates` to a .env file, keeping comments, order and unknown keys.

    Keys already present are rewritten where they stand; new ones are appended.
    Always written with LF endings — Compose keeps a trailing CR as part of the
    value, which turns a password into a password-plus-invisible-character.
    """
    if not updates:
        return

    path = pathlib.Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(updates)

    for index, line in enumerate(lines):
        match = _ASSIGN.match(line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            lines[index] = f"{key}={remaining.pop(key)}"

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# ── Added by scripts/bootstrap.py ─────────────")
        lines.extend(f"{key}={value}" for key, value in remaining.items())

    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
