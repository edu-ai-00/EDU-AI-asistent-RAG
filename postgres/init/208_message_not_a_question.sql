-- 208: not_a_question (2026-09-04, Jan).
-- Reported: "Aha" and "Tak to není sranda" were rewritten by resolve_context into
-- the PREVIOUS question and answered again, three turns running. The rewriter is
-- told to fill the subject in from context, and with no question in the input it
-- fills in all of it.
--
-- Returning the input unchanged stops the repeat but leaves the cascade searching
-- the corpus for "sranda" and answering "Tohle ve školních podkladech nemám" to a
-- remark. So the rewriter now says outright that the input is not a question, and
-- this is what the user gets instead: no retrieval, no LLM, tens of milliseconds.
--
-- Deliberately short and free of apology: nothing went wrong, the person simply
-- said something that is not a question.

INSERT INTO edu_ai.system_message (message_key, lang, message, is_active, notes)
SELECT v.message_key, v.lang, v.message, true, v.notes
FROM (VALUES
  ('not_a_question', 'cs',
   'Rozumím. Zeptej se na cokoli dalšího.',
   'Shown when resolve_context recognises the input as an acknowledgement, not a question'),
  ('not_a_question', 'en', 'Understood. Ask me anything else.', NULL),
  ('not_a_question', 'de', 'Alles klar. Frag mich gerne etwas anderes.', NULL),
  ('not_a_question', 'es', 'Entendido. Pregúntame cualquier otra cosa.', NULL)
) AS v(message_key, lang, message, notes)
WHERE NOT EXISTS (
    SELECT 1 FROM edu_ai.system_message s
     WHERE s.message_key = v.message_key AND s.lang = v.lang);
