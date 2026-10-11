-- last change in code by Martin Safranek (2026-08-04).
-- 119_entrance_exam_grades.sql
-- "entrance exams -> 9" was too blunt (Martin, 2026-08-04): applicants to an
-- EIGHT-year gymnasium are 5th-graders and to a SIX-year gymnasium
-- 7th-graders. Measured on the Cermat guide: one chunk got {6,9} where the
-- text spoke about multi-year gymnasia. Both enrichment prompts get the
-- precise rule; only the one sentence changes, so the new versions are
-- text-substitutions of the active ones.
--
-- Idempotent; transactional.

BEGIN;

-- chunk_enrichment v1 -> v2
UPDATE edu_ai.prompt_registry SET is_active = false
 WHERE prompt_key = 'chunk_enrichment' AND version = 1;

INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
SELECT 'chunk_enrichment', 2,
       replace(p.prompt_text,
               'pupils sitting entrance exams to secondary school -> 9.',
               'entrance exams: to a FOUR-year secondary programme -> 9, to an EIGHT-year gymnasium (osmileté gymnázium) -> 5, to a SIX-year gymnasium (šestileté gymnázium) -> 7; a mere mention of a multi-year gymnasium without applicants is NOT a grade.'),
       true, p.component, p.description, p.placeholders, p.model_hint,
       p.lang, 'init-119',
       'v2: entrance-exam rule split by programme length (osmileté -> 5, šestileté -> 7, čtyřleté -> 9).'
FROM edu_ai.prompt_registry p
WHERE p.prompt_key = 'chunk_enrichment' AND p.version = 1
ON CONFLICT (prompt_key, version) DO NOTHING;

-- chunk_grade_subject_extraction v2 -> v3 (table/fallback pass)
UPDATE edu_ai.prompt_registry SET is_active = false
 WHERE prompt_key = 'chunk_grade_subject_extraction' AND version = 2;

INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
SELECT 'chunk_grade_subject_extraction', 3,
       replace(p.prompt_text,
               'pupils sitting
  entrance exams to secondary school -> 9.',
               'entrance exams: to a FOUR-year secondary programme -> 9, to an
  EIGHT-year gymnasium (osmileté gymnázium) -> 5, to a SIX-year gymnasium
  (šestileté gymnázium) -> 7; a mere mention of a multi-year gymnasium
  without applicants is NOT a grade.'),
       true, p.component, p.description, p.placeholders, p.model_hint,
       p.lang, 'init-119',
       'v3: entrance-exam rule split by programme length (osmileté -> 5, šestileté -> 7, čtyřleté -> 9).'
FROM edu_ai.prompt_registry p
WHERE p.prompt_key = 'chunk_grade_subject_extraction' AND p.version = 2
ON CONFLICT (prompt_key, version) DO NOTHING;

COMMIT;
