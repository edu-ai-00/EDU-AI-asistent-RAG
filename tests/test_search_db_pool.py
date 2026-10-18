"""Offline: search's connection pool drops connections the server closed (postgres restart) instead of reusing them.

Reproduced 2026-10-18 on a lab VM: after `docker restart edu_ai_postgres` search answered 500 three times in a row
and again 30 s later (OperationalError, then InterfaceError "connection already closed" from a reused connection).
psycopg2 is replaced by a fake here; the live check is a restart on a VM.
"""
import importlib.util
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Error(Exception):
    pass


class OperationalError(Error):
    pass


class FakeConn:
    def __init__(self, dead=False):
        self.closed = 0
        self.dead = dead           # the server side went away
        self.commits = self.rollbacks = 0

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql):
                if conn.dead:
                    conn.closed = 2
                    raise OperationalError("server closed the connection unexpectedly")
        return Cur()

    def commit(self):
        self.commits += 1

    def rollback(self):
        if self.closed:
            raise Error("connection already closed")
        self.rollbacks += 1


class FakePool:
    closed = False

    def __init__(self, idle):
        self.idle = list(idle)
        self.opened = 0
        self.returned, self.discarded = [], []

    def getconn(self):
        if self.idle:
            return self.idle.pop(0)
        self.opened += 1
        return FakeConn()

    def putconn(self, conn, close=False):
        (self.discarded if close else self.returned).append(conn)
        if not close:
            self.idle.append(conn)


def load_db():
    psycopg2 = types.ModuleType("psycopg2")
    psycopg2.Error, psycopg2.OperationalError = Error, OperationalError
    psycopg2.pool = types.SimpleNamespace(ThreadedConnectionPool=object)
    package = types.ModuleType("fakeapp")
    package.__path__ = []
    package.config = types.SimpleNamespace()
    sys.modules.update({"psycopg2": psycopg2, "psycopg2.pool": psycopg2.pool, "fakeapp": package})
    spec = importlib.util.spec_from_file_location("fakeapp.db", ROOT / "search" / "app" / "db.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SearchPool(unittest.TestCase):
    def setUp(self):
        saved = {k: sys.modules.get(k) for k in ("psycopg2", "psycopg2.pool", "fakeapp")}
        self.addCleanup(lambda: [sys.modules.pop(k, None) if v is None else sys.modules.__setitem__(k, v)
                                 for k, v in saved.items()])
        self.db = load_db()
        self.db._server_spoke = lambda conn: conn.dead   # the socket check itself is verified live

    def use(self, p, fail=None):
        self.db.get_pool = lambda: p
        with self.db.get_cursor() as cur:
            cur.execute("select 1")
            if fail:
                raise fail

    def test_dead_pooled_connections_are_discarded_before_use(self):
        dead = [FakeConn(dead=True), FakeConn(dead=True)]
        p = FakePool(dead)
        self.use(p)                                    # no error reaches the caller
        self.assertEqual(dead, p.discarded)
        self.assertEqual(1, p.opened)
        self.assertEqual(1, p.returned[0].commits)

    def test_a_live_connection_is_reused(self):
        live = FakeConn()
        p = FakePool([live])
        self.use(p)
        self.use(p)
        self.assertEqual([live, live], p.returned)
        self.assertEqual(0, p.opened)

    def test_a_connection_that_breaks_during_use_is_not_returned(self):
        conn = FakeConn()
        p = FakePool([conn])
        self.db.get_pool = lambda: p
        with self.assertRaises(OperationalError):
            with self.db.get_cursor() as cur:
                conn.dead = True
                cur.execute("select 1")
        self.assertEqual([conn], p.discarded)
        self.assertEqual([], p.returned)

    def test_an_application_error_rolls_back_and_keeps_the_connection(self):
        conn = FakeConn()
        p = FakePool([conn])
        with self.assertRaises(ValueError):
            self.use(p, fail=ValueError("bad input"))
        self.assertEqual(1, conn.rollbacks)
        self.assertEqual([conn], p.returned)

    def test_a_pool_that_never_yields_a_live_connection_raises(self):
        class Dead(FakePool):
            def getconn(self):
                return FakeConn(dead=True)
        with self.assertRaises(OperationalError):
            self.use(Dead([]))


if __name__ == "__main__":
    unittest.main()
