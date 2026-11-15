-- 233: the capabilities answer introduces the assistant as EDU-AI's, not as a school's.
--
-- The message (system_message 'capabilities', route._meta_check) answers "Co umíš?" and,
-- since 229, the questions about the assistant itself ("Kolik ti je let?"). It began with
-- "Jsem asistent školy {{SCHOOL}}" and spoke of "its documents", which is wrong where one
-- school runs several chats (an admissions chat introduced itself as the school's
-- assistant) and for questions about the assistant. The topics ({{TOPICS}}) stay.
-- {{SCHOOL}} is no longer used here; the identity answer ("Jak se jmenuje naše škola?",
-- 'school_identity') still names the school.
--
-- Undo (the texts before this migration):
--   cs  Jsem asistent školy {{SCHOOL}}. Odpovídám na otázky z jejích dokumentů — například na
--       tato témata: {{TOPICS}}. Zeptej se vlastními slovy, klidně i s překlepy.
--   de  Ich bin der Assistent der Schule {{SCHOOL}}. Ich beantworte Fragen aus ihren
--       Dokumenten — zum Beispiel zu diesen Themen: {{TOPICS}}. Frag mit eigenen Worten,
--       Tippfehler sind kein Problem.
--   en  I am the assistant of {{SCHOOL}}. I answer questions from the school's documents —
--       for example on these topics: {{TOPICS}}. Ask in your own words, typos are fine.
--   es  Soy el asistente de la escuela {{SCHOOL}}. Respondo preguntas a partir de sus
--       documentos — por ejemplo sobre estos temas: {{TOPICS}}. Pregunta con tus propias
--       palabras, no importan las erratas.
--
-- Idempotent. No transaction control (runner, from 217).

UPDATE edu_ai.system_message m
   SET message = v.message,
       notes = '233: AI asistent od EDU-AI (dříve asistent školy)'
  FROM (VALUES
        ('cs', 'Jsem AI asistent od EDU-AI. Odpovídám na otázky — například na tato témata: {{TOPICS}}. Zeptej se vlastními slovy.'),
        ('en', 'I am an AI assistant by EDU-AI. I answer questions — for example on these topics: {{TOPICS}}. Ask in your own words.'),
        ('de', 'Ich bin ein KI-Assistent von EDU-AI. Ich beantworte Fragen — zum Beispiel zu diesen Themen: {{TOPICS}}. Frag mit eigenen Worten.'),
        ('es', 'Soy un asistente de IA de EDU-AI. Respondo preguntas — por ejemplo sobre estos temas: {{TOPICS}}. Pregunta con tus propias palabras.')
       ) AS v(lang, message)
 WHERE m.message_key = 'capabilities' AND m.is_active AND m.lang = v.lang
   AND m.message IS DISTINCT FROM v.message;

-- self-check: every active capabilities message is the EDU-AI one and keeps {{TOPICS}}
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM edu_ai.system_message
              WHERE message_key = 'capabilities' AND is_active
                AND lang IN ('cs', 'en', 'de', 'es')
                AND (message NOT LIKE '%EDU-AI%' OR message NOT LIKE '%{{TOPICS}}%')) THEN
    RAISE EXCEPTION '233: an active capabilities message was not updated';
  END IF;
END $$;
