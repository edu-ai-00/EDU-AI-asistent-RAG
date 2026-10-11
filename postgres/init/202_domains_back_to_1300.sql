-- Migrace 202: doménová taxonomie zpět do stavu z 27. 8. 2026, 13:00
--
-- Rekonstrukce podle měření, které jsem pořídil týž den večer PŘED zásahem:
--   8 globálních řádků  (klíčová slova jen u pěti — migrace 130)
--  11 řádků u SKOLA_NOVA_VES_ZS
--  11 řádků u SKOLA_NOVA_VES_SOS
--  = 30 řádků, sort_order 1–13, 13 různých type_code
--
-- Pět domén existovalo VÝHRADNĚ u jednotek a globální řádek neměly:
-- CATERING a ENROLLMENT u obou, MULTIYEAR_GYMNASIUM_ADMISSIONS jen u ZŠ,
-- GRADUATION_EXAM a UNIVERSITY_ADMISSIONS jen u SOŠ. Migrace 201 je omylem
-- vytvořila jako globální — tady se to vrací.
--
-- Klíčová slova na jednotkových řádcích: jen CATERING, ENROLLMENT a MULTIYEAR,
-- protože jejich UPDATE v migraci 130 nemá podmínku legal_entity_code IS NULL a
-- zasáhl tedy jednotkové řádky. Ostatní jednotkové řádky jsou bez nich.
--
-- CO SE NEVRACÍ: popisky v angličtině, němčině a španělštině u devíti domén.
-- Ty čtyři, jejichž globální řádek zásah přežil (BULLYING, DATA_PROTECTION,
-- GENERAL_LEGAL_DOCUMENT, SPECIAL_NEEDS), mají všechny čtyři jazyky a tahle
-- migrace se jich nedotýká. U zbytku je jen čeština; zdroj, ze kterého přišly,
-- na disku není.

BEGIN;

-- 1) klíčová slova, která ve 13:00 nebyla (doplnil je večer můj zásah)
UPDATE edu_ai.taxonomy_rules SET domain_keywords = NULL
 WHERE taxonomy_type_code='DOMAIN' AND legal_entity_code IS NULL
   AND type_code IN ('GENERAL_LEGAL_DOCUMENT','SPECIAL_NEEDS');

-- 2) jednotkové kopie osmi domén, které globální řádek mají (bez klíčových slov)
INSERT INTO edu_ai.taxonomy_rules
  (type_code, taxonomy_type_code, sort_order, is_active, created_by, labels,
   llm_instruction, domain_keywords, search_role, legal_entity_code)
SELECT g.type_code, 'DOMAIN', g.sort_order, true, 'restore-202', g.labels,
       g.llm_instruction, NULL, coalesce(g.search_role,'normal'), e.ent
  FROM edu_ai.taxonomy_rules g
  CROSS JOIN (VALUES ('SKOLA_NOVA_VES_ZS'),('SKOLA_NOVA_VES_SOS')) AS e(ent)
 WHERE g.taxonomy_type_code='DOMAIN' AND g.legal_entity_code IS NULL
   AND g.type_code IN ('ATTENDANCE','BULLYING','DATA_PROTECTION','GRADING',
                       'GENERAL_LEGAL_DOCUMENT','SCHOOL_EVENTS','SPECIAL_NEEDS')
   AND NOT (g.type_code='SECONDARY_ADMISSIONS');

-- SECONDARY_ADMISSIONS měla jednotkovou kopii jen u ZŠ
INSERT INTO edu_ai.taxonomy_rules
  (type_code, taxonomy_type_code, sort_order, is_active, created_by, labels,
   llm_instruction, domain_keywords, search_role, legal_entity_code)
SELECT g.type_code, 'DOMAIN', g.sort_order, true, 'restore-202', g.labels,
       g.llm_instruction, NULL, coalesce(g.search_role,'normal'), 'SKOLA_NOVA_VES_ZS'
  FROM edu_ai.taxonomy_rules g
 WHERE g.taxonomy_type_code='DOMAIN' AND g.legal_entity_code IS NULL
   AND g.type_code='SECONDARY_ADMISSIONS';

-- 3) pět domén, které existovaly JEN u jednotek — přesunout z globálu na jednotky
INSERT INTO edu_ai.taxonomy_rules
  (type_code, taxonomy_type_code, sort_order, is_active, created_by, labels,
   llm_instruction, domain_keywords, search_role, legal_entity_code)
SELECT g.type_code, 'DOMAIN', g.sort_order, true, 'restore-202', g.labels,
       g.llm_instruction, g.domain_keywords, 'normal', v.ent
  FROM edu_ai.taxonomy_rules g
  JOIN (VALUES
    ('CATERING','SKOLA_NOVA_VES_ZS'), ('CATERING','SKOLA_NOVA_VES_SOS'),
    ('ENROLLMENT','SKOLA_NOVA_VES_ZS'), ('ENROLLMENT','SKOLA_NOVA_VES_SOS'),
    ('MULTIYEAR_GYMNASIUM_ADMISSIONS','SKOLA_NOVA_VES_ZS'),
    ('GRADUATION_EXAM','SKOLA_NOVA_VES_SOS'),
    ('UNIVERSITY_ADMISSIONS','SKOLA_NOVA_VES_SOS')
  ) AS v(code, ent) ON v.code = g.type_code
 WHERE g.taxonomy_type_code='DOMAIN' AND g.legal_entity_code IS NULL;

DELETE FROM edu_ai.taxonomy_rules
 WHERE taxonomy_type_code='DOMAIN' AND legal_entity_code IS NULL
   AND type_code IN ('CATERING','ENROLLMENT','MULTIYEAR_GYMNASIUM_ADMISSIONS',
                     'GRADUATION_EXAM','UNIVERSITY_ADMISSIONS');

DO $$
DECLARE g int; z int; s int; tot int;
BEGIN
  SELECT count(*) FILTER (WHERE legal_entity_code IS NULL),
         count(*) FILTER (WHERE legal_entity_code='SKOLA_NOVA_VES_ZS'),
         count(*) FILTER (WHERE legal_entity_code='SKOLA_NOVA_VES_SOS'),
         count(*)
    INTO g, z, s, tot
    FROM edu_ai.taxonomy_rules WHERE taxonomy_type_code='DOMAIN' AND is_active;
  RAISE NOTICE 'globálních %, ZŠ %, SOŠ %, celkem % (čekáno 8 / 11 / 11 / 30)', g, z, s, tot;
  IF (g, z, s) <> (8, 11, 11) THEN
    RAISE EXCEPTION 'migrace 202: struktura nesedí na stav z 13:00';
  END IF;
END $$;

COMMIT;
