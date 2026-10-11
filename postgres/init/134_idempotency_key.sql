-- 134: idempotency_key (2026-08-13, Jan).
-- POST /search gets retried after a client timeout, and without this the retry runs
-- the whole cascade again: a second LLM call, a second bill, and possibly a DIFFERENT
-- answer to the identical question. The row remembers the hash of the request body
-- and the response that was already sent, keyed by the client's Idempotency-Key.
--
-- Postgres and not a per-process dict: the service runs `uvicorn --workers 2`, so an
-- in-memory store would miss roughly every other retry (the retry lands on the other
-- worker) and would lose everything on restart. That is not a cache miss, it is a
-- duplicate LLM call, so the cheap option is the wrong one here.
--
-- `account` is legal_entity + user_id. This service has no token to bind identity to
-- (see server _resolve_search_caller), and an allowed anonymous caller
-- (visibility.search_requires_user = false) carries an empty user_id — scoping the
-- key by entity keeps two tenants that pick the same uuid apart.
--
-- response NULL = claimed and still running. A claim older than 5 minutes is treated
-- as abandoned and taken over, so a worker that died mid-request cannot poison a key
-- for the whole retention window. Retention is 24 h, deleted opportunistically by
-- search itself — no cron to forget about.

CREATE TABLE IF NOT EXISTS edu_ai.idempotency_key (
    account     text NOT NULL,
    key         text NOT NULL,
    body_hash   text NOT NULL,
    response    jsonb,
    created_at  timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (account, key)
);

-- the retention sweep orders by age; the PK cannot serve it
CREATE INDEX IF NOT EXISTS idempotency_key_created_at_idx
    ON edu_ai.idempotency_key (created_at);

COMMENT ON TABLE edu_ai.idempotency_key IS
  'POST /search replay cache keyed by (account, Idempotency-Key). response NULL = claimed and still running; a claim older than 5 min is taken over. Retention 24 h, swept by the search service.';

COMMENT ON COLUMN edu_ai.idempotency_key.body_hash IS
  'sha256 of the CANONICAL request body (sorted-key JSON of the parsed model). Same key + different hash = 409; two byte-different bodies that parse identically are deliberately treated as the same request.';
