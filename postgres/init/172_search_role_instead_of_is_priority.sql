-- last change in code by Martin Safranek (2026-08-23).
--
-- Migrace 172: is_priority → search_role (priority | normal | excluded).
--
-- Ten sloupec už dnes neodpovídá na otázku „je to priorita?", ale „jakou roli má
-- tenhle typ dokumentu ve vyhledávání" — a k dvěma stavům přibývá třetí, protože
-- ROZCESTNÍK do vyhledávání nepatří vůbec. Doložený případ: CERMATova stránka
-- „Specifikace požadavků" má celý obsah v 970 znacích — dva odstavce o tom, že
-- takové dokumenty existují, a dva odkazy na PDF, která v korpusu nejsou. Na
-- „Co se hodnotí u přijímaček?" ji soudce staví na 1. místo (má s dotazem
-- nejsilnější slovní shodu) a odpověď pak nemá čím odpovědět; pasáž s údajem
-- „nejméně 60 % celkového hodnocení" skončí pátá a fáze 4 ji nepustí dál.
-- Přidání pravidla do promptu soudce (migrace 171) nezabralo — měřeno 3 běhy.
--
-- Hodnota je KURÁTORSKÁ: nikdo ji neodvozuje, žádný prompt o ní neví, zapisuje ji
-- člověk v číselníku. Čte ji jediné místo, answer.py.
BEGIN;

ALTER TABLE edu_ai.taxonomy_rules
  ADD COLUMN IF NOT EXISTS search_role text NOT NULL DEFAULT 'normal';

UPDATE edu_ai.taxonomy_rules
   SET search_role = CASE WHEN is_priority THEN 'priority' ELSE 'normal' END
 WHERE search_role = 'normal';

ALTER TABLE edu_ai.taxonomy_rules
  DROP CONSTRAINT IF EXISTS taxonomy_rules_search_role_chk;
ALTER TABLE edu_ai.taxonomy_rules
  ADD CONSTRAINT taxonomy_rules_search_role_chk
  CHECK (search_role IN ('priority', 'normal', 'excluded'));

COMMENT ON COLUMN edu_ai.taxonomy_rules.search_role IS
  'Role typu/domény ve vyhledávání: priority = autoritní stupeň nad ostatními, '
  'normal = běžný, excluded = do vyhledávání nevstupuje (rozcestníky). '
  'Kurátorská hodnota, nic ji neodvozuje.';

-- is_priority zůstává jako generovaný sloupec, aby starší čtenáři a dumpy nespadly;
-- zápis do něj už nemá smysl, protože pravdu drží search_role.
ALTER TABLE edu_ai.taxonomy_rules DROP COLUMN IF EXISTS is_priority;
ALTER TABLE edu_ai.taxonomy_rules
  ADD COLUMN is_priority boolean
  GENERATED ALWAYS AS (search_role = 'priority') STORED;

-- nový typ dokumentu: ROZCESTNÍK
INSERT INTO edu_ai.taxonomy_rules
       (type_code, taxonomy_type_code, llm_instruction, sort_order, is_active,
        created_by, notes, labels, search_role)
SELECT 'SIGNPOST', 'DOCUMENT_TYPE',
       'Rozcestník: dokument, jehož obsahem jsou pouze odkazy nebo ukazatele na '
       'jiné dokumenty. Sám neuvádí žádný údaj — číslo, termín, podmínku, postup '
       'ani výčet. Typicky rozcestníková stránka webu: nadpis, odstavec o tom, '
       'co jsou dokumenty, na které odkazuje, a seznam odkazů.',
       90, true, 'migration-172',
       'Do vyhledávání nevstupuje (search_role=excluded) — nemá čím odpovědět.',
       '{"cs": "Rozcestník", "en": "Signpost"}'::jsonb, 'excluded'
 WHERE NOT EXISTS (SELECT 1 FROM edu_ai.taxonomy_rules
                    WHERE type_code = 'SIGNPOST' AND taxonomy_type_code = 'DOCUMENT_TYPE');

COMMIT;
