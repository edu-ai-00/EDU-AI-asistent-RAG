-- last change in code by Martin Safranek (2026-08-04).
-- 105_grade_subject_llm_option.sql
-- Grade/subject extraction per chunk: deterministic ALWAYS, LLM as a SWITCH.
--
-- The deterministic pass (grade_term číselník + subject labels, init 103/104)
-- is free and runs on every chunk. The LLM pass costs one call per batch of
-- chunks on EVERY ingest run, so it is off by default and lives behind a
-- settings parameter (decided 2026-08-04):
--
--   settings.config -> chunking -> extract_grade_subject:
--     { "llm": { "enabled": false, "model": "gpt-5-mini",
--                "max_chunk_chars": 600, "batch_size": 20 } }
--
-- When enabled, the LLM runs ONLY for chunks where the deterministic pass
-- found nothing — the cheap signal always wins, the model fills the gaps
-- (implicit mentions the regexes cannot see: "učivo pro předposlední ročník
-- prvního stupně"). Results are validated: grades must be 1..9, subjects must
-- be codes of code_list('school_subject') — the model cannot invent a value.
--
-- The prompt is DATA (prompt_registry, hot-editable), like every other LLM
-- instruction in the system. {{SCHOOL_SUBJECTS}} is substituted from the
-- číselník at runtime, so a new subject needs no prompt edit.
--
-- Idempotent; transactional.

BEGIN;

INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, created_by, notes)
SELECT 'chunk_grade_subject_extraction', 1,
'You classify text chunks from school documents. Texts may be in any language.

For each input chunk identify:
- "grades": school year numbers (strings "1".."9") the chunk EXPLICITLY concerns
- "subjects": subject codes the chunk EXPLICITLY concerns, chosen ONLY from:
{{SCHOOL_SUBJECTS}}

Use only what the text states. Do not guess from vague context. When nothing
is determinable, return empty arrays.

Input: a JSON array of {"i": <index>, "text": "<chunk text>"}.
Output STRICT JSON: {"results": [{"i": <index>, "grades": [...], "subjects": [...]}]}
One result object per input chunk, same "i".',
    true, 'init-105',
    'Per-chunk grade/subject extraction — used by the chunks insert component '
    'when chunking.extract_grade_subject.llm.enabled is true.'
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.prompt_registry
                  WHERE prompt_key = 'chunk_grade_subject_extraction');

UPDATE edu_ai.settings s
   SET config = jsonb_set(
         s.config, '{chunking,extract_grade_subject}',
         '{"llm": {"enabled": false, "model": "gpt-5-mini",
                   "max_chunk_chars": 600, "batch_size": 20}}'::jsonb)
 WHERE s.is_active
   AND s.config ? 'chunking'
   AND NOT (s.config->'chunking') ? 'extract_grade_subject';

COMMIT;
