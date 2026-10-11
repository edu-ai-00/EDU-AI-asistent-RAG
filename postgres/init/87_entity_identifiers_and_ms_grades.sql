-- last change in code by Martin Safranek (2026-08-02).
-- 87_entity_identifiers_and_ms_grades.sql
-- The registry records what the school register records, and the grade taxonomy
-- gains the level it was missing.
--
-- ── why the identifiers matter here ─────────────────────────────────────────
-- Škola Nová Ves turned out to be ONE legal person with SIX registered units:
--
--     RED-IZO  1234     the legal person — this is the legal_entity
--     ZŠ       1234
--     SOŠ      1234
--     MŠ       1234
--     ŠD       1234     after-school club
--     ŠJ       1234     canteen
--
-- That settles a question the design had open. One legal person means ONE row in
-- legal_entity, so a grade code cannot be a bare number: inside this single entity
-- "2" is the second year of the primary school, of the secondary school, or of the
-- kindergarten, and the entity cannot disambiguate what it contains three of. The
-- level therefore stays in the code (ZS2 / SS2 / MS2) — see the school_grade rows
-- below — and the search API may accept a bare number only when an entity's active
-- grades happen to share one prefix.
--
-- The unit identifiers are stored as data rather than modelled as a table. Nothing
-- today needs to scope a document to the ZŠ rather than the SOŠ — and the very
-- first document we have spans both (skolni_rad_zs_ss.pdf). When something does
-- need it, the IZOs are already recorded and it becomes one column, not a
-- discovery exercise.
--
-- ── MŠ was missing from the grade taxonomy ──────────────────────────────────
-- code_list('school_grade') held ZS1..ZS9, SS1..SS4 and UNKNOWN. An entity with a
-- kindergarten had nowhere to put a kindergarten document, so it would have landed
-- on UNKNOWN and become invisible to any grade-aware ranking. MS1..MS3 for the
-- three years, ordered before the primary ones.
--
-- Idempotent; transactional.

BEGIN;

ALTER TABLE edu_ai.legal_entity
    ADD COLUMN IF NOT EXISTS red_izo text,
    ADD COLUMN IF NOT EXISTS units jsonb;

COMMENT ON COLUMN edu_ai.legal_entity.red_izo IS
    'RED-IZO — the school register''s id of the LEGAL PERSON. One per entity.';
COMMENT ON COLUMN edu_ai.legal_entity.units IS
    'The registered units under this legal person, {unit: izo}. Data, not a table: '
    'nothing scopes a document to a unit yet and the first document we have spans '
    'two of them — see migration 87.';

UPDATE edu_ai.legal_entity
   SET red_izo = '1234',
       units = jsonb_build_object(
           'ZS',  '1234',
           'SOS', '1234',
           'MS',  '1234',
           'SD',  '1234',
           'SJ',  '1234'),
       notes = 'Jedna právnická osoba (RED-IZO 1234) se šesti zapsanými '
            || 'jednotkami: ZŠ, SOŠ, MŠ, školní družina, školní jídelna. IČO '
            || 'zatím nedoplněno. Provozovny: ŠKOLA 1003863477, MŠ 10038634771.'
 WHERE legal_entity_code = 'SKOLA_NOVA_VES';

-- ── the kindergarten years the taxonomy lacked ──────────────────────────────
INSERT INTO edu_ai.code_list (list_code, code, labels, is_active, sort_order)
SELECT v.list_code, v.code, v.labels::jsonb, true, v.sort_order
FROM (VALUES
  ('school_grade', 'MS1', '{"cs": "1. ročník MŠ", "en": "Year 1 (pre-school)", "de": "1. Jahr (Kindergarten)", "es": "1.º curso (infantil)"}', 1),
  ('school_grade', 'MS2', '{"cs": "2. ročník MŠ", "en": "Year 2 (pre-school)", "de": "2. Jahr (Kindergarten)", "es": "2.º curso (infantil)"}', 2),
  ('school_grade', 'MS3', '{"cs": "3. ročník MŠ", "en": "Year 3 (pre-school)", "de": "3. Jahr (Kindergarten)", "es": "3.º curso (infantil)"}', 3)
) AS v(list_code, code, labels, sort_order)
WHERE NOT EXISTS (
    SELECT 1 FROM edu_ai.code_list c
     WHERE c.list_code = v.list_code AND c.code = v.code AND c.is_active);

COMMIT;
