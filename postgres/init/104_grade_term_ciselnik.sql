-- last change in code by Martin Safranek (2026-08-04).
-- 104_grade_term_ciselnik.sql
-- The grade-detection vocabulary becomes DATA — its own číselník, per language.
--
-- Init 102/103 hardcoded the anchors ("třída", "grade", "Klasse", "curso"),
-- ordinals and pupil nouns into TWO code sites (query side prepare.py, ingest
-- side chunk-insert component). That breaks the project rule that adding a
-- language is INSERTs, not code: a 5th language would need edits in both.
-- Same reasoning as qa_stopword having its own table: vocabulary changes more
-- often than code and MUCH more often than the versioned settings config.
--
--   kind = 'anchor'   a word that marks a grade context ("třída", "ročník",
--                     "grade", "Klasse"…). grade IS NULL. The digit patterns
--                     around anchors ("6. třída" / "třída – 6") stay code —
--                     digits are language-neutral.
--   kind = 'ordinal'  ordinal STEM + anchor right after ("šesté třídy",
--                     "sixth grade") → grade
--   kind = 'pupil'    pupil noun stem, standalone ("šesťák", "prvňáčka") → grade
--   kind = 'year'     school-year word, standalone ("sekunda", "kvartě") → grade
--
--   match = 'prefix'  \b term \w*   (stems — Czech drops vowels when inflecting)
--   match = 'exact'   \b term \b    (bounded forms — an open stem "prim" would
--                     match "primární", "sekund" would match "sekundární")
--
-- All terms are FOLDED (lower, no diacritics): both consumers match on folded
-- text. `lang` scopes administration; detection itself uses every active term
-- (the query language may be undecided, and a folded stem never collides
-- across these languages by construction — verify before adding one that does).
--
-- Idempotent; transactional.

BEGIN;

CREATE TABLE IF NOT EXISTS edu_ai.grade_term (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lang        text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('anchor', 'ordinal', 'pupil', 'year')),
    term        text NOT NULL,
    match       text NOT NULL DEFAULT 'prefix' CHECK (match IN ('prefix', 'exact')),
    grade       int  CHECK (grade BETWEEN 1 AND 9),
    is_active   boolean NOT NULL DEFAULT true,
    created_by  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    notes       text,
    CONSTRAINT grade_term_anchor_no_grade
        CHECK ((kind = 'anchor') = (grade IS NULL)),
    CONSTRAINT grade_term_unique UNIQUE (lang, kind, term)
);

INSERT INTO edu_ai.grade_term (lang, kind, term, match, grade, created_by)
SELECT v.*, 'init-104' FROM (VALUES
    -- anchors (grade NULL)
    ('cs','anchor','trid',   'prefix', NULL::int), ('cs','anchor','rocnik', 'prefix', NULL),
    ('en','anchor','grade',  'prefix', NULL), ('en','anchor','year',   'prefix', NULL),
    ('en','anchor','class',  'prefix', NULL),
    ('de','anchor','klasse', 'prefix', NULL), ('de','anchor','jahrgang','prefix', NULL),
    ('es','anchor','curso',  'prefix', NULL), ('es','anchor','grado',  'prefix', NULL),
    ('es','anchor','clase',  'prefix', NULL),
    -- ordinals (stem + anchor)
    ('cs','ordinal','prvni','prefix',1),('cs','ordinal','druh','prefix',2),
    ('cs','ordinal','tret','prefix',3),('cs','ordinal','ctvrt','prefix',4),
    ('cs','ordinal','pat','prefix',5),('cs','ordinal','sest','prefix',6),
    ('cs','ordinal','sedm','prefix',7),('cs','ordinal','osm','prefix',8),
    ('cs','ordinal','devat','prefix',9),
    ('en','ordinal','first','prefix',1),('en','ordinal','second','prefix',2),
    ('en','ordinal','third','prefix',3),('en','ordinal','fourth','prefix',4),
    ('en','ordinal','fifth','prefix',5),('en','ordinal','sixth','prefix',6),
    ('en','ordinal','seventh','prefix',7),('en','ordinal','eighth','prefix',8),
    ('en','ordinal','ninth','prefix',9),
    ('de','ordinal','erst','prefix',1),('de','ordinal','zweit','prefix',2),
    ('de','ordinal','dritt','prefix',3),('de','ordinal','viert','prefix',4),
    ('de','ordinal','funft','prefix',5),('de','ordinal','sechst','prefix',6),
    ('de','ordinal','siebt','prefix',7),('de','ordinal','siebent','prefix',7),
    ('de','ordinal','acht','prefix',8),('de','ordinal','neunt','prefix',9),
    ('es','ordinal','primer','prefix',1),('es','ordinal','segund','prefix',2),
    ('es','ordinal','tercer','prefix',3),('es','ordinal','cuart','prefix',4),
    ('es','ordinal','quint','prefix',5),('es','ordinal','sext','prefix',6),
    ('es','ordinal','septim','prefix',7),('es','ordinal','octav','prefix',8),
    ('es','ordinal','noven','prefix',9),
    -- pupil nouns (standalone stems; Czech realia)
    ('cs','pupil','prvnac','prefix',1),('cs','pupil','prvnak','prefix',1),
    ('cs','pupil','druhak','prefix',2),('cs','pupil','tretak','prefix',3),
    ('cs','pupil','ctvrtak','prefix',4),('cs','pupil','patak','prefix',5),
    ('cs','pupil','sestak','prefix',6),('cs','pupil','sedmak','prefix',7),
    ('cs','pupil','osmak','prefix',8),('cs','pupil','devatak','prefix',9),
    -- gymnasium years (bounded forms — open stems match primární/sekundární/kvartál)
    ('cs','year','prima','exact',6),('cs','year','primy','exact',6),
    ('cs','year','prime','exact',6),('cs','year','primu','exact',6),
    ('cs','year','primou','exact',6),
    ('cs','year','sekunda','exact',7),('cs','year','sekundy','exact',7),
    ('cs','year','sekunde','exact',7),('cs','year','sekundu','exact',7),
    ('cs','year','sekundou','exact',7),
    ('cs','year','tercie','exact',8),('cs','year','tercii','exact',8),
    ('cs','year','kvarta','exact',9),('cs','year','kvarty','exact',9),
    ('cs','year','kvarte','exact',9),('cs','year','kvartu','exact',9),
    ('cs','year','kvartou','exact',9)
) AS v(lang, kind, term, match, grade)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.grade_term);

COMMIT;
