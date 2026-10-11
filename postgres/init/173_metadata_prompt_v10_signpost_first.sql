-- last change in code by Martin Safranek (2026-08-23).
--
-- Migrace 173: klasifikace se nejdřív ptá, jestli je dokument ROZCESTNÍK.
--
-- Rozcestník je jediný typ, který se pozná z FORMY, ne z tématu — a právě proto ho
-- pozdější rozhodování mine: CERMATova stránka „Specifikace požadavků" mluví o
-- přijímačkách, o vědomostech a dovednostech i o RVP, takže každý tematický test ji
-- pošle mezi zkouškové materiály. Přitom její celý obsah je 970 znaků: dva odstavce
-- o tom, že takové dokumenty existují, a dva odkazy na PDF, která v korpusu nejsou.
-- Test na rozcestník proto stojí PŘED vším ostatním: když projde, tématem se už
-- není potřeba zabývat.
BEGIN;

UPDATE edu_ai.prompt_registry SET is_active = false
 WHERE prompt_key = 'document_metadata_extraction' AND version = 9 AND is_active;

INSERT INTO edu_ai.prompt_registry
       (prompt_key, version, prompt_text, is_active, component, description,
        model_hint, lang, created_by, notes)
SELECT prompt_key, 10,
       replace(prompt_text,
         'TASK — from text_to_be_analysed only, derive:',
         'FIRST TEST — SIGNPOST (rozcestník). Before anything else, ask: does the '
         'document CARRY information, or only POINT to where it is? Classify as '
         'SIGNPOST when its whole content is links or references to other '
         'documents plus a sentence or two describing what those documents are, '
         'and it states no fact of its own — no number, date, deadline, condition, '
         'procedure or list. Wording about the subject is NOT content: a page may '
         'name the exam, the subjects and the curriculum and still only say "these '
         'documents define the requirements, download them here". Length is a hint, '
         'not the test — a signpost is usually short, but a long page of links is '
         'one too. When the document does carry facts of its own, SIGNPOST is '
         'wrong even if it also links elsewhere. If it is a SIGNPOST, return that '
         'type and do not weigh the topical types below.' || chr(10) || chr(10) ||
         'TASK — from text_to_be_analysed only, derive:'),
       true, component, description, model_hint, lang, 'migration-173',
       'v10: test na SIGNPOST jako první krok klasifikace (rozcestník se pozná z formy, ne z tématu).'
  FROM edu_ai.prompt_registry
 WHERE prompt_key = 'document_metadata_extraction' AND version = 9
   AND NOT EXISTS (SELECT 1 FROM edu_ai.prompt_registry p2
                    WHERE p2.prompt_key = 'document_metadata_extraction'
                      AND p2.version = 10);

COMMIT;
