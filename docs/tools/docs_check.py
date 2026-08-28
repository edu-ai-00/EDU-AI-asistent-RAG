#!/usr/bin/env python3
# last change in code by Martin Safranek (2026-07-23).
"""docs-check — validate the RAG module documentation set.

Checks exactly three things (per the docs guidelines):
  1. Local markdown links and same-file anchors resolve.
  2. Every `sources:` path in frontmatter exists (relative to repo root).
  3. Frontmatter has the required fields with allowed values.

OpenAPI validation and parity-vector tests are NOT run here (they belong in CI).
No third-party dependencies — a minimal frontmatter parser handles the subset of
YAML this repo uses (scalars, inline `[a, b]` lists, block `- item` lists).

Usage:  python3 docs/tools/docs_check.py   (exit 0 = OK, 1 = problems)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# repo root = two levels up from docs/tools/
ROOT = Path(__file__).resolve().parents[2]
# Frontmatter is required on the guideline-defined pages only. The other files
# under docs/ (deploy runbooks, proposals, references) are prose, not doc-tree
# pages, and carry no frontmatter by design.
DOC_DIRS = [ROOT / "docs" / "ai", ROOT / "docs" / "architecture", ROOT / "specs"]
# Hand-written references that live directly under docs/ and carry frontmatter too,
# so their `sources:` paths are validated like any other page.
DOC_FILES = [ROOT / "docs" / n for n in
             ("SETTINGS_REFERENCE.md", "DATA_MODEL_AND_SETUP.md", "ADDING_A_LANGUAGE.md")]
LINK_DIRS = [ROOT / "docs", ROOT / "specs", ROOT / "contracts", ROOT / "AGENTS.md"]

REQUIRED = ["id", "status", "components", "sources", "last_verified"]
ALLOWED_STATUS = {"draft", "active", "current", "completed", "deprecated"}
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
ATX = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")

errors: list[str] = []


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


# ── minimal frontmatter parser ────────────────────────────────
def parse_frontmatter(text: str):
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    body = text[3:end].strip("\n")
    data: dict = {}
    key = None
    for line in body.split("\n"):
        if not line.strip():
            continue
        if line.startswith("  - ") and key:               # block list item
            data.setdefault(key, [])
            if isinstance(data[key], list):
                data[key].append(line[4:].strip())
            continue
        m = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val == "":
            data[key] = []                                 # opens a block list
        elif val.startswith("[") and val.endswith("]"):    # inline list
            data[key] = [v.strip() for v in val[1:-1].split(",") if v.strip()]
        else:
            data[key] = val
    return data


def slug(heading: str) -> str:
    s = heading.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"\s+", "-", s)


def heading_slugs(path: Path) -> set[str]:
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = ATX.match(line)
        if m:
            out.add(slug(m.group(1)))
    return out


# ── checks ────────────────────────────────────────────────────
def check_frontmatter(path: Path):
    fm = parse_frontmatter(path.read_text(encoding="utf-8"))
    if fm is None:
        errors.append(f"{rel(path)}: missing YAML frontmatter")
        return
    for field in REQUIRED:
        if field not in fm or fm[field] in ("", [], None):
            errors.append(f"{rel(path)}: frontmatter missing '{field}'")
    if isinstance(fm.get("status"), str) and fm["status"] not in ALLOWED_STATUS:
        errors.append(f"{rel(path)}: status '{fm['status']}' not in {sorted(ALLOWED_STATUS)}")
    if not isinstance(fm.get("components"), list) or not fm.get("components"):
        errors.append(f"{rel(path)}: 'components' must be a non-empty list")
    lv = fm.get("last_verified", "")
    if isinstance(lv, str) and not ISO_DATE.match(lv):
        errors.append(f"{rel(path)}: last_verified '{lv}' is not an ISO date (YYYY-MM-DD)")
    for src in fm.get("sources", []) if isinstance(fm.get("sources"), list) else []:
        if not (ROOT / src).exists():
            errors.append(f"{rel(path)}: sources path does not exist: {src}")


def check_links(path: Path):
    text = path.read_text(encoding="utf-8")
    own = heading_slugs(path)
    for target in LINK.findall(text):
        target = target.strip()
        if target.startswith(("http://", "https://", "mailto:", "tel:")):
            continue
        if target.startswith("#"):                          # same-file anchor
            if slug(target[1:]) not in own:
                errors.append(f"{rel(path)}: dead anchor {target}")
            continue
        filepart, _, anchor = target.partition("#")
        dest = (path.parent / filepart).resolve()
        if not dest.exists():
            errors.append(f"{rel(path)}: dead link {target}")
        elif anchor and dest.suffix == ".md" and slug(anchor) not in heading_slugs(dest):
            errors.append(f"{rel(path)}: dead anchor {target}")


def main() -> int:
    fm_files = sorted([p for d in DOC_DIRS for p in d.rglob("*.md")] +
                  [p for p in DOC_FILES if p.exists()])
    link_files = sorted(p for d in LINK_DIRS for p in d.rglob("*.md"))
    for p in fm_files:
        check_frontmatter(p)
    for p in link_files:
        check_links(p)
    print(f"docs-check: {len(fm_files)} doc pages, {len(link_files)} markdown files")
    if errors:
        print(f"\n{len(errors)} problem(s):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("OK — links resolve, sources exist, frontmatter valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
