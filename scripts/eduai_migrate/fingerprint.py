"""Schema and configuration fingerprints: ONE implementation for generator and runner."""
from __future__ import annotations

import bisect
import hashlib
import re

from . import sqltok

ALGORITHM = "edu-ai-fp-v1"

# The registry is excluded from the schema fingerprint by definition; its own
# definition is verified separately (registry.py).
SCHEMA_EXCLUDES = ("edu_ai.langchain_pg_*", "edu_ai.schema_migration")
SCHEMA_DUMP_ARGS = ["--schema-only", "--schema=edu_ai", "--no-owner", "--no-privileges"] + \
    [f"--exclude-table={p}" for p in SCHEMA_EXCLUDES]

# Same list as SEED_TABLES in scripts/gen_clean_install.sh (a test keeps them equal).
SEED_TABLES = ("code_list", "taxonomy_rules", "taxonomy_binding", "embedding_registry",
               "prompt_registry", "system_message", "ui_label", "qa_stopword", "qa_synonym",
               "qa_blocklist", "chunk_exclusion_rule", "answer_exclusion_rule", "grade_term",
               "subject_term", "validity_period", "settings", "legal_entity", "staff_user")
CONFIG_DUMP_ARGS = ["--data-only", "--column-inserts", "--disable-triggers", "--no-owner",
                    "--no-privileges"] + [f"--table=edu_ai.{t}" for t in SEED_TABLES]
MASK_COLUMNS = ("created_at", "updated_at")

CONTENT_TABLES = ("document_metadata", "qa_entries", "question_answer_log")

_DUMP_NOISE = ("-- Name:", "-- Dumped from database version", "-- Dumped by pg_dump version",
               "-- PostgreSQL database dump", "\\restrict ", "\\unrestrict ")
_INSERT_HEAD = re.compile(r"INSERT\s+INTO\s+(\S+)\s*\((.*?)\)\s*(OVERRIDING\s+(?:SYSTEM|USER)\s+VALUE\s+)?VALUES\s*\(",
                          re.S)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def file_sha256(data: bytes) -> str:
    """Hash with CRLF folded to LF: the form git stores (.gitattributes: eol=lf).

    A CR that really sits inside a seed literal still changes the configuration
    fingerprint, so folding it here hides nothing that matters.
    """
    return sha256_hex(data.replace(b"\r\n", b"\n"))


def normalize_schema_dump(dump: str) -> str:
    """Drop pg_dump's own banners, version lines and \\restrict tokens; keep all else.

    Only lines that START outside a string, quoted identifier or dollar-quoted
    body are metadata. A body line that looks like a banner is object content.
    """
    text = dump.replace("\r\n", "\n")
    spans = [(t.start, t.end) for t in sqltok.tokenize(text) if t.kind in ("string", "dollar", "ident")]
    starts = [s for s, _ in spans]

    def inside_literal(offset: int) -> bool:
        i = bisect.bisect_left(starts, offset) - 1
        return i >= 0 and spans[i][0] < offset < spans[i][1]

    keep, offset = [], 0
    for line in text.split("\n"):
        if inside_literal(offset) or not (line == "--" or line.startswith(_DUMP_NOISE)):
            keep.append(line)
        offset += len(line) + 1
    return "\n".join(keep).strip("\n") + "\n"


def normalize_config_dump(dump: str) -> str:
    """Statement-wise, row-order independent, volatile timestamp columns masked."""
    out = []
    for s in sqltok.split_statements(dump):
        if s.kind == "meta":  # \restrict / \unrestrict carry a random token
            continue
        stmt = dump[s.start:s.end]
        m = _INSERT_HEAD.match(stmt)
        if not m:
            if stmt[:6].upper() == "INSERT":
                raise ValueError("unrecognised INSERT shape in the configuration dump")
            out.append(" ".join(stmt.split()))
            continue
        cols = [c.strip() for c in m.group(2).split(",")]
        body = stmt[m.end():].rstrip()
        if not body.endswith(");"):
            raise ValueError(f"unexpected INSERT shape for {m.group(1)}")
        values = sqltok.split_top_level(body[:-2])
        if len(values) != len(cols):
            raise ValueError(f"INSERT into {m.group(1)}: {len(cols)} columns, {len(values)} values")
        values = ["'<masked>'" if c in MASK_COLUMNS else v for c, v in zip(cols, values)]
        over = " ".join(m.group(3).split()) + " " if m.group(3) else ""
        out.append(f"INSERT INTO {m.group(1)} ({', '.join(cols)}) {over}VALUES ({', '.join(values)});")
    return "\n".join(sorted(out)) + "\n"


def schema_fingerprint(raw_dump: str) -> str:
    return sha256_hex(normalize_schema_dump(raw_dump))


def config_fingerprint(raw_dump: str) -> str:
    return sha256_hex(normalize_config_dump(raw_dump))


def measure_schema(target) -> str:
    from . import pg
    return schema_fingerprint(pg.pg_dump_text(target, SCHEMA_DUMP_ARGS))


def measure_config(target) -> str:
    from . import pg
    return config_fingerprint(pg.pg_dump_text(target, CONFIG_DUMP_ARGS))
