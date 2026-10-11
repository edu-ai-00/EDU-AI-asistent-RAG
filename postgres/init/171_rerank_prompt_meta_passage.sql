-- last change in code by Martin Safranek (2026-08-23).
--
-- Migrace 171: soudce má poznat pasáž, která popisuje sama sebe.
--
-- Na „Co se hodnotí u přijímaček?" dal soudce na 1. místo chunk 301 z CERMATovy
-- stránky „Specifikace požadavků": „Specifikace požadavků jsou dokumenty, které
-- jsou určeny pro jednotnou přijímací zkoušku… Tyto dokumenty vymezují rozsah
-- požadavků…" Ta stránka je rozcestník — dva odstavce o tom, že takové dokumenty
-- existují, a dva odkazy na PDF, které v korpusu nemáme. Vyhrála proto, že má s
-- dotazem nejsilnější slovní shodu. Pasáž s odpovědí (chunk 307: „výsledek
-- jednotné zkoušky tvoří nejméně 60 % celkového hodnocení, u Gymnázia se sportovní
-- přípravou nejméně 40 %") skončila pátá a fáze 4 ji už do kontextu nepustila,
-- protože sources_top_n = 3.
--
-- Pravidlo se přidává OBĚMA verzím soudce (order i graded), protože jde o vlastnost
-- úlohy, ne režimu.
BEGIN;

UPDATE edu_ai.prompt_registry SET is_active = false
 WHERE prompt_key IN ('search_rerank_order', 'search_rerank_graded')
   AND version = 1 AND is_active;

INSERT INTO edu_ai.prompt_registry
       (prompt_key, version, prompt_text, is_active, component, description,
        model_hint, lang, created_by, notes)
SELECT prompt_key, 2,
       prompt_text || chr(10) ||
       'Pasáž, která jen POPISUJE, co nějaký dokument je, k čemu slouží nebo co '
       'vymezuje, na věcný dotaz NEODPOVÍDÁ — i když opakuje jeho slova. Hledej '
       'pasáž, která nese samotný údaj (číslo, podmínku, termín, postup, výčet). '
       'Rozcestník s odkazy na jiné dokumenty patří až za pasáže s údaji.',
       true, component, description, model_hint, lang, 'migration-171',
       'v2: pasáž popisující sama sebe (rozcestník) není odpověď — měřeno na M4.'
  FROM edu_ai.prompt_registry
 WHERE prompt_key IN ('search_rerank_order', 'search_rerank_graded')
   AND version = 1
   AND NOT EXISTS (SELECT 1 FROM edu_ai.prompt_registry p2
                    WHERE p2.prompt_key = edu_ai.prompt_registry.prompt_key
                      AND p2.version = 2);

COMMIT;
