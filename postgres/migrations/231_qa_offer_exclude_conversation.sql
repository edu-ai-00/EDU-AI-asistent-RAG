-- 231: conversational QA entries are never offered as a chip (offer_exclude_categories).
--
-- A school may curate small talk ("Ahoj", "Děkuji", "Nashledanou") so that it gets an
-- answer. Those entries are short and share letters and words with everything, so they
-- turned up among the offered questions of real ones: measured on a school's packs with 22
-- such entries (regression set, 13 Nov 2026), a conversational chip in about one of twelve
-- real questions, mostly the n-gram chip of a polite question ("Dobrý den, mohla bych se
-- zeptat ..." -> "Ahoj"). The capabilities answer ("Co umíš?") lists the SHORTEST curated
-- questions as topics, so it would have listed "Ahoj" and "Pardon" first.
--
-- This migration
--   1. adds the global QA category CONVERSATION (code_list 'qa_category'), which a curator
--      sets on such entries (QA pack CSV column category_code, or the portal);
--   2. sets the step parameter `offer_exclude_categories: ["CONVERSATION"]` on qa_lexical,
--      qa_exact, qa_vector and route of every ACTIVE setting that does not have it yet.
-- An entry of the category may still be the confident answer (exact or lexical match); it
-- is left out of the "did you mean" list, the dead-end fallback, the related chips of an
-- answer, the n-gram chip, the other-ročník offer and the topics of the capabilities
-- answer. A step without the parameter behaves as before (search/app/nodes/qa_common.py
-- offer_codes / no_offer_ids). Without entries of the category nothing changes.
--
-- Undo: remove offer_exclude_categories from the step params; the category may stay.
--
-- Idempotent. No transaction control (runner, from 217).

INSERT INTO edu_ai.code_list (list_code, legal_entity_code, code, labels, sort_order,
                              is_active, created_by, notes)
SELECT 'qa_category', NULL, 'CONVERSATION',
       '{"cs": "Konverzace", "en": "Small talk", "de": "Small Talk", "es": "Conversación"}'::jsonb,
       COALESCE((SELECT max(sort_order) FROM edu_ai.code_list WHERE list_code = 'qa_category'), 0) + 10,
       true, '231', '231: small talk; never offered as a chip (offer_exclude_categories)'
 WHERE NOT EXISTS (SELECT 1 FROM edu_ai.code_list
                    WHERE list_code = 'qa_category' AND code = 'CONVERSATION');

UPDATE edu_ai.settings s
   SET config = jsonb_set(
         s.config, '{pipeline}',
         (SELECT jsonb_agg(
                   CASE WHEN st->>'step' IN ('qa_lexical', 'qa_exact', 'qa_vector', 'route')
                             AND jsonb_typeof(st->'params') = 'object'
                             AND NOT (st->'params' ? 'offer_exclude_categories')
                        THEN jsonb_set(st, '{params,offer_exclude_categories}',
                                       '["CONVERSATION"]'::jsonb, true)
                        ELSE st END
                   ORDER BY ord)
            FROM jsonb_array_elements(s.config->'pipeline') WITH ORDINALITY AS t(st, ord)))
 WHERE s.is_active
   AND jsonb_typeof(s.config->'pipeline') = 'array'
   AND EXISTS (SELECT 1 FROM jsonb_array_elements(s.config->'pipeline') st
                WHERE st->>'step' IN ('qa_lexical', 'qa_exact', 'qa_vector', 'route')
                  AND jsonb_typeof(st->'params') = 'object'
                  AND NOT (st->'params' ? 'offer_exclude_categories'));

-- self-check: the category exists (an operator may switch it off); every active step of
-- the four has the key
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM edu_ai.code_list
                  WHERE list_code = 'qa_category' AND code = 'CONVERSATION') THEN
    RAISE EXCEPTION '231: qa_category CONVERSATION is missing';
  END IF;
  IF EXISTS (SELECT 1 FROM edu_ai.settings s, jsonb_array_elements(s.config->'pipeline') st
              WHERE s.is_active
                AND st->>'step' IN ('qa_lexical', 'qa_exact', 'qa_vector', 'route')
                AND jsonb_typeof(st->'params') = 'object'
                AND NOT (st->'params' ? 'offer_exclude_categories')) THEN
    RAISE EXCEPTION '231: an active step has no offer_exclude_categories';
  END IF;
END $$;
