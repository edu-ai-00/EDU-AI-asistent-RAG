-- last change in code by Martin Safranek (2026-07-22).
-- ─────────────────────────────────────────────────────────────────────────
-- prompt_registry: versioned store for all LLM prompts used by components.
--
-- Components fetch their prompt by a stable TECHNICAL NAME (prompt_key), never
-- by row id. Every change = a NEW row (version+1); the old row is deactivated,
-- so the full history is preserved and a prompt can be rolled back by
-- re-activating an older version.
--
-- Component contract:
--   SELECT prompt_text FROM edu_ai.prompt_registry
--   WHERE prompt_key = %s AND is_active LIMIT 1;
-- On miss, the component falls back to its in-code DEFAULT_*_PROMPT (so a fresh
-- / empty DB still runs). Connection uses the POSTGRES_* env variables.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS edu_ai.prompt_registry (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    prompt_key    text        NOT NULL,                 -- technical name the component queries by
    version       int         NOT NULL,                 -- 1,2,3… per prompt_key
    prompt_text   text        NOT NULL,                 -- template; may contain placeholders {sample_a}…
    is_active     boolean     NOT NULL DEFAULT true,    -- exactly one active version per prompt_key
    component     text,                                 -- component class (reference only, not queried)
    description   text,                                 -- what the prompt does
    placeholders  text[],                               -- documented placeholders (for validation)
    model_hint    text,                                 -- intended model (e.g. gpt-5-mini)
    lang          text        DEFAULT 'cs',
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text,                                 -- who authored the version (left NULL for now)
    notes         text                                  -- changelog: why it changed
);

-- one row per (key, version)
CREATE UNIQUE INDEX IF NOT EXISTS prompt_registry_key_version
    ON edu_ai.prompt_registry (prompt_key, version);

-- exactly ONE active version per prompt_key
CREATE UNIQUE INDEX IF NOT EXISTS prompt_registry_one_active
    ON edu_ai.prompt_registry (prompt_key) WHERE is_active;

-- fast active-lookup (the component hot path)
CREATE INDEX IF NOT EXISTS prompt_registry_active_lookup
    ON edu_ai.prompt_registry (prompt_key) WHERE is_active;
