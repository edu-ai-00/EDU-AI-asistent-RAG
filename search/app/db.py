# last change in code by Martin Safranek (2026-05-27).
"""Thin Postgres helper — connection pool + query runner."""

import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
from . import config

_pool: pool.ThreadedConnectionPool | None = None


def get_pool() -> pool.ThreadedConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            host=config.PG_HOST,
            port=config.PG_PORT,
            user=config.PG_USER,
            password=config.PG_PASSWORD,
            dbname=config.PG_DATABASE,
        )
    return _pool


@contextmanager
def get_cursor():
    """Yield a cursor from the pool; auto-return connection on exit."""
    p = get_pool()
    conn = p.getconn()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        p.putconn(conn)
