-- 139: the answer model being down gets its own message (2026-08-18).
--
-- Until now answer.py caught an LLM failure and returned outcome='no_answer' —
-- the same text, the same log row as a question the corpus genuinely cannot
-- answer. The user was told to REPHRASE, which is the one action that cannot
-- help when the model is the thing that is down, and log.py (which writes
-- outcome into `stage`) counted the outage as a gap in the documents.
--
-- Wording is PROVISIONAL — produktový vlastník owns the final text. Two things it must keep:
-- no "rephrase", and an explicit statement that the question was fine. A user
-- who reads "I have no answer" concludes they asked badly; that inference is
-- exactly what this message exists to prevent.
--
-- config.LLM_UNAVAILABLE_MESSAGE mirrors the Czech row as the code fallback for
-- an unreachable DB; keep the two in step if this text changes.

INSERT INTO edu_ai.system_message (message_key, lang, message, is_active, created_by, notes)
VALUES
  ('llm_unavailable', 'cs',
   'Teď nemůžu odpověď připravit kvůli dočasnému technickému problému. Tvoje otázka je v pořádku — zkus to prosím za chvíli znovu.',
   true, 'migration_139', 'provizorní znění, k přepsání (produktový vlastník)'),
  ('llm_unavailable', 'en',
   'I can''t produce an answer right now because of a temporary technical problem. Your question is fine — please try again in a moment.',
   true, 'migration_139', 'provisional wording, to be rewritten (produktový vlastník)'),
  ('llm_unavailable', 'de',
   'Ich kann gerade wegen eines vorübergehenden technischen Problems keine Antwort erstellen. Deine Frage ist in Ordnung — bitte versuche es gleich noch einmal.',
   true, 'migration_139', 'provisional wording, to be rewritten (produktový vlastník)'),
  ('llm_unavailable', 'es',
   'Ahora mismo no puedo preparar una respuesta por un problema técnico temporal. Tu pregunta está bien: inténtalo de nuevo en un momento.',
   true, 'migration_139', 'provisional wording, to be rewritten (produktový vlastník)')
ON CONFLICT DO NOTHING;
