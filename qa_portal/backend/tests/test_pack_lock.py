"""Race-safety of PackLock (CONTRACT.md §3 "Vlastnictví zámku" / "Souběžné
převzetí"): a stale reclaim must never let two instances hold the same lock,
and only the token-matching owner may ever remove its own lock file.
"""
from __future__ import annotations

import json
import os
import threading
import time

from app.state.lock import PackLock, PublishInProgress


def _make(tmp_path, ttl_seconds=1):
    return PackLock(tmp_path, "ZS_DEMO", "faq", ttl_seconds)


def _rewrite_acquired_at(lock: PackLock, when: float) -> None:
    """Force staleness without sleeping: rewrite the lock's own payload,
    keeping its token intact so ownership checks stay meaningful."""
    data = json.loads(lock._path.read_text(encoding="utf-8"))
    data["acquired_at"] = when
    lock._path.write_text(json.dumps(data), encoding="utf-8")


def test_stale_reclaim_has_exactly_one_winner(tmp_path):
    seed = _make(tmp_path, ttl_seconds=0)
    seed.acquire()
    _rewrite_acquired_at(seed, time.time() - 10)  # force staleness

    # Racers use a real ttl: once one wins, its fresh lock must stay non-stale
    # for the (sub-millisecond) remainder of the race so the rest see it as held.
    N = 8
    locks = [_make(tmp_path) for _ in range(N)]
    results: list[object] = [None] * N
    barrier = threading.Barrier(N)

    def worker(i):
        barrier.wait()
        try:
            locks[i].acquire()
            results[i] = "ok"
        except PublishInProgress:
            results[i] = "busy"
        except Exception as e:  # pragma: no cover - guarded by assertion below
            results[i] = e

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r == "ok"]
    losers = [r for r in results if r == "busy"]
    others = [r for r in results if r not in ("ok", "busy")]
    assert others == []
    assert len(winners) == 1
    assert len(losers) == N - 1

    winner = locks[results.index("ok")]
    winner.release()


def test_non_stale_conflict_raises_publish_in_progress(tmp_path):
    holder = _make(tmp_path, ttl_seconds=60)
    holder.acquire()

    challenger = _make(tmp_path, ttl_seconds=60)
    try:
        challenger.acquire()
        assert False, "expected PublishInProgress"
    except PublishInProgress:
        pass

    holder.release()


def test_ttl_takeover_makes_old_holder_release_a_noop(tmp_path):
    old = _make(tmp_path, ttl_seconds=0)
    old.acquire()
    _rewrite_acquired_at(old, time.time() - 10)  # old holder is now stale

    new = _make(tmp_path, ttl_seconds=0)
    new.acquire()  # takes over: reclaims the stale file, writes its own token

    lock_path = new._path
    before = json.loads(lock_path.read_text(encoding="utf-8"))
    assert before["token"] == new._token

    old.release()  # must be a no-op: old no longer owns what's on disk

    assert lock_path.exists()
    after = json.loads(lock_path.read_text(encoding="utf-8"))
    assert after["token"] == new._token

    new.release()
    assert not lock_path.exists()


def test_toctou_guard_serialises_reclaim_against_a_concurrent_racer(tmp_path):
    """F1: forces B to pause mid-reclaim (already holding the per-path
    in-process guard) and only lets racer C attempt acquire() once B is
    inside that pause. Whether C then blocks on the guard or finds B's
    finished fresh lock, C must never rename away B's lock: it must observe
    a non-stale, token-owned-by-B file and lose. This replaces reliance on
    lucky OS scheduling with an explicit, deterministic interleaving.
    """
    seed = _make(tmp_path, ttl_seconds=0)
    seed.acquire()
    _rewrite_acquired_at(seed, time.time() - 10)  # force staleness

    b = _make(tmp_path, ttl_seconds=1)
    c = _make(tmp_path, ttl_seconds=1)

    b_in_reclaim = threading.Event()
    c_may_proceed = threading.Event()
    original_reclaim = PackLock._reclaim_if_stale_or_raise

    def b_reclaim(self):
        b_in_reclaim.set()
        c_may_proceed.wait(timeout=2)
        return original_reclaim(self)

    b._reclaim_if_stale_or_raise = b_reclaim.__get__(b)  # patch only B's instance

    results: dict[str, str] = {}

    def run_b():
        b.acquire()
        results["b"] = "ok"

    def run_c():
        b_in_reclaim.wait(timeout=2)  # only start once B is paused in reclaim
        c_may_proceed.set()
        try:
            c.acquire()
            results["c"] = "ok"
        except PublishInProgress:
            results["c"] = "busy"

    t_b = threading.Thread(target=run_b)
    t_c = threading.Thread(target=run_c)
    t_b.start()
    t_c.start()
    t_b.join(timeout=5)
    t_c.join(timeout=5)

    assert results.get("b") == "ok"
    assert results.get("c") == "busy"

    # B's fresh lock must be untouched: still on disk, still B's own token.
    on_disk = json.loads(b._path.read_text(encoding="utf-8"))
    assert on_disk["token"] == b._token

    b.release()


def test_persistent_rename_failure_is_bounded_not_a_spin(tmp_path, monkeypatch):
    """F2: a rename that keeps failing for a reason OTHER than a race loss
    (e.g. Windows PermissionError while something else holds the file open)
    must not be swallowed as "someone else won" and looped on forever."""
    seed = _make(tmp_path, ttl_seconds=0)
    seed.acquire()
    _rewrite_acquired_at(seed, time.time() - 10)  # stale, so acquire() reclaims

    calls = {"n": 0}

    def failing_rename(src, dst):
        calls["n"] += 1
        raise PermissionError("simulated: file in use")

    monkeypatch.setattr(os, "rename", failing_rename)

    victim = _make(tmp_path, ttl_seconds=1)
    start = time.monotonic()
    try:
        victim.acquire()
        assert False, "expected PublishInProgress"
    except PublishInProgress:
        pass
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"acquire() spun instead of bounding out ({elapsed}s)"
    assert 0 < calls["n"] <= 5
