-- last change in code by Martin Safranek (2026-07-27).
-- 30_code_lists.sql
-- Generic code lists (číselníky) for LANGUAGES, SCHOOL SUBJECTS and SCHOOL
-- GRADES — the same idea as edu_ai.document_type_taxonomy (migration 29), but
-- since all three share the identical minimal shape agreed 2026-07-27
-- ("kód + název v ČJ a AJ — toť vše"), they live in ONE generic table with a
-- list discriminator. Adding a future list = INSERT, no DDL.
--
-- Codes: languages use ISO 639-1; subjects use stable UPPERCASE English codes
-- (same style as document_type_taxonomy); grades use ZS1–ZS9 / SS1–SS4.
-- Each list has an UNKNOWN catch-all (subjects also MULTIPLE_SUBJECTS).
-- Served by search API GET /code-lists and the gateway proxy
-- /v1/code-lists (TTL cache) for UI select menus. Wiring the codes into
-- edu_ai_document_metadata storage/validation is the metadata-revision phase 2.
-- Idempotent per list; transactional.

BEGIN;

CREATE TABLE IF NOT EXISTS edu_ai.code_list (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    list_code  text NOT NULL,          -- 'language' | 'school_subject' | 'school_grade'
    code       text NOT NULL,          -- ISO 639-1 / stable code
    label_cs   text NOT NULL,
    label_en   text NOT NULL,
    sort_order integer NOT NULL,
    is_active  boolean NOT NULL DEFAULT true,
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    notes      text
);

CREATE UNIQUE INDEX IF NOT EXISTS code_list_active_code
    ON edu_ai.code_list (list_code, code) WHERE is_active;

-- ── languages (ISO 639-1) ──────────────────────────────────────────────
INSERT INTO edu_ai.code_list (list_code, code, label_cs, label_en, sort_order, created_by)
SELECT 'language', * FROM (VALUES
    ('cs',    'Čeština',      'Czech',        10, 'migration-30'),
    ('en',    'Angličtina',   'English',      20, 'migration-30'),
    ('sk',    'Slovenština',  'Slovak',       30, 'migration-30'),
    ('de',    'Němčina',      'German',       40, 'migration-30'),
    ('pl',    'Polština',     'Polish',       50, 'migration-30'),
    ('uk',    'Ukrajinština', 'Ukrainian',    60, 'migration-30'),
    ('fr',    'Francouzština','French',       70, 'migration-30'),
    ('es',    'Španělština',  'Spanish',      80, 'migration-30'),
    ('multi', 'Vícejazyčný',  'Multilingual', 90, 'migration-30'),
    ('unknown','Neurčeno',    'Unknown',     100, 'migration-30')
) AS v(code, label_cs, label_en, sort_order, created_by)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.code_list WHERE list_code = 'language');

-- ── school subjects (RVP ZŠ core) ─────────────────────────────────────
INSERT INTO edu_ai.code_list (list_code, code, label_cs, label_en, sort_order, created_by)
SELECT 'school_subject', * FROM (VALUES
    ('CZECH_LANGUAGE',     'Český jazyk a literatura', 'Czech Language and Literature', 10, 'migration-30'),
    ('MATHEMATICS',        'Matematika',               'Mathematics',                   20, 'migration-30'),
    ('ENGLISH_LANGUAGE',   'Anglický jazyk',           'English Language',              30, 'migration-30'),
    ('GERMAN_LANGUAGE',    'Německý jazyk',            'German Language',               40, 'migration-30'),
    ('INFORMATICS',        'Informatika',              'Informatics',                   50, 'migration-30'),
    ('HISTORY',            'Dějepis',                  'History',                       60, 'migration-30'),
    ('GEOGRAPHY',          'Zeměpis',                  'Geography',                     70, 'migration-30'),
    ('BIOLOGY',            'Přírodopis',               'Biology / Natural Science',     80, 'migration-30'),
    ('PHYSICS',            'Fyzika',                   'Physics',                       90, 'migration-30'),
    ('CHEMISTRY',          'Chemie',                   'Chemistry',                    100, 'migration-30'),
    ('CIVICS',             'Občanská výchova',         'Civics',                       110, 'migration-30'),
    ('MUSIC_EDUCATION',    'Hudební výchova',          'Music Education',              120, 'migration-30'),
    ('ART_EDUCATION',      'Výtvarná výchova',         'Art Education',                130, 'migration-30'),
    ('PHYSICAL_EDUCATION', 'Tělesná výchova',          'Physical Education',           140, 'migration-30'),
    ('MULTIPLE_SUBJECTS',  'Více předmětů',            'Multiple subjects',            150, 'migration-30'),
    ('UNKNOWN',            'Nezařazeno',               'Unknown',                      160, 'migration-30')
) AS v(code, label_cs, label_en, sort_order, created_by)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.code_list WHERE list_code = 'school_subject');

-- ── school grades (CZ system: ZŠ 1–9, SŠ 1–4) ─────────────────────────
INSERT INTO edu_ai.code_list (list_code, code, label_cs, label_en, sort_order, created_by)
SELECT 'school_grade', * FROM (VALUES
    ('ZS1', '1. ročník ZŠ', 'Grade 1 (primary)',          10, 'migration-30'),
    ('ZS2', '2. ročník ZŠ', 'Grade 2 (primary)',          20, 'migration-30'),
    ('ZS3', '3. ročník ZŠ', 'Grade 3 (primary)',          30, 'migration-30'),
    ('ZS4', '4. ročník ZŠ', 'Grade 4 (primary)',          40, 'migration-30'),
    ('ZS5', '5. ročník ZŠ', 'Grade 5 (primary)',          50, 'migration-30'),
    ('ZS6', '6. ročník ZŠ', 'Grade 6 (lower secondary)',  60, 'migration-30'),
    ('ZS7', '7. ročník ZŠ', 'Grade 7 (lower secondary)',  70, 'migration-30'),
    ('ZS8', '8. ročník ZŠ', 'Grade 8 (lower secondary)',  80, 'migration-30'),
    ('ZS9', '9. ročník ZŠ', 'Grade 9 (lower secondary)',  90, 'migration-30'),
    ('SS1', '1. ročník SŠ', 'Year 1 (upper secondary)',  100, 'migration-30'),
    ('SS2', '2. ročník SŠ', 'Year 2 (upper secondary)',  110, 'migration-30'),
    ('SS3', '3. ročník SŠ', 'Year 3 (upper secondary)',  120, 'migration-30'),
    ('SS4', '4. ročník SŠ', 'Year 4 (upper secondary)',  130, 'migration-30'),
    ('UNKNOWN', 'Neurčeno', 'Unknown',                   140, 'migration-30')
) AS v(code, label_cs, label_en, sort_order, created_by)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.code_list WHERE list_code = 'school_grade');

COMMIT;
