-- 207: kratší no_answer_missing_topic (2026-09-04, Jan).
-- Verze z migrace 206 vysvětlovala uživateli mechaniku systému ("Přeformulování
-- nepomůže — informace tam zatím není"). To ho nezajímá: potřebuje vědět, co se
-- stalo a co má dělat dál. Zbytek byl náš interní důvod, ne jeho informace.
--
-- UPDATE, ne INSERT: 206 už na produkci běželo a řádky existují.

UPDATE edu_ai.system_message SET message =
  'Tohle ve školních podkladech nemám. Zkus se zeptat ve škole.'
 WHERE message_key = 'no_answer_missing_topic' AND lang = 'cs';

UPDATE edu_ai.system_message SET message =
  'I do not have this in the school materials. Try asking at school.'
 WHERE message_key = 'no_answer_missing_topic' AND lang = 'en';

UPDATE edu_ai.system_message SET message =
  'Das habe ich in den Schulunterlagen nicht. Frag am besten in der Schule nach.'
 WHERE message_key = 'no_answer_missing_topic' AND lang = 'de';

UPDATE edu_ai.system_message SET message =
  'No tengo esto en los materiales del colegio. Pregunta en el centro.'
 WHERE message_key = 'no_answer_missing_topic' AND lang = 'es';
