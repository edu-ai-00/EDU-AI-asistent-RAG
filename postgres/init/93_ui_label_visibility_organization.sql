-- last change in code by Martin Safranek (2026-08-02).
-- 93_ui_label_visibility_organization.sql
-- The forms catch up with the three levels: `public` is gone (migration 91) and
-- `organization` needs a label. The search page needs an entity and a user picker,
-- because since /search requires legal_entity a page that does not send it gets a
-- 422 on every query — and because who is asking now decides what comes back.
--
-- `visibility_public` is deactivated rather than deleted: ui_label is read WHERE
-- is_active, so an inactive row is invisible to every form while its four
-- translations survive if the level ever returns.
--
-- The wording avoids "škola" — the platform is meant to serve companies too, and
-- the middle option is renamed per entity once ui_label carries legal_entity_code.
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.ui_label SET is_active = false
 WHERE scope = 'upload_test' AND label_key = 'visibility_public';

INSERT INTO edu_ai.ui_label (scope, label_key, lang, label_text, notes)
SELECT v.scope, v.label_key, v.lang, v.label_text, v.notes
FROM (VALUES
  ('upload_test', 'visibility_organization', 'cs', 'pro celou organizaci — všechny jednotky',
   'The whole tree of the legal person, siblings included'),
  ('upload_test', 'visibility_organization', 'en', 'the whole organisation — every unit', NULL),
  ('upload_test', 'visibility_organization', 'de', 'die ganze Organisation — alle Einheiten', NULL),
  ('upload_test', 'visibility_organization', 'es', 'toda la organización — todas las unidades', NULL),

  ('upload_test', 'visibility_entity', 'cs', 'jen pro tuto entitu', NULL),
  ('upload_test', 'visibility_entity', 'en', 'this entity only', NULL),
  ('upload_test', 'visibility_entity', 'de', 'nur diese Einheit', NULL),
  ('upload_test', 'visibility_entity', 'es', 'solo esta entidad', NULL),

  -- the search page: who is asking decides what is found
  ('search_test', 'entity', 'cs', 'Právní entita',  'Sent as legal_entity; required by /search'),
  ('search_test', 'entity', 'en', 'Legal entity',   NULL),
  ('search_test', 'entity', 'de', 'Rechtsträger',   NULL),
  ('search_test', 'entity', 'es', 'Entidad legal',  NULL),

  ('search_test', 'user', 'cs', 'Uživatel', 'Sent as user_id; only affects private documents'),
  ('search_test', 'user', 'en', 'User',     NULL),
  ('search_test', 'user', 'de', 'Benutzer', NULL),
  ('search_test', 'user', 'es', 'Usuario',  NULL),

  ('search_test', 'anonymous', 'cs', '— anonymní (bez privátních) —', NULL),
  ('search_test', 'anonymous', 'en', '— anonymous (no private docs) —', NULL),
  ('search_test', 'anonymous', 'de', '— anonym (keine privaten) —', NULL),
  ('search_test', 'anonymous', 'es', '— anónimo (sin privados) —', NULL),

  ('search_test', 'choose_entity', 'cs', '— vyber entitu —', NULL),
  ('search_test', 'choose_entity', 'en', '— choose an entity —', NULL),
  ('search_test', 'choose_entity', 'de', '— Rechtsträger wählen —', NULL),
  ('search_test', 'choose_entity', 'es', '— elige una entidad —', NULL)
) AS v(scope, label_key, lang, label_text, notes)
WHERE NOT EXISTS (
    SELECT 1 FROM edu_ai.ui_label u
     WHERE u.scope = v.scope AND u.label_key = v.label_key
       AND u.lang = v.lang AND u.is_active);

-- the middle option was worded before `organization` existed beside it
UPDATE edu_ai.ui_label SET label_text = v.txt
FROM (VALUES ('cs','jen pro tuto entitu'), ('en','this entity only'),
             ('de','nur diese Einheit'),   ('es','solo esta entidad')) AS v(lang, txt)
WHERE ui_label.scope='upload_test' AND ui_label.label_key='visibility_entity'
  AND ui_label.lang=v.lang AND ui_label.is_active;

COMMIT;
