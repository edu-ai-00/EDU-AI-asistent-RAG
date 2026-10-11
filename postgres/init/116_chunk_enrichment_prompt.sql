-- last change in code by Martin Safranek (2026-08-04).
-- 116_chunk_enrichment_prompt.sql
-- The TEXT branch's two per-chunk LLM calls become ONE (decided 2026-08-04):
-- the LLM reads the whole chunk for keywords anyway, so the keyword BatchRun
-- (q8buW) now uses THIS combined prompt for EVERY row — keywords + grades +
-- subjects in a single response. The deterministic analysis stays in the
-- insert component (always on, free) and everything is validated/unioned
-- against the school's číselník there. chunk_keyword_extraction stays active
-- as the fallback when the switch is off or the fetch fails;
-- chunk_grade_subject_extraction stays active for the TABLE branch, which
-- has no keyword BatchRun.
--
-- Idempotent; transactional.

BEGIN;

INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
VALUES
    ('chunk_enrichment', 1,
'You are a precise chunk-enrichment assistant for a school retrieval system. Texts may be in any language.

INPUT: one record with the field chunk_text (headings may be included).

TASK 1 — KEYWORDS
Extract retrieval keywords: domain-specific nouns or short noun phrases that carry the core meaning and distinguish this text. Prefer technical, named, subject, institutional and educational terms. No verbs, no stopwords, no full sentences, no duplicates, no overly general words.
5-10 keywords (ideal 6-8), lowercase, keep the diacritics of the source language, singular preferred, max four words each, no punctuation.

TASK 2 — GRADES
"grades": ALL school years (strings "1".."9") the text concerns. Resolve explicit numbers, enumerations and ranges ("grades 1-5" -> "1".."5") and unambiguous indirect references — e.g. Czech "první stupeň" -> 1-5, "druhý stupeň" -> 6-9, pupil nouns ("deváťáci" -> 9), gymnasium years (prima -> 6, sekunda -> 7, tercie -> 8, kvarta -> 9), pupils sitting entrance exams to secondary school -> 9. Empty array when genuinely undeterminable.

TASK 3 — SUBJECTS
"subjects": subject codes the text concerns, chosen ONLY from: {{SCHOOL_SUBJECTS}}. Empty array when undeterminable. Never output a code outside the list.

STRICT OUTPUT — a single JSON object, no other text, no markdown fences:
{"keywords": ["..."], "grades": ["..."], "subjects": ["..."]}',
     true,
     'BatchRunComponent-q8buW (MARKDOWN flow, text branch)',
     'Unified per-chunk enrichment: keywords + grades + subjects in one LLM call for every text chunk.',
     ARRAY['{{SCHOOL_SUBJECTS}}'],
     'gpt-5-mini', 'en', 'init-116',
     'v1: unification of chunk_keyword_extraction + chunk_grade_subject_extraction for the text branch; older keys stay active as fallbacks (switch off; table branch).')
ON CONFLICT (prompt_key, version) DO NOTHING;

COMMIT;
