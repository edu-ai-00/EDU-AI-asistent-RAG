-- last change in code by Martin Safranek (2026-08-04).
-- 112_gs_llm_signal_trigger.sql
-- The per-chunk grade/subject LLM pass gets a TRIGGER MODE (decided
-- 2026-08-04): "empty" (init 105 behaviour — only chunks where deterministic
-- found neither grades nor subjects), "signal" (chunks whose text carries a
-- grade signal — an anchor/pupil/year term from grade_term — that the
-- deterministic patterns could NOT resolve to a number: "žáci posledního
-- ročníku prvního stupně"), or "all". The signal mode is the target: regexes
-- resolve every explicit mention for free, the LLM is spent exactly on the
-- indirect ones.
--
-- Prompt v2 goes with it: v1 forbade inference ("EXPLICITLY concerns", "Do
-- not guess") — correct for blind classification, wrong for signal-picked
-- chunks where the mention is indirect BY SELECTION. v2 resolves
-- enumerations, ranges and unambiguous indirect references, and still
-- returns empty when genuinely undeterminable.
--
-- Idempotent; transactional.

BEGIN;

-- trigger mode + enable the pass on every active settings row
UPDATE edu_ai.settings
   SET config = jsonb_set(config,
       '{chunking,extract_grade_subject,llm}',
       (config->'chunking'->'extract_grade_subject'->'llm')
           || '{"enabled": true, "trigger": "signal"}'::jsonb)
 WHERE is_active
   AND config->'chunking'->'extract_grade_subject'->'llm' IS NOT NULL;

-- prompt v2: indirect references are the point, not a violation
UPDATE edu_ai.prompt_registry
   SET is_active = false
 WHERE prompt_key = 'chunk_grade_subject_extraction' AND version = 1;

INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
VALUES
    ('chunk_grade_subject_extraction', 2,
'You extract school grade levels and subjects from text chunks of school
documents. Texts may be in any language. Chunks were pre-screened: they
likely mention grades/classes, often indirectly.

For each input chunk return:
- "grades": ALL school years (strings "1".."9") the chunk concerns. Resolve
  explicit numbers, enumerations and ranges ("grades 1-5" -> "1".."5"), and
  unambiguous indirect references — e.g. Czech "první stupeň" -> 1-5,
  "druhý stupeň" -> 6-9, pupil nouns ("deváťáci" -> 9), gymnasium years
  (prima -> 6, sekunda -> 7, tercie -> 8, kvarta -> 9), pupils sitting
  entrance exams to secondary school -> 9.
- "subjects": subject codes the chunk concerns, chosen ONLY from:
{{SCHOOL_SUBJECTS}}

Resolve only what the text supports; when genuinely undeterminable, return
empty arrays. Never output a grade outside "1".."9" or a subject outside
the list.

Input: a JSON array of {"i": <index>, "text": "<chunk text>"}.
Output STRICT JSON: {"results": [{"i": <index>, "grades": [...], "subjects": [...]}]}
One result object per input chunk, same "i".',
     true,
     'PostgresInsertVectorisedDocuments (MARKDOWN flow, shared chunk path)',
     'Per-chunk grade/subject extraction for signal-picked chunks — resolves indirect references.',
     ARRAY['{{SCHOOL_SUBJECTS}}'],
     'gpt-5-mini', 'en', 'init-112',
     'v2: signal-trigger companion — v1 forbade indirect inference, which the signal mode exists to resolve.')
ON CONFLICT (prompt_key, version) DO NOTHING;

COMMIT;
