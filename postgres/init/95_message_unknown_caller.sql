-- last change in code by Martin Safranek (2026-08-02).
-- 95_message_unknown_caller.sql
-- One generic message for "this caller cannot be accepted", in four languages.
--
-- /search refuses a request whose user_id does not belong to the entity it names,
-- and the refusal used to say WHY: whether the user existed, which entity it
-- belonged to, whether the entity was above or beside. Each of those is a fact
-- about somebody else's organisation, handed to whoever asked. Enumerating users or
-- entities becomes a matter of reading the error messages.
--
-- So the reason stays in the server log — where it is needed to diagnose a real
-- problem — and the caller gets one sentence that covers every case: unknown user,
-- inactive user, user of another school, user of a sibling unit, and whatever else
-- turns out to belong on the list. The message deliberately does not distinguish
-- them, and it says what to do next instead.
--
-- In system_message, not in code: the four translations are data like every other
-- canned message (guard_profanity, no_answer, qa_suggest), and system_message()
-- already resolves the language from the registry's is_ui_default when the query
-- language is unknown.
--
-- Idempotent; transactional.

BEGIN;

INSERT INTO edu_ai.system_message (message_key, lang, message, is_active, notes)
SELECT v.message_key, v.lang, v.message, true, v.notes
FROM (VALUES
  ('unknown_caller', 'cs',
   'Tento uživatel není známý nebo není přiřazený k této škole. Zkontroluj '
   || 'prosím přihlášení, nebo se obrať na správce školy.',
   'Deliberately generic: the concrete reason would let anyone enumerate users and entities'),
  ('unknown_caller', 'en',
   'This user is unknown or is not assigned to this school. Please check the '
   || 'sign-in, or contact the school administrator.', NULL),
  ('unknown_caller', 'de',
   'Dieser Benutzer ist unbekannt oder dieser Schule nicht zugeordnet. Bitte '
   || 'überprüfe die Anmeldung oder wende dich an die Schulverwaltung.', NULL),
  ('unknown_caller', 'es',
   'Este usuario es desconocido o no está asignado a este centro. Comprueba el '
   || 'inicio de sesión o contacta con la administración del centro.', NULL)
) AS v(message_key, lang, message, notes)
WHERE NOT EXISTS (
    SELECT 1 FROM edu_ai.system_message m
     WHERE m.message_key = v.message_key AND m.lang = v.lang AND m.is_active);

COMMIT;
