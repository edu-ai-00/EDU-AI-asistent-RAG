-- 206: no_answer_missing_topic (2026-09-04, Jan).
-- "Zkus ji prosím přeformulovat" is the right thing to say when the words missed
-- and the wrong thing when the SUBJECT is not in the school's materials at all.
-- Reported from production: "Jak se jmenuje školník?" was answered with a
-- rephrasing, the rephrasing produced another no_answer with another rephrasing,
-- and the loop had no end — the corpus holds no caretaker, so no wording could
-- have worked. qa_lexical knows the difference (every content concept has df 0),
-- and now the message does too.
--
-- Deliberately says what is missing rather than what to do: there is no next step
-- the ASKER can take, and inventing one is what the old message did wrong.

INSERT INTO edu_ai.system_message (message_key, lang, message, is_active, notes)
SELECT v.message_key, v.lang, v.message, true, v.notes
FROM (VALUES
  ('no_answer_missing_topic', 'cs',
   'Tohle ve školních podkladech nemám. Přeformulování nepomůže — informace tam '
   || 'zatím není. Zkus se zeptat školy napřímo.',
   'Shown only when every content concept of the question is absent from the corpus'),
  ('no_answer_missing_topic', 'en',
   'I do not have this in the school materials. Rephrasing will not help — the '
   || 'information is not there yet. Try asking the school directly.', NULL),
  ('no_answer_missing_topic', 'de',
   'Das steht nicht in den Schulunterlagen. Eine andere Formulierung hilft nicht '
   || '— die Information fehlt dort. Frage am besten direkt in der Schule nach.', NULL),
  ('no_answer_missing_topic', 'es',
   'No tengo esto en los materiales del colegio. Reformular no ayudará: la '
   || 'información aún no está ahí. Prueba a preguntar directamente en el centro.', NULL)
) AS v(message_key, lang, message, notes)
WHERE NOT EXISTS (
    SELECT 1 FROM edu_ai.system_message s
     WHERE s.message_key = v.message_key AND s.lang = v.lang);
