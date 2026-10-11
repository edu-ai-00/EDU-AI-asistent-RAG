"""Per-(school, pack) exclusive lock, held across check -> write -> verify.

CONTRACT.md §3: the lock lives in qa_admin_state as a file created with an
exclusive open (O_CREAT|O_EXCL) plus a PID/timestamp, with a TTL so a crashed
holder cannot block a school's pack forever. Every lock also carries a unique
owner token; only the instance that wrote a given token may remove it, so a
TTL takeover can never be undone by the previous holder's later release().

KNOWN LIMITATION, documented rather than hidden: this lock is IN-PROCESS-FILE
based and correct only for a SINGLE instance of edu_ai_qa_admin. Running more
than one replica would require a shared lock (e.g. a Postgres advisory lock
via the search service) — out of scope for the MVP (CONTRACT.md §3, §11).
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

# Per-lock-path in-process mutex: the documented scope is a single instance,
# so within that instance two threads must never both pass the staleness
# check for the same path (that was the TOCTOU: read-judge-rename was not
# atomic against a second thread doing the same). Keyed by resolved path,
# guarded by one module-level mutex against concurrent first-touch creation.
_registry_lock = threading.Lock()
_path_guards: dict[str, threading.Lock] = {}


def _guard_for(path: Path) -> threading.Lock:
    key = str(path)
    with _registry_lock:
        guard = _path_guards.get(key)
        if guard is None:
            guard = threading.Lock()
            _path_guards[key] = guard
        return guard


class PublishInProgress(Exception):
    """Another operation already holds the lock for this (school, pack)."""


class PackLock:
    def __init__(self, state_dir: str | Path, school: str, pack: str, ttl_seconds: int):
        self._dir = Path(state_dir) / "locks"
        self._dir.mkdir(parents=True, exist_ok=True)
        # school/pack are already policy- and pattern-validated by the caller;
        # still replace path separators defensively before touching the filesystem.
        safe_school = school.replace("/", "_").replace("\\", "_")
        safe_pack = pack.replace("/", "_").replace("\\", "_")
        self._path = self._dir / f"{safe_school}__{safe_pack}.lock"
        self._ttl = ttl_seconds
        self._acquired = False
        # Unique per instance: proves ownership across a TTL takeover.
        self._token = uuid.uuid4().hex

    def acquire(self) -> None:
        # Hold the per-path guard across the WHOLE check/reclaim/create
        # section: this is what makes staleness-judgement and the removal of
        # the judged file a single atomic step from every other thread's view.
        with _guard_for(self._path):
            self._acquire_locked()

    def _acquire_locked(self) -> None:
        # Bounded, not `while True`: a rename that keeps failing for a reason
        # other than "a racer already moved it" must not spin a thread forever.
        max_attempts = 5
        for _ in range(max_attempts):
            payload = json.dumps({
                "pid": os.getpid(), "acquired_at": time.time(), "token": self._token,
            }).encode()
            try:
                fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                # Either outcome (we cleared it, or someone else did first)
                # means: re-read the path from scratch, never assume ownership.
                self._reclaim_if_stale_or_raise()
                continue
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            self._acquired = True
            return
        raise PublishInProgress()

    def _reclaim_if_stale_or_raise(self) -> None:
        """Judge the file currently at `path` and, if stale, remove exactly
        that file via an atomic rename-away (never a blind unlink). Whether
        we performed the removal or another racer beat us to it, the caller
        just retries the exclusive create against whatever is there now.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
            age = time.time() - float(json.loads(raw).get("acquired_at", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            # Includes the file having vanished already: the rename below
            # then fails too, so we correctly report "not our removal".
            raw = None
            age = self._ttl + 1
        if age <= self._ttl:
            raise PublishInProgress()
        # Rename atomically moves whatever is CURRENTLY at `path`. If another
        # racer already renamed it away, this raises and we lost the race —
        # never a second unlink of a lock someone else already claimed.
        tmp = self._path.with_name(self._path.name + f".stale-{uuid.uuid4().hex}")
        try:
            os.rename(self._path, tmp)
        except FileNotFoundError:
            return  # another racer already moved it away; retry the create
        except OSError:
            # Not a race loss (nothing moved) — a real failure (e.g. Windows
            # PermissionError while something else holds the file open).
            # Treat as busy rather than looping on a rename that will keep failing.
            raise PublishInProgress()
        # Belt and braces: confirm we moved the SAME payload we judged stale,
        # not a fresh lock written between our read and our rename.
        try:
            moved = tmp.read_text(encoding="utf-8")
        except OSError:
            moved = None
        if raw is not None and moved != raw:
            os.rename(tmp, self._path)  # put back what we didn't actually judge
            raise PublishInProgress()
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass

    def release(self) -> None:
        if not self._acquired:
            return
        self._acquired = False
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return  # gone or unreadable: nothing of ours left to remove.
        if data.get("token") != self._token:
            return  # a later holder's lock now sits at this path — leave it.
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "PackLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()
