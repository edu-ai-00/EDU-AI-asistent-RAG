-- last change in code by Martin Safranek (2026-08-02).
-- 82_ui_label_entity_user.sql
-- Labels for the ingest form's first two choices: the legal entity, then the user.
--
-- The order is not cosmetic. A staff user belongs to exactly one entity and the
-- database refuses a document whose owner comes from another one (the owner
-- foreign key is the pair, not the id), so the user list cannot be built before
-- the entity is known. The form says so with choose_entity_first rather than
-- offering a list that would be rejected.
--
-- Idempotent; transactional.

BEGIN;

INSERT INTO edu_ai.ui_label (scope, label_key, lang, label_text, notes)
SELECT v.scope, v.label_key, v.lang, v.label_text, v.notes
FROM (VALUES
  ('upload_test', 'entity', 'cs', 'Právní entita',
   'Not "škola": the platform is meant to serve companies too'),
  ('upload_test', 'entity', 'en', 'Legal entity',   NULL),
  ('upload_test', 'entity', 'de', 'Rechtsträger',   NULL),
  ('upload_test', 'entity', 'es', 'Entidad legal',  NULL),

  ('upload_test', 'choose_entity', 'cs', '— vyber entitu —', NULL),
  ('upload_test', 'choose_entity', 'en', '— choose an entity —', NULL),
  ('upload_test', 'choose_entity', 'de', '— Rechtsträger wählen —', NULL),
  ('upload_test', 'choose_entity', 'es', '— elige una entidad —', NULL),

  ('upload_test', 'choose_entity_first', 'cs', '— nejdřív vyber entitu —', NULL),
  ('upload_test', 'choose_entity_first', 'en', '— choose an entity first —', NULL),
  ('upload_test', 'choose_entity_first', 'de', '— zuerst Rechtsträger wählen —', NULL),
  ('upload_test', 'choose_entity_first', 'es', '— elige primero una entidad —', NULL),

  ('upload_test', 'no_users', 'cs', '— entita nemá aktivní uživatele —', NULL),
  ('upload_test', 'no_users', 'en', '— no active users for this entity —', NULL),
  ('upload_test', 'no_users', 'de', '— keine aktiven Benutzer —', NULL),
  ('upload_test', 'no_users', 'es', '— sin usuarios activos —', NULL),

  ('upload_test', 'choose_user', 'cs', 'Vyber uživatele, který dokument nahrává.', NULL),
  ('upload_test', 'choose_user', 'en', 'Choose the user ingesting this document.', NULL),
  ('upload_test', 'choose_user', 'de', 'Wähle den einspeisenden Benutzer.', NULL),
  ('upload_test', 'choose_user', 'es', 'Elige el usuario que sube el documento.', NULL)
) AS v(scope, label_key, lang, label_text, notes)
WHERE NOT EXISTS (
    SELECT 1 FROM edu_ai.ui_label u
     WHERE u.scope = v.scope AND u.label_key = v.label_key
       AND u.lang = v.lang AND u.is_active);

COMMIT;
