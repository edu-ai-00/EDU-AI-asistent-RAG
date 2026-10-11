-- last change in code by Martin Safranek (2026-07-27).
-- 29_document_type_taxonomy.sql
-- Document-type taxonomy moves from INSIDE the LLM prompt to its own číselník.
-- Single source of truth consumed by: the MARKDOWN-flow classification component
-- (composes the prompt's {{DOCUMENT_TYPES}} section per request), the search API
-- (GET /document-types + filter validation) and, through the gateway proxy
-- (GET /v1/document-types, 60 s TTL cache), the upload UI select menu.
--
-- Design (agreed 2026-07-26): stable UPPERCASE type_code (matches the values the
-- LLM already returns → existing edu_ai_document_metadata.document_type rows stay
-- valid), label_cs for display, llm_instruction = the per-type definition that
-- used to live in the prompt. UNKNOWN acts as the catch-all ("other"). NO hard FK
-- from document_metadata (an LLM output must never break the insert — the
-- component maps unknown outputs to UNKNOWN instead).
--
-- Also inserts prompt v2 of `document_metadata_extraction`: identical to v1
-- except the hardcoded type list is replaced by the {{DOCUMENT_TYPES}}
-- placeholder (derived from the ACTIVE v1 via regexp_replace, so formatting
-- stays byte-consistent). v1 is kept inactive for rollback.
-- Idempotent; transactional.

BEGIN;

CREATE TABLE IF NOT EXISTS edu_ai.document_type_taxonomy (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    type_code       text NOT NULL,
    label_cs        text NOT NULL,
    label_en        text NOT NULL,
    llm_instruction text NOT NULL,
    sort_order      integer NOT NULL,
    is_active       boolean NOT NULL DEFAULT true,
    created_by      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    notes           text
);

-- upgrade path for a DB that ran the first revision of this migration
ALTER TABLE edu_ai.document_type_taxonomy ADD COLUMN IF NOT EXISTS label_en text;
UPDATE edu_ai.document_type_taxonomy SET label_en = v.label_en
FROM (VALUES
    ('INTERNAL_LEGAL_DOCUMENT',     'Internal regulations / policies'),
    ('STUDY_MATERIAL',              'Study material'),
    ('STRATEGIC_DOCUMENT',          'Strategic document'),
    ('SCHOOL_INFORMATION_DOCUMENT', 'School information (events, notices)'),
    ('ADMINISTRATIVE_RECORD',       'Administrative record'),
    ('EXAMINATION_DOCUMENT',        'Examination document (tests, assignments)'),
    ('MIXED_DOCUMENT',              'Mixed document'),
    ('UNKNOWN',                     'Uncategorized / other')
) AS v(type_code, label_en)
WHERE edu_ai.document_type_taxonomy.type_code = v.type_code
  AND edu_ai.document_type_taxonomy.label_en IS NULL;
ALTER TABLE edu_ai.document_type_taxonomy ALTER COLUMN label_en SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS document_type_taxonomy_active_code
    ON edu_ai.document_type_taxonomy (type_code) WHERE is_active;

INSERT INTO edu_ai.document_type_taxonomy
    (type_code, label_cs, label_en, llm_instruction, sort_order, created_by, notes)
SELECT * FROM (VALUES
    ('INTERNAL_LEGAL_DOCUMENT',     'Interní předpis / řád', 'Internal regulations / policies',
     'Formal regulations, policies, procedures, governance documents.', 10, 'migration-29', NULL),
    ('STUDY_MATERIAL',              'Studijní materiál', 'Study material',
     'Instructional content directly used for teaching students.', 20, 'migration-29', NULL),
    ('STRATEGIC_DOCUMENT',          'Strategický dokument', 'Strategic document',
     'Institutional planning, curriculum approval, annual reports, development plans, governance-level documents.', 30, 'migration-29', NULL),
    ('SCHOOL_INFORMATION_DOCUMENT', 'Informace školy (akce, sdělení)', 'School information (events, notices)',
     'Event info, trips, schedules, communication to parents/students.', 40, 'migration-29', NULL),
    ('ADMINISTRATIVE_RECORD',       'Administrativní záznam', 'Administrative record',
     'Attendance lists, grade sheets, internal records.', 50, 'migration-29', NULL),
    ('EXAMINATION_DOCUMENT',        'Zkouškový dokument (testy, zadání)', 'Examination document (tests, assignments)',
     'Tests, assignments, question sheets.', 60, 'migration-29', NULL),
    ('MIXED_DOCUMENT',              'Smíšený dokument', 'Mixed document',
     'Clearly contains multiple independent document types.', 70, 'migration-29', NULL),
    ('UNKNOWN',                     'Nezařazeno / ostatní', 'Uncategorized / other',
     'Cannot determine confidently.', 80, 'migration-29',
     'Catch-all ("other"): the classification component maps any LLM output not present in this taxonomy to UNKNOWN.')
) AS v(type_code, label_cs, label_en, llm_instruction, sort_order, created_by, notes)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.document_type_taxonomy);

-- prompt v2: hardcoded type list -> {{DOCUMENT_TYPES}} placeholder
INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
SELECT prompt_key,
       2,
       regexp_replace(
           prompt_text,
           'DOCUMENT TYPE \(choose exactly one, return in UPPERCASE\).*?⸻',
           'DOCUMENT TYPE (choose exactly one, return in UPPERCASE)  The allowed types and their definitions: {{DOCUMENT_TYPES}}  ⸻'),
       false,
       component,
       'v2: taxonomy externalized to edu_ai.document_type_taxonomy — the component substitutes {{DOCUMENT_TYPES}} per request.',
       ARRAY['DOCUMENT_TYPES']::text[],
       model_hint, lang, 'migration-29',
       'Derived from v1 via regexp_replace; v1 kept inactive for rollback.'
FROM edu_ai.prompt_registry
WHERE prompt_key = 'document_metadata_extraction' AND version = 1
  AND NOT EXISTS (SELECT 1 FROM edu_ai.prompt_registry
                  WHERE prompt_key = 'document_metadata_extraction' AND version = 2);

UPDATE edu_ai.prompt_registry SET is_active = false
WHERE prompt_key = 'document_metadata_extraction' AND version = 1 AND is_active;
UPDATE edu_ai.prompt_registry SET is_active = true
WHERE prompt_key = 'document_metadata_extraction' AND version = 2 AND NOT is_active;

COMMIT;
