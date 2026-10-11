-- last change in code by Martin Safranek (2026-08-05).
-- 124_meta_questions.sql
-- Meta-questions ("Co umíš?") — blok 2, produktový vlastník's scenario 7. Until now RAG
-- role-played from whatever chunks matched ("umím analyzovat texty" from a
-- reading-literacy page). Detection is DATA: qa_blocklist category 'meta'
-- holds the folded phrases per language (extending is an INSERT; the generic
-- blocklist matcher EXCLUDES the category — route._meta_check matches the
-- whole short query exactly). The answer is the new `capabilities` system
-- message with {{SCHOOL}} and {{TOPICS}} substituted live from the corpus.
--
-- Idempotent; transactional.

BEGIN;

-- the category check predates 'meta' — extend it (meta rows are DETECTION
-- vocabulary, not blocked terms; severity 0, excluded from the block matcher)
ALTER TABLE edu_ai.qa_blocklist DROP CONSTRAINT IF EXISTS qa_blocklist_category_check;
ALTER TABLE edu_ai.qa_blocklist ADD CONSTRAINT qa_blocklist_category_check
    CHECK (category = ANY (ARRAY['profanity'::text, 'hate'::text,
                                 'sexual'::text, 'self_harm'::text,
                                 'violence'::text, 'meta'::text]));

-- capabilities message, 4 languages
INSERT INTO edu_ai.system_message (message_key, lang, message, created_by, notes)
SELECT v.k, v.l, v.m, 'init-124', 'meta-otázky (blok 2); {{SCHOOL}} a {{TOPICS}} doplňuje kód'
FROM (VALUES
  ('capabilities', 'cs', 'Jsem asistent školy {{SCHOOL}}. Odpovídám na otázky z jejích dokumentů — například na tato témata: {{TOPICS}}. Zeptej se vlastními slovy, klidně i s překlepy.'),
  ('capabilities', 'en', 'I am the assistant of {{SCHOOL}}. I answer questions from the school''s documents — for example on these topics: {{TOPICS}}. Ask in your own words, typos are fine.'),
  ('capabilities', 'de', 'Ich bin der Assistent der Schule {{SCHOOL}}. Ich beantworte Fragen aus ihren Dokumenten — zum Beispiel zu diesen Themen: {{TOPICS}}. Frag mit eigenen Worten, Tippfehler sind kein Problem.'),
  ('capabilities', 'es', 'Soy el asistente de la escuela {{SCHOOL}}. Respondo preguntas a partir de sus documentos — por ejemplo sobre estos temas: {{TOPICS}}. Pregunta con tus propias palabras, no importan las erratas.')
) AS v(k, l, m)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.system_message
                  WHERE message_key = v.k AND lang = v.l);

-- meta phrases (folded, whole-query match in route._meta_check)
INSERT INTO edu_ai.qa_blocklist (term, term_norm, term_lemma, category, severity, is_phrase, lang, source)
SELECT v.t, v.t, NULL, 'meta', 0, true, v.l, 'init-124'
FROM (VALUES
  ('co umis', 'cs'), ('co vsechno umis', 'cs'), ('co dokazes', 'cs'),
  ('co dovedes', 'cs'), ('jak fungujes', 'cs'), ('kdo jsi', 'cs'),
  ('co jsi zac', 'cs'), ('napoveda', 'cs'), ('pomoc', 'cs'),
  ('s cim mi pomuzes', 'cs'), ('s cim pomuzes', 'cs'),
  ('jak te mam pouzivat', 'cs'), ('jak te pouzivat', 'cs'),
  ('na co se te muzu zeptat', 'cs'), ('co se te muzu ptat', 'cs'),
  ('co umi tento chat', 'cs'), ('k cemu slouzis', 'cs'),
  ('what can you do', 'en'), ('who are you', 'en'), ('help', 'en'),
  ('how do you work', 'en'), ('what do you know', 'en'),
  ('what can i ask you', 'en'),
  ('was kannst du', 'de'), ('wer bist du', 'de'), ('hilfe', 'de'),
  ('wie funktionierst du', 'de'),
  ('que sabes hacer', 'es'), ('quien eres', 'es'), ('ayuda', 'es'),
  ('como funcionas', 'es')
) AS v(t, l)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.qa_blocklist
                  WHERE term_norm = v.t AND category = 'meta');

COMMIT;
