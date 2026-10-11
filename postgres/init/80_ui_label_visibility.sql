-- last change in code by Martin Safranek (2026-08-02).
-- 80_ui_label_visibility.sql
-- The ingest form's visibility choice grows a third option, and one of the two
-- existing labels now means something else.
--
-- The three VALUES stay in a CHECK constraint (migration 78), not in a code list:
-- each is a branch of the retrieval predicate, so a fourth row would be a value no
-- query understands and documents carrying it would reach nobody. A code list
-- promises "add a row and it works", which here would be false.
--
-- The LABELS belong in ui_label, and they need to be data for a reason the values
-- do not: "pro školu" is wrong the first time a company uses this. Once ui_label
-- carries legal_entity_code (the per-key override group in spec.multi-entity), an
-- entity renames it to "pro firmu" without anyone touching code. That column does
-- not exist yet, so today these are global.
--
-- ── the wording of 'public' had to change ───────────────────────────────────
-- The page has offered "soukromý / veřejný" against private_public_flag since the
-- beginning, in a world with one school, where "veřejný" meant "everyone here".
-- With entities it means every entity on the platform — the school act, a public
-- health notice. A label that says only "veřejný" now understates what the person
-- is agreeing to, so both existing labels are restated and the new middle option
-- takes over the old meaning.
--
-- Lowercase, because these are options in a select and the existing two are.
--
-- The form itself keeps two options until the gateway accepts the third: /v1/ingest
-- validates private_public_flag against {private, public} and would reject
-- 'entity'. Labels first, form and API together.
--
-- Idempotent; transactional.

BEGIN;

-- an earlier pass added a heading key that duplicates the existing 'visibility'
DELETE FROM edu_ai.ui_label
 WHERE scope = 'upload_test' AND label_key = 'visibility_label';

-- restate the two that exist: what "public" reaches is no longer obvious
UPDATE edu_ai.ui_label SET label_text = v.txt
FROM (VALUES
  ('cs', 'soukromý — jen pro mě'),
  ('en', 'private — only me'),
  ('de', 'privat — nur ich'),
  ('es', 'privado — solo yo')
) AS v(lang, txt)
WHERE ui_label.scope = 'upload_test' AND ui_label.label_key = 'visibility_private'
  AND ui_label.lang = v.lang AND ui_label.is_active;

UPDATE edu_ai.ui_label SET label_text = v.txt
FROM (VALUES
  ('cs', 'veřejný — pro všechny školy'),
  ('en', 'public — every school'),
  ('de', 'öffentlich — alle Schulen'),
  ('es', 'público — todos los centros')
) AS v(lang, txt)
WHERE ui_label.scope = 'upload_test' AND ui_label.label_key = 'visibility_public'
  AND ui_label.lang = v.lang AND ui_label.is_active;

-- an earlier pass inserted this key capitalised; normalise it so the file gives
-- the same result whether the row is new or already there
UPDATE edu_ai.ui_label SET label_text = v.txt
FROM (VALUES
  ('cs', 'pro školu'),
  ('en', 'for the whole school'),
  ('de', 'für die ganze Schule'),
  ('es', 'para todo el centro')
) AS v(lang, txt)
WHERE ui_label.scope = 'upload_test' AND ui_label.label_key = 'visibility_entity'
  AND ui_label.lang = v.lang AND ui_label.is_active;

-- the new middle option, which carries what "veřejný" used to mean
INSERT INTO edu_ai.ui_label (scope, label_key, lang, label_text, notes)
SELECT v.scope, v.label_key, v.lang, v.label_text, v.notes
FROM (VALUES
  ('upload_test', 'visibility_entity', 'cs', 'pro školu',
   'Renamed per entity once ui_label carries legal_entity_code — a company says "pro firmu"'),
  ('upload_test', 'visibility_entity', 'en', 'for the whole school',  NULL),
  ('upload_test', 'visibility_entity', 'de', 'für die ganze Schule',  NULL),
  ('upload_test', 'visibility_entity', 'es', 'para todo el centro',   NULL)
) AS v(scope, label_key, lang, label_text, notes)
WHERE NOT EXISTS (
    SELECT 1 FROM edu_ai.ui_label u
     WHERE u.scope = v.scope AND u.label_key = v.label_key
       AND u.lang = v.lang AND u.is_active);

COMMIT;
