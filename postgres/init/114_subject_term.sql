-- last change in code by Martin Safranek (2026-08-04).
-- 114_subject_term.sql
-- Subject-detection vocabulary — the subject sibling of grade_term (init 104).
-- Deterministic subject extraction matched ONLY the číselník labels ("Zeměpis",
-- "Dějepis"); pupils and documents also say "zemák", "děják", "matika",
-- "tělák" — and some STANDARD nouns are not in any label either ("čeština":
-- the label says "Český jazyk a literatura"). Terms live per language and map
-- to the subject CODE of the founding template; at ingest the code resolves
-- to the SCHOOL'S own row id (a school without that code ignores the term).
--
-- match='prefix' matches \bterm\w* (stems), match='exact' matches \bterm\b.
-- Exact is used where a stem would collide: "zemak" prefix would match the
-- surname Zemáková, "fyz" would match "fyzicky", "nemine" (won't miss) is
-- skipped from němina's forms entirely.
--
-- Idempotent; transactional.

BEGIN;

CREATE TABLE IF NOT EXISTS edu_ai.subject_term (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lang         text        NOT NULL,
    term         text        NOT NULL,
    match        text        NOT NULL DEFAULT 'prefix'
                             CHECK (match IN ('prefix', 'exact')),
    subject_code text        NOT NULL,
    is_active    boolean     NOT NULL DEFAULT true,
    created_by   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    notes        text,
    UNIQUE (lang, term)
);

COMMENT ON TABLE edu_ai.subject_term IS
    'Subject-detection vocabulary per language (colloquial and standard nouns '
    'missing from code_list labels). Maps to the founding-template subject '
    'code; ingest resolves the code to the school''s own číselník row.';

INSERT INTO edu_ai.subject_term (lang, term, match, subject_code, created_by, notes) VALUES
    -- standard nouns missing from the labels
    ('cs', 'cestin',     'prefix', 'CZECH_LANGUAGE',     'init-114', 'čeština — label říká "Český jazyk a literatura"'),
    ('cs', 'anglictin',  'prefix', 'ENGLISH_LANGUAGE',   'init-114', 'angličtina — label říká "Anglický jazyk"'),
    ('cs', 'nemcin',     'prefix', 'GERMAN_LANGUAGE',    'init-114', 'němčina — label říká "Německý jazyk"'),
    ('cs', 'telocvik',   'prefix', 'PHYSICAL_EDUCATION', 'init-114', 'tělocvik — label říká "Tělesná výchova"'),
    -- hovorové výrazy
    ('cs', 'matik',      'prefix', 'MATHEMATICS',        'init-114', 'matika/matiky/matiku/matikář; "matice" záměrně chybí (kolize: matice)'),
    ('cs', 'dejak',      'prefix', 'HISTORY',            'init-114', 'děják/dějáku/dějákem'),
    ('cs', 'zemak',      'exact',  'GEOGRAPHY',          'init-114', 'zemák — exact: prefix by chytal příjmení Zemáková'),
    ('cs', 'zemaku',     'exact',  'GEOGRAPHY',          'init-114', 'zemák — 2./3./6. pád'),
    ('cs', 'zemakem',    'exact',  'GEOGRAPHY',          'init-114', 'zemák — 7. pád'),
    ('cs', 'zemaky',     'exact',  'GEOGRAPHY',          'init-114', 'zemák — množné číslo'),
    ('cs', 'prirodak',   'prefix', 'BIOLOGY',            'init-114', 'přírodák/přírodáku/přírodákem'),
    ('cs', 'prirodoved', 'prefix', 'BIOLOGY',            'init-114', 'přírodověda (1. stupeň) — mapováno na Přírodopis'),
    ('cs', 'fyza',       'exact',  'PHYSICS',            'init-114', 'fyza — exact: prefix "fyz" by chytal "fyzicky"'),
    ('cs', 'fyzy',       'exact',  'PHYSICS',            'init-114', 'fyza — 2. pád'),
    ('cs', 'fyze',       'exact',  'PHYSICS',            'init-114', 'fyza — 3./6. pád ("ve fyze")'),
    ('cs', 'fyzu',       'exact',  'PHYSICS',            'init-114', 'fyza — 4. pád'),
    ('cs', 'fyzou',      'exact',  'PHYSICS',            'init-114', 'fyza — 7. pád'),
    ('cs', 'chemk',      'prefix', 'CHEMISTRY',          'init-114', 'chemka/chemky/chemku/chemkou'),
    ('cs', 'chemce',     'exact',  'CHEMISTRY',          'init-114', 'chemka — 3./6. pád (k→c)'),
    ('cs', 'obcank',     'prefix', 'CIVICS',             'init-114', 'občanka/občanky — POZOR: kolize s "občanka"=občanský průkaz; deaktivovat, pokud bude vadit'),
    ('cs', 'obcance',    'exact',  'CIVICS',             'init-114', 'občanka — 3./6. pád (k→c)'),
    ('cs', 'hudebk',     'prefix', 'MUSIC_EDUCATION',    'init-114', 'hudebka/hudebky/hudebku'),
    ('cs', 'hudebce',    'exact',  'MUSIC_EDUCATION',    'init-114', 'hudebka — 3./6. pád (k→c)'),
    ('cs', 'vytvark',    'prefix', 'ART_EDUCATION',      'init-114', 'výtvarka/výtvarky/výtvarku'),
    ('cs', 'vytvarce',   'exact',  'ART_EDUCATION',      'init-114', 'výtvarka — 3./6. pád (k→c)'),
    ('cs', 'telak',      'prefix', 'PHYSICAL_EDUCATION', 'init-114', 'tělák/těláku/tělákem'),
    ('cs', 'anglin',     'prefix', 'ENGLISH_LANGUAGE',   'init-114', 'anglina/angliny/anglinu'),
    ('cs', 'ajin',       'prefix', 'ENGLISH_LANGUAGE',   'init-114', 'ajina/ajiny/ajinu'),
    ('cs', 'nemina',     'exact',  'GERMAN_LANGUAGE',    'init-114', 'němina — exact: "nemine" (sloveso) záměrně chybí'),
    ('cs', 'neminy',     'exact',  'GERMAN_LANGUAGE',    'init-114', 'němina — 2. pád'),
    ('cs', 'neminu',     'exact',  'GERMAN_LANGUAGE',    'init-114', 'němina — 4. pád'),
    ('cs', 'neminou',    'exact',  'GERMAN_LANGUAGE',    'init-114', 'němina — 7. pád'),
    ('cs', 'infosk',     'prefix', 'INFORMATICS',        'init-114', 'infoška/infošky/infošku'),
    ('cs', 'infosce',    'exact',  'INFORMATICS',        'init-114', 'infoška — 3./6. pád (k→c)')
ON CONFLICT (lang, term) DO NOTHING;

COMMIT;
