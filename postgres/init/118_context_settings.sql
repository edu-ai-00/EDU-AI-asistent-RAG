-- last change in code by Martin Safranek (2026-08-04).
-- 118_context_settings.sql
-- kolega pro infrastrukturu's resolve_context (origin/main 0704984) reads settings.config->
-- 'context' — follow-up questions are rewritten to stand alone before
-- `prepare`. The code has safe in-code defaults when the section is missing;
-- this init makes the knobs VISIBLE in every settings row (a row is a full
-- copy of the config, not a diff — init 98 pattern), so an organisation can
-- turn the feature off or tune it without touching code.
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.settings
   SET config = jsonb_set(config, '{context}',
       '{"enabled": true, "max_turns": 4, "window": "2 hours",
         "model": "gpt-4.1-mini", "max_tokens": 120}'::jsonb)
 WHERE is_active
   AND NOT (config ? 'context');

COMMIT;
