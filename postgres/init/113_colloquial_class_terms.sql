-- last change in code by Martin Safranek (2026-08-04).
-- 113_colloquial_class_terms.sql
-- Czech colloquial class nouns for grades 6-9: "trénink na přijímací zkoušky
-- v osmičkách" (found unmatched on a live chunk, 2026-08-04). Grades 6-9 ONLY:
-- "pětka"/"jednička" etc. primarily mean the school MARK (marks are 1-5),
-- mapping them to grades would misfire — decided with Martin 2026-08-04.
-- EXACT declined forms, not prefix stems: a prefix "sestk" would also match
-- "šestkrát" (six times), and "v sedmičce" (sedmicce) doesn't share a usable
-- prefix with "sedmička" anyway.
--
-- Idempotent; transactional.

BEGIN;

INSERT INTO edu_ai.grade_term (lang, kind, term, match, grade, created_by, notes)
SELECT 'cs', 'pupil', stem || suffix, 'exact', grade, 'init-113',
       'hovorové označení třídy — ' || noun
FROM (VALUES ('sestk',   6, 'šestka'),
             ('sedmick', 7, 'sedmička'),
             ('osmick',  8, 'osmička'),
             ('devitk',  9, 'devítka')) AS n(stem, grade, noun),
     unnest(ARRAY['a','y','u','ou','am','ach','ami']) AS suffix
ON CONFLICT (lang, kind, term) DO NOTHING;

-- the dative/locative singular and genitive plural palatalize k->c/ek and
-- need their own forms: šestce, sedmičce, osmičce, devítce; šestek, sedmiček…
INSERT INTO edu_ai.grade_term (lang, kind, term, match, grade, created_by, notes)
VALUES
    ('cs', 'pupil', 'sestce',   'exact', 6, 'init-113', 'hovorové označení třídy — šestka'),
    ('cs', 'pupil', 'sestek',   'exact', 6, 'init-113', 'hovorové označení třídy — šestka'),
    ('cs', 'pupil', 'sedmicce', 'exact', 7, 'init-113', 'hovorové označení třídy — sedmička'),
    ('cs', 'pupil', 'sedmicek', 'exact', 7, 'init-113', 'hovorové označení třídy — sedmička'),
    ('cs', 'pupil', 'osmicce',  'exact', 8, 'init-113', 'hovorové označení třídy — osmička'),
    ('cs', 'pupil', 'osmicek',  'exact', 8, 'init-113', 'hovorové označení třídy — osmička'),
    ('cs', 'pupil', 'devitce',  'exact', 9, 'init-113', 'hovorové označení třídy — devítka'),
    ('cs', 'pupil', 'devitek',  'exact', 9, 'init-113', 'hovorové označení třídy — devítka')
ON CONFLICT (lang, kind, term) DO NOTHING;

COMMIT;
