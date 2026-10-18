# last change in code by Martin Safranek (2026-05-27).
"""Thin Postgres helper — connection pool + query runner."""

import select

import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
from . import config

_pool: pool.ThreadedConnectionPool | None = None
_MAXCONN = 10


def get_pool() -> pool.ThreadedConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=_MAXCONN,
            host=config.PG_HOST,
            port=config.PG_PORT,
            user=config.PG_USER,
            password=config.PG_PASSWORD,
            dbname=config.PG_DATABASE,
        )
    return _pool


def _server_spoke(conn) -> bool:
    """An idle connection's socket is readable only when the server sent something unasked:
    its termination notice or EOF after a restart. No round trip. (poll() does not notice it;
    both checked on a lab VM, 2026-10-18.)"""
    return bool(select.select([conn.fileno()], [], [], 0)[0])


def _alive(conn) -> bool:
    """False for a pooled connection the server has dropped (e.g. postgres restarted)."""
    if conn.closed:
        return False
    try:
        return not _server_spoke(conn)
    except (OSError, ValueError, psycopg2.Error):
        return False


def _borrow(p: pool.ThreadedConnectionPool):
    # After a restart every pooled connection may be dead; the pool opens new ones as needed.
    for _ in range(_MAXCONN + 1):
        conn = p.getconn()
        if _alive(conn):
            return conn
        p.putconn(conn, close=True)
    raise psycopg2.OperationalError("no live database connection in the pool")


@contextmanager
def get_cursor():
    """Yield a cursor from the pool; auto-return connection on exit (a broken one is discarded)."""
    p = get_pool()
    conn = _borrow(p)
    broken = False
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except psycopg2.Error:
            broken = True
        raise
    finally:
        p.putconn(conn, close=broken or bool(conn.closed))
