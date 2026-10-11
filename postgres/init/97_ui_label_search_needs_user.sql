-- last change in code by Martin Safranek (2026-08-02).
-- 97_ui_label_search_needs_user.sql
-- The search test page loses its "anonymous" user option.
--
-- /search requires a verified user_id: without it the entity in the request is
-- an unbacked claim (anyone could read another school's documents by naming it),
-- so an anonymous search now gets 403 — offering the option would only offer a
-- guaranteed error. The page gets the same pair of labels the upload page
-- already has: a "choose a user" placeholder and a "no active users" notice.
-- The old `anonymous` rows are deactivated, not deleted — the číselník keeps
-- its history like every other one.

BEGIN;

UPDATE edu_ai.ui_label SET is_active = false
 WHERE scope = 'search_test' AND label_key = 'anonymous' AND is_active;

INSERT INTO edu_ai.ui_label (scope, label_key, lang, label_text, notes)
SELECT * FROM (VALUES
    ('search_test', 'choose_user', 'cs', '— vyber uživatele —',
     'search requires a verified user (init 97)'),
    ('search_test', 'choose_user', 'en', '— choose a user —',
     'search requires a verified user (init 97)'),
    ('search_test', 'choose_user', 'de', '— Benutzer wählen —',
     'search requires a verified user (init 97)'),
    ('search_test', 'choose_user', 'es', '— elige un usuario —',
     'search requires a verified user (init 97)'),
    ('search_test', 'no_users', 'cs', '— entita nemá aktivní uživatele —',
     'search requires a verified user (init 97)'),
    ('search_test', 'no_users', 'en', '— no active users for this entity —',
     'search requires a verified user (init 97)'),
    ('search_test', 'no_users', 'de', '— keine aktiven Benutzer —',
     'search requires a verified user (init 97)'),
    ('search_test', 'no_users', 'es', '— sin usuarios activos —',
     'search requires a verified user (init 97)')
) AS v(scope, label_key, lang, label_text, notes)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.ui_label
                  WHERE scope = 'search_test' AND label_key = 'choose_user');

COMMIT;
