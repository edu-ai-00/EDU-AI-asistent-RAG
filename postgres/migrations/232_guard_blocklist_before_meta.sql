-- 232: the blocklist (self-harm, profanity, hate, ...) decides before the meta answer.
--
-- `blocklist_before_meta` is a parameter of the route step read by search/app/nodes/route.py
-- (_guard; off when absent). The meta intent guard answers any short sentence with an
-- ability verb ("umíš", "jsi") and no school topic with the capabilities message, and it
-- ran first: "umíš mi pomoct? chci se zabít" and "jsi k ničemu, chci umřít" got "Jsem
-- asistent školy ..." instead of the crisis line, "jsi kokot" instead of the refusal.
-- With the parameter a blocklist hit decides first; a clean meta or identity question
-- still gets its answer, and soft (below threshold) hits travel with it in the trace.
--
-- Measured before release: the guard outcome of every query of the regression sets, of
-- the production query log (Aug-Oct 2026) and of a sample of conversational messages from
-- a children's chat, with and without the parameter; see the commit message.
--
-- Settings are per organisation: every ACTIVE row with a route step gets the value. A step
-- that already has blocklist_before_meta is left alone.
-- Undo: remove blocklist_before_meta from the route params (or set it to false).
--
-- Idempotent. No transaction control (runner, from 217).

UPDATE edu_ai.settings s
   SET config = jsonb_set(
         s.config, '{pipeline}',
         (SELECT jsonb_agg(
                   CASE WHEN st->>'step' = 'route'
                             AND jsonb_typeof(st->'params') = 'object'
                             AND NOT (st->'params' ? 'blocklist_before_meta')
                        THEN jsonb_set(st, '{params,blocklist_before_meta}', 'true'::jsonb, true)
                        ELSE st END
                   ORDER BY ord)
            FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY AS t(st, ord)))
 WHERE s.is_active
   AND jsonb_typeof(s.config->'pipeline') = 'array'
   AND EXISTS (SELECT 1 FROM jsonb_array_elements(s.config->'pipeline') st
                WHERE st->>'step' = 'route'
                  AND jsonb_typeof(st->'params') = 'object'
                  AND NOT (st->'params' ? 'blocklist_before_meta'));

-- self-check: every active route step carries blocklist_before_meta
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM edu_ai.settings s, jsonb_array_elements(s.config->'pipeline') st
              WHERE s.is_active AND st->>'step' = 'route'
                AND jsonb_typeof(st->'params') = 'object'
                AND NOT (st->'params' ? 'blocklist_before_meta')) THEN
    RAISE EXCEPTION '232: an active route step has no blocklist_before_meta';
  END IF;
END $$;
