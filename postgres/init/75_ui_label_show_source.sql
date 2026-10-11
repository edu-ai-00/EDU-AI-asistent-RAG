-- last change in code by Martin Safranek (2026-08-02).
-- 75_ui_label_show_source.sql
-- One label for the collapsible source excerpt under each answer paragraph
-- (answer_parts[].excerpt, migration 74). The test page renders it as a <details>
-- summary, so this is the text the reader clicks to see what the paragraph was
-- written from.
--
-- All four languages the registry currently serves, like every other search_test
-- label. Idempotent; transactional.

BEGIN;

-- ui_label_active is a UNIQUE index with a WHERE clause, which ON CONFLICT cannot
-- name as a conflict target — hence NOT EXISTS instead.
INSERT INTO edu_ai.ui_label (scope, label_key, lang, label_text, notes)
SELECT v.scope, v.label_key, v.lang, v.label_text, v.notes
FROM (VALUES
  ('search_test', 'show_source', 'cs', 'Zobrazit zdrojový text',
   'Summary of the <details> holding answer_parts[].excerpt'),
  ('search_test', 'show_source', 'en', 'Show source text',  NULL),
  ('search_test', 'show_source', 'de', 'Quelltext anzeigen', NULL),
  ('search_test', 'show_source', 'es', 'Mostrar el texto de origen', NULL)
) AS v(scope, label_key, lang, label_text, notes)
WHERE NOT EXISTS (
    SELECT 1 FROM edu_ai.ui_label u
     WHERE u.scope = v.scope AND u.label_key = v.label_key
       AND u.lang = v.lang AND u.is_active);

COMMIT;
