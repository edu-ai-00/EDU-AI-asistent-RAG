-- last change in code by Martin Safranek (2026-08-23).
--
-- Migrace 170: ročníková patička je POVINNÁ, když kontext ročníky nese.
--
-- v9 říká „end the answer with ONE short italic line", což model číst umí, ale
-- vynechá ji: na „Kdy je nejbližší škola v přírodě?" patička v jednom běhu přišla
-- a v druhém ne, přitom chunk kalendáře akcí ročníky má. Stejná vada jako u výběru
-- ročníkové varianty — vědět nestačí, musí to být příkaz. Formulace „MANDATORY …
-- never omit it" je proto explicitní a doplněná i o zápornou stranu: když kontext
-- ročníky NEnese, patička se nepíše (aby model nezačal vymýšlet).
BEGIN;

UPDATE edu_ai.prompt_registry SET is_active = false
 WHERE prompt_key = 'search_answer_generation' AND version = 9
   AND EXISTS (SELECT 1 FROM edu_ai.prompt_registry p
                WHERE p.prompt_key = 'search_answer_generation'
                  AND p.version = 9 AND p.is_active);

INSERT INTO edu_ai.prompt_registry
       (prompt_key, version, prompt_text, is_active, component, description,
        model_hint, lang, created_by, notes)
SELECT prompt_key, 10,
       replace(prompt_text,
         '1. When the passages you used carry grade information (see "Grades the' || chr(10) ||
         '   context passages apply to"), end the answer with ONE short italic line in' || chr(10) ||
         '   the question''s language stating that the text COMES FROM a PART of a document that concerns that' || chr(10) ||
         '   grade — do NOT claim the answer itself is valid only for it.',
         '1. When the passages you used carry grade information (see "Grades the' || chr(10) ||
         '   context passages apply to"), the answer MUST end with ONE short italic' || chr(10) ||
         '   line in the question''s language stating that the text COMES FROM a PART of' || chr(10) ||
         '   a document that concerns that grade — do NOT claim the answer itself is' || chr(10) ||
         '   valid only for it. This line is MANDATORY whenever those grades are given:' || chr(10) ||
         '   never omit it, and never merge it into a paragraph. When the context' || chr(10) ||
         '   carries NO grade information, write no such line at all.'),
       true, component, description, model_hint, lang, 'migration-170',
       'v10: ročníková patička je povinná, když kontext ročníky nese (v9 ji model vynechával).'
  FROM edu_ai.prompt_registry
 WHERE prompt_key = 'search_answer_generation' AND version = 9
   AND NOT EXISTS (SELECT 1 FROM edu_ai.prompt_registry p2
                    WHERE p2.prompt_key = 'search_answer_generation' AND p2.version = 10);

COMMIT;
