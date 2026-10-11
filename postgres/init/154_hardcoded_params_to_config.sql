-- last change in code by Martin Safranek (2026-08-20).
--
-- 154 — the values that could only be changed by editing code become config.
--
-- Two homes, deliberately not one:
--
--   * config.search on the GLOBAL row (legal_entity_code IS NULL) — read by
--     settings.platform_setting(), so a school row cannot override it. For
--     things one school must not be able to change for everyone: the accepted
--     ročník range (API input validation), the idempotency windows (operations),
--     and the reranker's max_length — that model is a process-lifetime
--     singleton, so a per-school value would be decided by whichever school
--     triggered the load first.
--
--   * pipeline[step].params on EVERY active row — tunable per school:
--     answer.temperature, answer.chars_per_token, answer.excerpt_cut_ratio,
--     and the guard policy on `route`.
--
-- Every value below equals what the code did before, so this migration changes
-- no behaviour. It only makes the values visible and editable. Steps are matched
-- BY NAME: the pipeline order differs between rows, and `route` carries
-- params: null today, which jsonb_set cannot extend without the CASE below.
--
-- NOT moved here, on purpose:
--   settings._CACHE_TTL_S  — it is the TTL of the cache that loads this config;
--                            sourcing it from the config it caches is circular.
--   prepare._GRADE_TTL_S   — same class: operational, not search behaviour.
--   server._RETRYABLE      — read inside the HTTP error handler, i.e. exactly
--                            when the DB is most likely to be the thing that
--                            failed; a settings read there would spam the log
--                            during an outage. It is an API contract, not a knob.
--   the IDF "+1" smoothing — a formula, not a policy. In the config someone can
--                            change it with nothing to reason about; scoring
--                            would shift with no way to tell better from worse.

BEGIN;

-- ── 0) snapshot of the pipeline lengths, so step 3 can prove none was lost ───
CREATE TEMP TABLE _len_before ON COMMIT DROP AS
SELECT setting_id, jsonb_array_length(config::jsonb -> 'pipeline') AS n
  FROM edu_ai.settings
 WHERE is_active AND jsonb_typeof(config::jsonb -> 'pipeline') = 'array';

-- ── 1) platform-wide values (global row only) ───────────────────────────────
UPDATE edu_ai.settings
   SET config = (
         jsonb_set(config::jsonb, '{search}',
           CASE WHEN jsonb_typeof(config::jsonb -> 'search') = 'object'
                THEN config::jsonb -> 'search' ELSE '{}'::jsonb END
           || jsonb_build_object(
                'grade_min', 1,
                'grade_max', 13,
                'idempotency_stale_claim', '5 minutes',
                'idempotency_retention', '24 hours',
                'rerank_max_length', 512))
       )::json
 WHERE is_active AND legal_entity_code IS NULL;

-- ── 2) per-step params on every active row, matched by step name ────────────
--
-- The merge is `existing || new`, so a value already tuned on a school row
-- survives; only missing keys are filled in.
WITH new_params(step, params) AS (
    VALUES
      ('answer', jsonb_build_object(
          'temperature', 0.1,
          'chars_per_token', 4,
          'excerpt_cut_ratio', 0.6)),
      ('route', jsonb_build_object(
          -- the shipped policy, written out so it can be changed without a rebuild
          'guard_category_order',
              jsonb_build_array('self_harm', 'hate', 'sexual', 'violence', 'profanity'),
          -- severity floor per category; blocklist severity is NOT NULL DEFAULT 1,
          -- so 1 admits every row = today's "any hit blocks"
          'guard_block_min_severity', jsonb_build_object(
              'self_harm', 1, 'hate', 1, 'sexual', 1, 'violence', 1, 'profanity', 1),
          'guard_message_key', jsonb_build_object('self_harm', 'guard_self_harm'),
          'guard_outcome', jsonb_build_object('self_harm', 'self_harm'),
          'phrase_slack', 3))
)
UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config::jsonb, '{pipeline}', (
         SELECT jsonb_agg(
                  CASE WHEN np.params IS NULL THEN e.elem
                       ELSE jsonb_set(e.elem, '{params}',
                              CASE WHEN jsonb_typeof(e.elem -> 'params') = 'object'
                                   THEN e.elem -> 'params' ELSE '{}'::jsonb END
                              || np.params)
                  END
                  ORDER BY e.ord)
           FROM jsonb_array_elements(s.config::jsonb -> 'pipeline')
                WITH ORDINALITY AS e(elem, ord)
           LEFT JOIN new_params np ON np.step = e.elem ->> 'step'
       ))::json
 WHERE s.is_active
   AND jsonb_typeof(s.config::jsonb -> 'pipeline') = 'array'
   -- jsonb_agg over an empty array yields NULL, and jsonb_set(x, path, NULL) is
   -- NULL — that would blank the whole config instead of doing nothing.
   AND jsonb_array_length(s.config::jsonb -> 'pipeline') > 0;

-- ── 3) invariants — fail loudly rather than half-applied ────────────────────
DO $$
DECLARE
    n_missing int;
    n_rows    int;
BEGIN
    -- the platform values landed on the global row
    SELECT count(*) INTO n_missing
      FROM edu_ai.settings
     WHERE is_active AND legal_entity_code IS NULL
       AND NOT (config::jsonb -> 'search' ?& array['grade_min', 'grade_max',
                'idempotency_stale_claim', 'idempotency_retention',
                'rerank_max_length']);
    IF n_missing > 0 THEN
        RAISE EXCEPTION '154: platform values missing on the global row';
    END IF;

    -- every active row carrying an `answer` step got the three params
    SELECT count(*) INTO n_missing
      FROM edu_ai.settings s,
           jsonb_array_elements(s.config::jsonb -> 'pipeline') e
     WHERE s.is_active AND e ->> 'step' = 'answer'
       AND NOT (e -> 'params' ?& array['temperature', 'chars_per_token',
                                       'excerpt_cut_ratio']);
    IF n_missing > 0 THEN
        RAISE EXCEPTION '154: answer params missing on % row(s)', n_missing;
    END IF;

    -- and every `route` step got a params object where there was null
    SELECT count(*) INTO n_missing
      FROM edu_ai.settings s,
           jsonb_array_elements(s.config::jsonb -> 'pipeline') e
     WHERE s.is_active AND e ->> 'step' = 'route'
       AND NOT (e -> 'params' ?& array['guard_category_order',
                'guard_block_min_severity', 'guard_message_key',
                'guard_outcome', 'phrase_slack']);
    IF n_missing > 0 THEN
        RAISE EXCEPTION '154: route guard policy missing on % row(s)', n_missing;
    END IF;

    -- no step lost or gained, and no config blanked
    SELECT count(*) INTO n_missing
      FROM _len_before b
      JOIN edu_ai.settings s USING (setting_id)
     WHERE s.config IS NULL
        OR jsonb_array_length(s.config::jsonb -> 'pipeline') <> b.n;
    IF n_missing > 0 THEN
        RAISE EXCEPTION '154: pipeline length changed on % row(s)', n_missing;
    END IF;

    SELECT count(*) INTO n_rows FROM edu_ai.settings WHERE is_active;
    RAISE NOTICE '154: applied to % active settings row(s)', n_rows;
END $$;

COMMIT;
