-- 234: the refusal of rude words no longer promises help "with the admission exam".
--
-- system_message 'guard_profanity' is the same for every organisation and every chat, yet the
-- cs, de and es texts ended with "Rád ti pomůžu s otázkami k přijímačkám", which a school's
-- operations chat should not say. They now end like the en text ("I'll be happy to help you
-- with your questions"). The emoji, the only one among the system messages, is dropped in all
-- four languages.
--
-- Undo (the texts before this migration):
--   cs  Zkus se prosím zeptat slušně a bez neslušných slov. Rád ti pomůžu s otázkami k
--       přijímačkám. 🙂
--   de  Bitte stell deine Frage höflich und ohne Schimpfwörter. Ich helfe dir gern bei Fragen
--       zur Aufnahmeprüfung. 🙂
--   en  Please ask politely, without rude words. I'll be happy to help you with your
--       questions. 🙂
--   es  Por favor, haz tu pregunta con educación y sin palabrotas. Con gusto te ayudo con las
--       preguntas sobre el examen de admisión. 🙂
--
-- Idempotent. No transaction control (runner, from 217).

UPDATE edu_ai.system_message m
   SET message = v.message,
       notes = '234: bez přijímaček a bez emoji'
  FROM (VALUES
        ('cs', 'Zkus se prosím zeptat slušně a bez neslušných slov. Rád ti pomůžu s tvými otázkami.'),
        ('en', 'Please ask politely, without rude words. I''ll be happy to help you with your questions.'),
        ('de', 'Bitte stell deine Frage höflich und ohne Schimpfwörter. Ich helfe dir gern bei deinen Fragen.'),
        ('es', 'Por favor, haz tu pregunta con educación y sin palabrotas. Con gusto te ayudo con tus preguntas.')
       ) AS v(lang, message)
 WHERE m.message_key = 'guard_profanity' AND m.is_active AND m.lang = v.lang
   AND m.message IS DISTINCT FROM v.message;

-- self-check: no active refusal text mentions the admission exam
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM edu_ai.system_message
              WHERE message_key = 'guard_profanity' AND is_active
                AND (message ILIKE '%přijímač%' OR message ILIKE '%Aufnahme%'
                     OR message ILIKE '%admisión%')) THEN
    RAISE EXCEPTION '234: a guard_profanity message still mentions the admission exam';
  END IF;
END $$;
