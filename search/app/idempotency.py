# last change in code by Jan Drozd and Martin Safranek (2026-08-19).
"""Replay cache for POST /v1/ask, keyed by the client's Idempotency-Key.

A client that times out retries the same question. Without this the cascade — and
the answer LLM — runs a second time: paid twice, and free to answer differently the
second time. Table + reasoning: postgres/init/134_idempotency_key.sql.

FAIL OPEN on every database problem. Idempotency is an optimisation; a search that
refuses to answer because the replay cache is unreachable is a worse outcome than a
duplicated call, so each helper swallows its errors and lets the caller proceed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from psycopg2.extras import Json

from . import config
from .db import get_cursor
from .settings import platform_setting

logger = logging.getLogger(__name__)

# A claim this old with no response is treated as abandoned (worker died mid-request)
# and taken over. Below the client timeout it would let a slow request be run twice.
STALE_CLAIM = "5 minutes"
RETENTION = "24 hours"

# Both windows reach SQL as an `interval` LITERAL — psycopg2 cannot parameterize
# the operand of INTERVAL — so a value from settings is only ever accepted in
# this exact shape. It is not defence against a hostile admin (they own the DB
# anyway); it is defence against a typo or a stray quote in a config edit
# silently turning a retention sweep into arbitrary SQL.
_INTERVAL_RE = re.compile(r"^\d{1,4} (?:second|minute|hour|day|week)s?$")


def _window(key: str, default: str) -> str:
    """Platform-wide interval literal for `key`, validated against _INTERVAL_RE."""
    v = platform_setting(key, default)
    if isinstance(v, str) and _INTERVAL_RE.match(v.strip()):
        return v.strip()
    logger.warning("idempotency: %s=%r is not a plain interval like '5 minutes'"
                   " — using %r", key, v, default)
    return default

_TABLE = f"{config.PG_SCHEMA}.idempotency_key"


def account_of(legal_entity: str, user_id: str | None) -> str:
    """Identity the key is scoped to. Entity is part of it so two tenants that pick
    the same user id — or two anonymous callers — cannot read each other's replays."""
    return f"{legal_entity}\x1f{user_id or ''}"


def body_hash(payload: dict) -> str:
    """Hash of the CANONICAL body: same parsed request → same hash, whatever the
    byte-level formatting was. Spec §12.1 asks for byte-for-byte equality; this is
    deliberately more forgiving, because re-serialising a model is not a new request.
    """
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def claim(account: str, key: str, digest: str) -> tuple[str, dict | None]:
    """Try to become the request that computes this key.

    Returns (state, response):
      "new"         — we own it, run the search and call store()
      "replay"      — response is the one already sent
      "conflict"    — same key, different body → the caller must 409
      "in_progress" — someone else is computing it right now
    """
    try:
        with get_cursor() as cur:
            cur.execute(
                f"INSERT INTO {_TABLE} (account, key, body_hash) VALUES (%s,%s,%s) "
                "ON CONFLICT (account, key) DO NOTHING RETURNING 1",
                (account, key, digest))
            if cur.fetchone():
                return "new", None

            cur.execute(
                f"SELECT body_hash, response, created_at < now() - interval"
                f" '{_window('idempotency_stale_claim', STALE_CLAIM)}' "
                f"FROM {_TABLE} WHERE account = %s AND key = %s", (account, key))
            row = cur.fetchone()
            if row is None:            # deleted between the two statements
                return "new", None
            stored_hash, response, is_stale = row

            if stored_hash != digest:
                return "conflict", None
            if response is not None:
                return "replay", response
            if is_stale:
                cur.execute(
                    f"UPDATE {_TABLE} SET created_at = now(), response = NULL "
                    "WHERE account = %s AND key = %s AND response IS NULL",
                    (account, key))
                logger.info("idempotency: took over an abandoned claim")
                return "new", None
            return "in_progress", None
    except Exception as e:
        logger.warning("idempotency: claim failed (%s) — running without it", e)
        return "new", None


def store(account: str, key: str, response: dict) -> None:
    """Record what was sent, so the next retry replays it instead of recomputing."""
    try:
        with get_cursor() as cur:
            cur.execute(
                f"UPDATE {_TABLE} SET response = %s WHERE account = %s AND key = %s",
                (Json(response), account, key))
    except Exception as e:
        logger.warning("idempotency: store failed (%s) — a retry will recompute", e)


def release(account: str, key: str) -> None:
    """Drop a claim whose request never produced a response (the cascade raised).

    Without this the key stays claimed until it goes stale, and the client's retry —
    the very thing it would do after an error — gets 'in_progress' for 5 minutes.
    """
    try:
        with get_cursor() as cur:
            cur.execute(
                f"DELETE FROM {_TABLE} "
                "WHERE account = %s AND key = %s AND response IS NULL",
                (account, key))
    except Exception as e:
        logger.warning("idempotency: release failed (%s)", e)


def purge_expired() -> None:
    """Retention sweep. Runs in the request's background task — an indexed delete,
    and it keeps the table from needing a cron nobody would remember."""
    try:
        with get_cursor() as cur:
            cur.execute(
                f"DELETE FROM {_TABLE} WHERE created_at < now() - interval"
                f" '{_window('idempotency_retention', RETENTION)}'")
            if cur.rowcount:
                logger.info("idempotency: purged %d expired keys", cur.rowcount)
    except Exception as e:
        logger.warning("idempotency: purge failed (%s)", e)
