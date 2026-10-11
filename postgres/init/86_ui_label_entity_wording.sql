-- last change in code by Martin Safranek (2026-08-02).
-- 86_ui_label_entity_wording.sql
-- The UI stops saying "school".
--
-- The platform is meant to serve companies as well — the design conversation used
-- a bank with an insurance arm and a pension fund as the example — and every place
-- that says "škola" is wrong the first time one of those logs in. The database side
-- was already neutral (legal_entity, legal_entity_code, migrations 76-77); this is
-- the visible half catching up.
--
-- Two labels carried it, both from migration 80: the middle visibility option
-- ("pro školu") and the public one ("pro všechny školy"). They are the two places a
-- person actually reads before deciding how far a document reaches, so getting the
-- noun wrong there is not cosmetic — it tells them the wrong thing about who will
-- see it.
--
-- The word chosen is "entita" / "entity", matching legal_entity in the schema, so
-- the UI and the data model use one vocabulary. Where an entity wants its own noun
-- ("pro školu", "pro firmu"), that is what the per-entity ui_label override is for
-- once ui_label carries legal_entity_code — the global default stays neutral.
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.ui_label SET label_text = v.txt
FROM (VALUES
  ('cs', 'pro celou entitu'),
  ('en', 'for the whole entity'),
  ('de', 'für die ganze Einheit'),
  ('es', 'para toda la entidad')
) AS v(lang, txt)
WHERE ui_label.scope = 'upload_test' AND ui_label.label_key = 'visibility_entity'
  AND ui_label.lang = v.lang AND ui_label.is_active;

UPDATE edu_ai.ui_label SET label_text = v.txt
FROM (VALUES
  ('cs', 'veřejný — pro všechny entity'),
  ('en', 'public — every entity'),
  ('de', 'öffentlich — alle Einheiten'),
  ('es', 'público — todas las entidades')
) AS v(lang, txt)
WHERE ui_label.scope = 'upload_test' AND ui_label.label_key = 'visibility_public'
  AND ui_label.lang = v.lang AND ui_label.is_active;

COMMIT;
