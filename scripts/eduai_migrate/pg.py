"""Reaching PostgreSQL: psql / pg_dump / pg_restore via `docker exec` or local binaries."""
from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
from dataclasses import dataclass


class PgError(Exception):
    def __init__(self, message: str, returncode: int | None = None, output: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.output = output


class SessionDied(PgError):
    """The psql process ended while a statement was pending (error or lost connection)."""


@dataclass(frozen=True)
class Target:
    db: str
    user: str
    container: str | None = None  # None: local psql/pg_dump with the usual PG* environment

    def _cmd(self, tool: str, env: dict[str, str] | None = None, stdin: bool = True):
        env = dict(env or {})
        env.setdefault("PGAPPNAME", "edu_ai_migrate")
        if self.container:
            argv = ["docker", "exec"] + (["-i"] if stdin else [])
            for k, v in env.items():
                argv += ["-e", f"{k}={v}"]
            return argv + [self.container, tool], None
        return [tool], {**os.environ, **env}

    def psql_argv(self, readonly: bool = False):
        env = {"PGOPTIONS": "-c default_transaction_read_only=on"} if readonly else {}
        argv, penv = self._cmd("psql", env)
        return argv + ["-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", self.user, "-d", self.db], penv

    def tool_argv(self, tool: str, *args: str, db: bool = True):
        argv, penv = self._cmd(tool, stdin=(tool == "pg_restore"))
        argv += ["-U", self.user] if tool != "pg_restore" else []
        if db and tool != "pg_restore":
            argv += ["-d", self.db]
        return argv + list(args), penv


def container_env(container: str, name: str) -> str | None:
    p = subprocess.run(["docker", "exec", container, "printenv", name], capture_output=True)
    return p.stdout.decode().strip() if p.returncode == 0 else None


def run_psql(target: Target, sql: str, readonly: bool = False) -> str:
    argv, env = target.psql_argv(readonly)
    p = subprocess.run(argv, input=sql.encode("utf-8"), capture_output=True, env=env)
    out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    if p.returncode != 0:
        raise PgError(f"psql failed (rc={p.returncode}): {err.strip()}", p.returncode, err)
    return out.strip("\n")


def pg_dump_text(target: Target, args: list[str]) -> str:
    argv, env = target.tool_argv("pg_dump", *args)
    p = subprocess.run(argv, capture_output=True, env=env)
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace")
        raise PgError(f"pg_dump failed (rc={p.returncode}): {err.strip()}", p.returncode, err)
    return p.stdout.decode("utf-8")


def pg_dump_file(target: Target, path: str) -> None:
    """Custom-format dump of the whole database into `path`; raises unless pg_dump exits 0."""
    argv, env = target.tool_argv("pg_dump", "-Fc")
    with open(path, "wb") as f:
        p = subprocess.run(argv, stdout=f, stderr=subprocess.PIPE, env=env)
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace")
        raise PgError(f"pg_dump -Fc failed (rc={p.returncode}): {err.strip()}", p.returncode, err)


def pg_restore_list(target: Target, path: str) -> list[str]:
    """TOC of an archive via pg_restore --list; raises unless the archive is readable."""
    argv, env = target.tool_argv("pg_restore", "--list", db=False)
    with open(path, "rb") as f:
        p = subprocess.run(argv, stdin=f, capture_output=True, env=env)
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace")
        raise PgError(f"pg_restore --list failed (rc={p.returncode}): {err.strip()}", p.returncode, err)
    return p.stdout.decode("utf-8", "replace").splitlines()


def tool_version(target: Target, tool: str) -> str:
    argv, env = target._cmd(tool, stdin=False)
    p = subprocess.run(argv + ["--version"], capture_output=True, env=env)
    m = re.search(r"(\d+(?:\.\d+)+)", p.stdout.decode())
    if p.returncode != 0 or not m:
        raise PgError(f"{tool} --version failed")
    return m.group(1)


class Session:
    """One long-lived psql session driven through pipes (holds locks and transactions)."""

    def __init__(self, target: Target, readonly: bool = False):
        argv, env = target.psql_argv(readonly)
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=env)
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._n = 0

    def _pump(self) -> None:
        for raw in iter(self.proc.stdout.readline, b""):
            self._lines.put(raw)
        self._lines.put(None)

    def run(self, sql: str) -> str:
        """Send SQL, return what psql printed up to the end marker, or raise SessionDied."""
        self._n += 1
        mark = f"__EDU_AI_DONE_{self._n}__"
        # the extra ';' ends a final statement written without one (an empty one is a no-op)
        data = (sql.rstrip() + "\n;\n\\echo " + mark + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # psql already exited; the reader below reports what it said
        out: list[str] = []
        while True:
            raw = self._lines.get()
            if raw is None:
                rc = self.proc.wait()
                text = "\n".join(out)
                raise SessionDied(f"psql session ended (rc={rc}): {text.strip()}", rc, text)
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == mark:
                return "\n".join(out)
            out.append(line)

    def kill(self) -> None:
        """Drop the connection without a goodbye (used to simulate a lost connection in tests)."""
        self.proc.kill()
        self.proc.wait()

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc.stdout.close()
