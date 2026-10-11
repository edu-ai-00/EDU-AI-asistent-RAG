"""Small PostgreSQL/psql lexer: top-level statements, quoting and migration lint.

Standard library only. It follows psql's own rules for what hides text from the
statement splitter (comments, strings, identifiers, dollar quoting, parentheses),
so a keyword is judged only where psql would see it at the start of a statement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_WORD = re.compile(r"[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_$\u0080-\U0010ffff]*")
_NUMBER = re.compile(r"[0-9][0-9A-Za-z_.]*")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_\u0080-\U0010ffff]*)?\$")


class LexError(Exception):
    def __init__(self, message: str, line: int):
        super().__init__(message)
        self.line = line


@dataclass
class Tok:
    kind: str  # word, ident, string, dollar, number, param, punct, meta
    start: int
    end: int
    line: int


def tokenize(text: str) -> list[Tok]:
    """Significant tokens only (whitespace and comments are dropped)."""
    toks: list[Tok] = []
    i, n, line = 0, len(text), 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c in " \t\r\f\v":
            i += 1
        elif text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            if depth:
                raise LexError("unterminated /* comment", line)
            line += text.count("\n", i, j)
            i = j
        elif c == "'":
            prev = toks[-1] if toks else None
            esc = bool(prev and prev.kind == "word" and prev.end == i
                       and text[prev.start:prev.end] in ("E", "e"))
            j = i + 1
            while True:
                if j >= n:
                    raise LexError("unterminated string literal", line)
                ch = text[j]
                if esc and ch == "\\":
                    j += 2
                elif ch == "'":
                    if text.startswith("''", j):
                        j += 2
                    else:
                        break
                else:
                    j += 1
            toks.append(Tok("string", i, j + 1, line))
            line += text.count("\n", i, j + 1)
            i = j + 1
        elif c == '"':
            j = i + 1
            while True:
                if j >= n:
                    raise LexError("unterminated quoted identifier", line)
                if text[j] == '"':
                    if text.startswith('""', j):
                        j += 2
                        continue
                    break
                j += 1
            toks.append(Tok("ident", i, j + 1, line))
            line += text.count("\n", i, j + 1)
            i = j + 1
        elif c == "$":
            if i + 1 < n and text[i + 1].isdigit():
                j = i + 1
                while j < n and text[j].isdigit():
                    j += 1
                toks.append(Tok("param", i, j, line))
                i = j
                continue
            m = _DOLLAR_TAG.match(text, i)
            if not m:
                toks.append(Tok("punct", i, i + 1, line))
                i += 1
                continue
            tag = m.group(0)
            j = text.find(tag, m.end())
            if j < 0:
                raise LexError(f"unterminated dollar-quoted string {tag}", line)
            toks.append(Tok("dollar", i, j + len(tag), line))
            line += text.count("\n", i, j + len(tag))
            i = j + len(tag)
        elif c == "\\":
            j = text.find("\n", i)
            j = n if j < 0 else j
            toks.append(Tok("meta", i, j, line))
            i = j
        else:
            m = _WORD.match(text, i) if not c.isdigit() else None
            if m:
                toks.append(Tok("word", i, m.end(), line))
                i = m.end()
                continue
            m = _NUMBER.match(text, i)
            if m:
                toks.append(Tok("number", i, m.end(), line))
                i = m.end()
                continue
            toks.append(Tok("punct", i, i + 1, line))
            i += 1
    return toks


@dataclass
class Stmt:
    kind: str  # 'sql' or 'meta'
    line: int
    start: int
    end: int
    toks: list[Tok] = field(default_factory=list)
    terminated: bool = True

    def words(self, text: str, limit: int = 8) -> list[str]:
        """Upper-cased leading bare words (stops at the first non-word token)."""
        out: list[str] = []
        for t in self.toks[:limit]:
            if t.kind != "word":
                break
            out.append(text[t.start:t.end].upper())
        return out

    def has_word(self, text: str, word: str) -> bool:
        return any(t.kind == "word" and text[t.start:t.end].upper() == word for t in self.toks)


def split_statements(text: str) -> list[Stmt]:
    """Split at top-level ';' the way psql does (parentheses and BEGIN ATOMIC bodies hold)."""
    stmts: list[Stmt] = []
    cur: list[Tok] = []
    depth = atomic = 0
    for t in tokenize(text):
        if t.kind == "meta":
            stmts.append(Stmt("meta", t.line, t.start, t.end, [t]))
            continue
        word = text[t.start:t.end].upper() if t.kind == "word" else ""
        if t.kind == "punct":
            ch = text[t.start]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
        elif atomic:
            if word == "CASE":
                atomic += 1
            elif word == "END":
                atomic -= 1
        elif word == "ATOMIC" and cur and cur[-1].kind == "word" \
                and text[cur[-1].start:cur[-1].end].upper() == "BEGIN" \
                and _is_create_routine(cur, text):
            atomic = 1
        cur.append(t)
        if t.kind == "punct" and text[t.start] == ";" and depth == 0 and not atomic:
            stmts.append(Stmt("sql", cur[0].line, cur[0].start, t.end, cur))
            cur = []
    if cur:
        stmts.append(Stmt("sql", cur[0].line, cur[0].start, cur[-1].end, cur, terminated=False))
    return stmts


def _is_create_routine(toks: list[Tok], text: str) -> bool:
    head = [text[t.start:t.end].upper() for t in toks[:6] if t.kind == "word"]
    return bool(head) and head[0] == "CREATE" and ("FUNCTION" in head or "PROCEDURE" in head)


def split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split a value list at top-level separators (quotes, parentheses, brackets hold)."""
    parts: list[str] = []
    depth, start = 0, 0
    for t in tokenize(text):
        if t.kind != "punct":
            continue
        ch = text[t.start]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif ch == sep and depth == 0:
            parts.append(text[start:t.start].strip())
            start = t.end
    parts.append(text[start:].strip())
    return parts


@dataclass
class Finding:
    line: int
    rule: str
    message: str

    def format(self, name: str) -> str:
        return f"{name}:{self.line}: [{self.rule}] {self.message}"


_TX_CONTROL = {
    ("BEGIN",): "BEGIN",
    ("START", "TRANSACTION"): "START TRANSACTION",
    ("COMMIT",): "COMMIT",
    ("END",): "END",
    ("ROLLBACK",): "ROLLBACK",
    ("ABORT",): "ABORT",
    ("PREPARE", "TRANSACTION"): "PREPARE TRANSACTION",
}
_NON_TX = {
    ("VACUUM",): "VACUUM",
    ("CREATE", "DATABASE"): "CREATE DATABASE",
    ("DROP", "DATABASE"): "DROP DATABASE",
    ("CREATE", "TABLESPACE"): "CREATE TABLESPACE",
    ("DROP", "TABLESPACE"): "DROP TABLESPACE",
    ("ALTER", "SYSTEM"): "ALTER SYSTEM",
}


def lint_migration(text: str) -> list[Finding]:
    """Findings for a migration file: the runner owns the transaction, the file does not."""
    try:
        stmts = split_statements(text)
    except LexError as e:
        return [Finding(e.line, "lex", str(e))]
    out: list[Finding] = []
    for s in stmts:
        if s.kind == "meta":
            out.append(Finding(s.line, "psql-meta",
                               f"psql meta-command {text[s.start:s.end].split()[0]} is not allowed"))
            continue
        words = s.words(text)
        for table, rule in ((_TX_CONTROL, "tx-control"), (_NON_TX, "non-transactional")):
            for key, label in table.items():
                if tuple(words[:len(key)]) == key:
                    out.append(Finding(s.line, rule, f"{label} is not allowed; the runner owns the transaction"
                                       if rule == "tx-control" else
                                       f"{label} cannot run inside a transaction"))
        if words[:1] in (["CREATE"], ["DROP"], ["REINDEX"], ["ALTER"]) and s.has_word(text, "CONCURRENTLY"):
            out.append(Finding(s.line, "non-transactional", "CONCURRENTLY cannot run inside a transaction"))
    return out
