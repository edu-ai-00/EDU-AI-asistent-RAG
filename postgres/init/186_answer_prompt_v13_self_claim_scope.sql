-- last change in code by Martin Safranek (2026-08-26).
--
-- Migrace 186: pravidlo o první osobě platí jen tam, kde je předmětem VÝHRADNĚ
-- asistent.
--
-- v12 (migrace 185) zakázalo tvrdit vlastní schopnosti a hned to přestřelilo:
-- dotaz „umíš poradit s obědy?" dostal odpověď „Odpovídám na otázky z dokumentů
-- této školy" místo pravidla o obědech, které v korpusu je a které předtím
-- odpovídalo správně. Zdvořilostní obal („umíš mi říct“, „umíš poradit s“) totiž
-- vypadá stejně jako dotaz na schopnosti.
--
-- Rozlišení je stejné jako v bráně (route.py, doménová klíčová slova): když je
-- v téže větě školní věc — pravidlo, termín, seznam, postup — je obal jen obalem
-- a odpovídá se na tu školní věc. Pravidlo o první osobě platí jedině tehdy, když
-- v dotazu žádná školní věc není.
--
-- Druhá věc: ta jednovětá odpověď není z dokumentů, takže se k ní nesmí přidat
-- ročníková patička. v12 ji přidalo („Text pochází z části dokumentu, která se
-- týká 5., 7. a 9. ročníku“) a to je nesmysl.
BEGIN;

UPDATE edu_ai.prompt_registry SET is_active = false
 WHERE prompt_key = 'search_answer_generation' AND version = 12 AND is_active;

INSERT INTO edu_ai.prompt_registry
       (prompt_key, version, prompt_text, is_active, component, description,
        model_hint, lang, created_by, notes)
SELECT prompt_key, 13,
       replace(prompt_text,
'   Answer instead with one sentence: you answer questions from this school''s
   documents. This is the ONE case where the answer does not come from the
   passages.',
'   Answer instead with one sentence: you answer questions from this school''s
   documents — and that reply carries NO grade note line, because it does not
   come from a document at all. This is the ONE case where the answer does not
   come from the passages.
   BUT ONLY WHEN THE QUESTION IS ABOUT YOU AND NOTHING ELSE. An ability verb is
   very often just politeness wrapped around a real question: "umíš mi říct, kdy
   jsou přijímačky?", "umíš poradit s obědy?", "můžeš mi najít školní řád?" all
   ask about a SCHOOL matter — a rule, a date, a list, a procedure — and that
   matter is the subject. Answer it normally from the passages and ignore the
   wrapper. Measured: v12 replied "Odpovídám na otázky z dokumentů této školy"
   to "umíš poradit s obědy?" while the lunch rule sat in the context.'),
       true, component,
       'v13: pravidlo o první osobě jen když v dotazu není školní věc',
       model_hint, lang, 'martin',
       'Migrace 186 — v12 přestřelilo na „umíš poradit s obědy?"'
  FROM edu_ai.prompt_registry
 WHERE prompt_key = 'search_answer_generation' AND version = 12;

COMMIT;
