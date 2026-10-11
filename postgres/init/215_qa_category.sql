-- Shared multilingual QA category catalog (qa_category) and the nullable
-- qa_entries.category_id assignment onto it.
-- Model: .claude/qa-portal/category-db.md. Contract: contracts/qa-portal/CONTRACT.md §4.
-- tests/test_qa_category_migration.py pins this file against the approved
-- proposal text in contracts/qa-portal/proposed-215_qa_category.sql.

-- Sdílený vícejazyčný číselník QA kategorií (qa_category) a nullable přiřazení
-- na qa_entries. Model podle .claude/qa-portal/category-db.md, ověřený proti
-- skutečnému schématu v postgres/init_clean/01_schema.sql.
--
-- CO TATO MIGRACE ZÁMĚRNĚ NEDĚLÁ
--   * Žádný seed taxonomie. Ani jeden řádek qa_category. Ukázkové kódy
--     (AFTER_SCHOOL_CARE, …) v návrhu jsou příklady, ne schválený obsah.
--     Kategorie zakládá administrátor explicitně přes API.
--   * Žádný backfill category_id ze starého tags.category. Ten vyžaduje
--     zkontrolované mapování nad skutečnými daty (category-db.md §Převod, bod 2)
--     a je samostatná etapa se samostatným schválením.
--   * Nemění čtecí cestu. search/app/nodes/qa_common.py::category_siblings
--     nadále seskupuje podle tags->>'category'. Přepnutí na category_id bez
--     backfillu by okamžitě odstranilo související otázky u všech existujících
--     položek. Viz CONTRACT.md §4.2 (etapa 2).
--   * Nedotýká se fulltextové konfigurace, ANN indexů, embedding modelu ani
--     jeho dimenze. Přeznačení metadat nemění normalizovaný text otázky.
--
-- DOPAD NA VÝKON: jediný nový index na qa_entries je částečný a úzký. Plán
-- je nutné před nasazením ověřit na skutečných datech (category-db.md §5).

BEGIN;

-- ── 1. qa_category je VŽDY globální ────────────────────────────────────────
-- Bez tohoto CHECKu by škola mohla založit vlastní kopii kategorie a rozbít
-- tím jedinou platformní identitu, na které celý návrh stojí.
ALTER TABLE edu_ai.code_list
    ADD CONSTRAINT code_list_qa_category_is_global
    CHECK (list_code <> 'qa_category' OR legal_entity_code IS NULL);

COMMENT ON CONSTRAINT code_list_qa_category_is_global ON edu_ai.code_list IS
    'qa_category je jeden katalog pro celou platformu: žádné školní kopie. '
    'Ostatní list_code se tím neomezují — school_grade/school_subject vlastnictví '
    'nejbližším vlastníkem zůstává beze změny.';

-- ── 2. Kód kategorie se nerecykluje ────────────────────────────────────────
-- code_list_active_code chrání jen aktivní řádky. Vypnutý kód by tak šlo použít
-- znovu pro jiný význam a starý export by po reimportu znamenal něco jiného.
CREATE UNIQUE INDEX code_list_qa_category_code
    ON edu_ai.code_list (code)
    WHERE list_code = 'qa_category';

COMMENT ON INDEX edu_ai.code_list_qa_category_code IS
    'Jedinečnost kódu qa_category včetně NEAKTIVNÍCH záznamů. Pro obnovení se '
    'aktivuje tentýž řádek, nikdy se nezakládá druhý se stejným kódem.';

-- ── 3. Přiřazení kategorie k položce ───────────────────────────────────────
-- Nullable: nula nebo jedna kategorie na položku. Žádné pole kategorií, žádná
-- kategorie na variantách či embeddingu. domain_ids zůstává nezávislé
-- vícehodnotové členění. entry_key/entry_id se tím nemění.
ALTER TABLE edu_ai.qa_entries
    ADD COLUMN category_id bigint;

ALTER TABLE edu_ai.qa_entries
    ADD CONSTRAINT qa_entries_category_id_fkey
    FOREIGN KEY (category_id) REFERENCES edu_ai.code_list(id) ON DELETE RESTRICT;

COMMENT ON COLUMN edu_ai.qa_entries.category_id IS
    'Volitelné jemné téma položky z globálního číselníku code_list(qa_category). '
    'NULL = bez kategorie. Autoritativní pro portál, API a CSV sloupec '
    'category_code. tags->>''category'' zůstává přechodně jako původní text pro '
    'kompatibilitu a pro dnešní seskupení souvisejících otázek.';

-- ── 4. Obecný FK nestačí ───────────────────────────────────────────────────
-- Do FK na code_list(id) by šlo vložit ID jazyka nebo ročníku. Trigger vynutí
-- správný list_code a globálního vlastníka, a u NOVÉHO nebo ZMĚNĚNÉHO přiřazení
-- také aktivní cíl. Ponechání téže, mezitím deaktivované kategorie při běžné
-- opravě odpovědi projde — jinak by deaktivace kategorie zablokovala editaci
-- všech položek, které ji nesou.
--
-- SOUBĚH: SELECT … FOR SHARE zamkne cílový řádek číselníku, takže deaktivace
-- kategorie běžící současně s přiřazením počká na commit a nemůže vzniknout
-- nové přiřazení na kategorii, která je v té chvíli vypínána.
CREATE FUNCTION edu_ai.qa_entry_category_must_be_valid() RETURNS trigger
    LANGUAGE plpgsql AS $$
DECLARE
    v_list   text;
    v_owner  text;
    v_active boolean;
    v_changed boolean;
BEGIN
    IF NEW.category_id IS NULL THEN
        RETURN NEW;
    END IF;

    v_changed := (TG_OP = 'INSERT')
                 OR (OLD.category_id IS DISTINCT FROM NEW.category_id);

    SELECT list_code, legal_entity_code, is_active
      INTO v_list, v_owner, v_active
      FROM edu_ai.code_list
     WHERE id = NEW.category_id
       FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'qa_entries.category_id % is not a code_list row',
                        NEW.category_id;
    END IF;

    IF v_list <> 'qa_category' THEN
        RAISE EXCEPTION
            'qa_entries.category_id % points at list_code %, not qa_category',
            NEW.category_id, v_list;
    END IF;

    IF v_owner IS NOT NULL THEN
        RAISE EXCEPTION
            'qa_category % must be global (legal_entity_code IS NULL), owner is %',
            NEW.category_id, v_owner;
    END IF;

    IF v_changed AND NOT v_active THEN
        RAISE EXCEPTION
            'qa_category % is inactive and cannot be newly assigned',
            NEW.category_id;
    END IF;

    RETURN NEW;
END $$;

COMMENT ON FUNCTION edu_ai.qa_entry_category_must_be_valid() IS
    'Vynucuje, že category_id míří na aktivní globální qa_category. Beze změny '
    'ponechané neaktivní přiřazení přežije round-trip; nové nebo změněné ne.';

CREATE TRIGGER qa_entries_category_valid
    BEFORE INSERT OR UPDATE OF category_id ON edu_ai.qa_entries
    FOR EACH ROW EXECUTE FUNCTION edu_ai.qa_entry_category_must_be_valid();

-- ── 5. Neměnná identita cíle ───────────────────────────────────────────────
-- Správný FK by šlo porušit pozdější úpravou cíle: přepsat list_code řádku,
-- na který už položky odkazují, nebo přejmenovat kód a tím změnit význam
-- existujících exportů. Název se mění výhradně v labels.
CREATE FUNCTION edu_ai.code_list_identity_is_immutable() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.list_code = 'qa_category' OR NEW.list_code = 'qa_category' THEN
        IF NEW.code <> OLD.code THEN
            RAISE EXCEPTION
                'qa_category code is immutable (% -> %); rename in labels instead',
                OLD.code, NEW.code;
        END IF;
        IF NEW.list_code <> OLD.list_code THEN
            RAISE EXCEPTION 'qa_category list_code is immutable (% -> %)',
                            OLD.list_code, NEW.list_code;
        END IF;
    END IF;

    IF NEW.list_code IS DISTINCT FROM OLD.list_code
       AND EXISTS (SELECT 1 FROM edu_ai.qa_entries
                    WHERE category_id = OLD.id) THEN
        RAISE EXCEPTION
            'code_list row % is referenced by qa_entries.category_id; '
            'its list_code cannot change', OLD.id;
    END IF;

    RETURN NEW;
END $$;

CREATE TRIGGER code_list_identity_immutable
    BEFORE UPDATE OF code, list_code ON edu_ai.code_list
    FOR EACH ROW EXECUTE FUNCTION edu_ai.code_list_identity_is_immutable();

-- ── 6. Index pro kategoriové související otázky ────────────────────────────
-- Úzký a částečný, ve stejném tvaru, v jakém dotaz filtruje. Není to nový
-- fulltextový ani ANN index. Plán ověřit na skutečných datech před nasazením.
CREATE INDEX qa_entries_category_group
    ON edu_ai.qa_entries (legal_entity_code, pack_id, category_id)
    WHERE is_active AND category_id IS NOT NULL;

-- ── 7. Resolver musí globální katalog vrátit i pro školní volání ───────────
-- Obecná větev hledá NEJBLIŽŠÍHO vlastníka a při jeho absenci nevrací globální
-- šablonu, takže by škola pro qa_category nedostala nic. Rozšiřuje se výhradně
-- větev pro globální seznamy; chování language, school_grade, school_subject
-- a ostatních zůstává beze změny (jediný přidaný řádek WHEN).
CREATE OR REPLACE FUNCTION edu_ai.resolved_code_list(p_entity text, p_list text)
    RETURNS SETOF edu_ai.code_list
    LANGUAGE sql STABLE
    AS $$
    WITH RECURSIVE up AS (
        SELECT legal_entity_code, parent_code, 0 AS depth
        FROM edu_ai.legal_entity WHERE legal_entity_code = p_entity
        UNION ALL
        SELECT l.legal_entity_code, l.parent_code, up.depth + 1
        FROM edu_ai.legal_entity l JOIN up ON l.legal_entity_code = up.parent_code
    ), owner AS (
        SELECT u.legal_entity_code
        FROM up u
        WHERE EXISTS (SELECT 1 FROM edu_ai.code_list c
                      WHERE c.is_active AND c.list_code = p_list
                        AND c.legal_entity_code = u.legal_entity_code)
        ORDER BY u.depth
        LIMIT 1
    )
    SELECT c.* FROM edu_ai.code_list c
    WHERE c.is_active AND c.list_code = p_list
      AND c.legal_entity_code IS NOT DISTINCT FROM (
          CASE
              WHEN p_list = 'role' THEN NULL              -- platform registry
              WHEN p_list = 'qa_category' THEN NULL       -- global QA catalog (215)
              WHEN p_list = 'language' THEN
                   (SELECT legal_entity_code FROM owner)  -- selection-with-default
              ELSE
                   COALESCE((SELECT legal_entity_code FROM owner), '(nobody)')
          END)
$$;

COMMENT ON FUNCTION edu_ai.resolved_code_list(p_entity text, p_list text) IS
    'The entity''s číselník set: the NEAREST owner up the tree (self, then parents) '
    'for school lists — a unit may own a list when its semantics differ (SOŠ grades '
    '1..4), otherwise it inherits its organisation''s; never the NULL template. '
    '''language'' is the nearest selection up the tree, else the platform set. '
    '''role'' and ''qa_category'' are platform-global registries. Init 120, 215.';

COMMIT;

-- ── PO SPUŠTĚNÍ ────────────────────────────────────────────────────────────
-- 1. postgres/init_clean/ se NEUPRAVUJE ručně (AGENTS.md). Regeneruje se
--    scripts/gen_clean_install.sh proti řízené referenční databázi.
-- 2. Návrat: DROP TRIGGER/FUNCTION/INDEX/CONSTRAINT a DROP COLUMN category_id.
--    Sloupec je nullable a nic ho zatím nečte, takže návrat nemaže obsah:
--    tags.category zůstává nedotčený. Rollback během souběžných publikací
--    vyžaduje zastavení publikací (category-db.md §Nasazení).
