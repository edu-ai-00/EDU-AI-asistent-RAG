-- last change in code by Martin Safranek (2026-08-04).
-- 111_pupil_plural_terms.sql
-- Czech pupil nouns palatalize k->c in the plural: osmák -> osmáci. The
-- seeded prefix stems end in -ak, so every PLURAL form ("osmáci si
-- vyzkoušeli...") slipped through grade detection (found on a live chunk,
-- 2026-08-04). Add the -ac stems alongside; "prvnac" already existed.
--
-- Idempotent; transactional.

BEGIN;

INSERT INTO edu_ai.grade_term (lang, kind, term, match, grade, created_by, notes)
VALUES
    ('cs', 'pupil', 'druhac',  'prefix', 2, 'init-111', 'plural stem: druháci'),
    ('cs', 'pupil', 'tretac',  'prefix', 3, 'init-111', 'plural stem: třeťáci'),
    ('cs', 'pupil', 'ctvrtac', 'prefix', 4, 'init-111', 'plural stem: čtvrťáci'),
    ('cs', 'pupil', 'patac',   'prefix', 5, 'init-111', 'plural stem: páťáci'),
    ('cs', 'pupil', 'sestac',  'prefix', 6, 'init-111', 'plural stem: šesťáci'),
    ('cs', 'pupil', 'sedmac',  'prefix', 7, 'init-111', 'plural stem: sedmáci'),
    ('cs', 'pupil', 'osmac',   'prefix', 8, 'init-111', 'plural stem: osmáci'),
    ('cs', 'pupil', 'devatac', 'prefix', 9, 'init-111', 'plural stem: deváťáci')
ON CONFLICT (lang, kind, term) DO NOTHING;

COMMIT;
