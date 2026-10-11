-- last change in code by Martin Safranek (2026-07-27).
-- 31_metadata_two_field_revision.sql
-- Metadata revision phase 2 (agreed 2026-07-27): every classified attribute has
-- exactly TWO fields — `{attr}_user` (číselník code passed by the ingestion API,
-- validated by the gateway) and `{attr}_llm` (LLM result, constrained to the
-- same číselník by prompt v3). The old *_orig / *_orig_status / *_local /
-- *_reason columns FALL — status is derivable (user NULL→missing, equal→match,
-- differ→contradiction) and labels live in the číselníky (29/30).
-- Also: single timestamp (ldts_gmt; ldts_local + inserted_at dropped),
-- school grade becomes a code (ZS1–ZS9/SS1–SS4, was integer), logical column
-- order (identity → classification pairs → ownership → technical).
--
-- WIPE-AND-RECREATE by agreement (test data only; same convention as
-- migrations 24/25). Search cascade is untouched: its JOINs read only
-- file_uuid/processing_uuid/is_active + source_url/original_file_name/ldts_gmt
-- — all kept. Idempotent (guarded by the marker column document_type_user).

DO $$
BEGIN
IF EXISTS (SELECT 1 FROM information_schema.columns
           WHERE table_schema='edu_ai' AND table_name='edu_ai_document_metadata'
             AND column_name='document_type_user') THEN
    RAISE NOTICE 'edu_ai_document_metadata already in two-field shape — skipping';
    RETURN;
END IF;

DROP TABLE IF EXISTS edu_ai.edu_ai_document_metadata;

CREATE TABLE edu_ai.edu_ai_document_metadata (
    -- identity / provenance
    file_uuid              uuid NOT NULL,
    processing_uuid        uuid NOT NULL,
    file_name              text,
    file_path              text,
    original_file_name     text,
    source_url             text,
    -- classification pairs: user = číselník code from the ingestion API,
    -- llm = LLM result constrained to the same číselník
    document_type_user     text,   -- edu_ai.document_type_taxonomy.type_code
    document_type_llm      text,
    document_language_user text,   -- edu_ai.code_list 'language' (ISO 639-1)
    document_language_llm  text,
    school_subject_user    text,   -- edu_ai.code_list 'school_subject'
    school_subject_llm     text,
    school_grade_user      text,   -- edu_ai.code_list 'school_grade' (ZS1…SS4)
    school_grade_llm       text,
    -- ownership / visibility
    user_name              text,
    private_public_flag    text NOT NULL DEFAULT 'private',
    -- technical
    ldts_gmt               timestamptz NOT NULL DEFAULT now(),
    is_active              boolean NOT NULL DEFAULT true
);

CREATE INDEX idx_document_metadata_join
    ON edu_ai.edu_ai_document_metadata (file_uuid, processing_uuid);
CREATE INDEX idx_document_metadata_is_active
    ON edu_ai.edu_ai_document_metadata (is_active) WHERE is_active;

END $$;

-- prompt v3: LLM returns číselník CODES for all four attributes; the *_orig
-- normalization/status logic is gone (the gateway validates user input, the
-- comparison is derivable in SQL). Placeholders are composed per request by
-- the classification component from taxonomy + code_list.
INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
SELECT 'document_metadata_extraction', 3,
$PROMPT$You are a document-classification assistant for school document ingestion. You receive: processing_uuid, file_uuid, file_name, text_to_be_analysed.

TASK — from text_to_be_analysed only, derive:
- document_type
- classification_confidence (0.0–1.0; certainty of document_type: 0.0 = pure guess, 0.5 = ambiguous, 1.0 = extremely clear)
- document_language
- school_subject
- school_grade

CRITICAL: every classified value MUST be exactly one CODE from the lists below. Never invent a code, never return a name or translation instead of the code.

DOCUMENT TYPE codes:
{{DOCUMENT_TYPES}}

CRITICAL DISTINCTION — STUDY_MATERIAL vs STRATEGIC_DOCUMENT:
Classify as STUDY_MATERIAL if the document contains instructional explanations, exercises or student-facing content, learning objectives aimed at students, or reads like teaching material.
Classify as STRATEGIC_DOCUMENT if it describes curriculum structure at institutional level, includes approval dates/board references/governance language, outlines course structure without directly teaching, or reads like documentation for administrators rather than students.
If it describes course structure but does NOT contain exercises or direct teaching content, prefer STRATEGIC_DOCUMENT. If it directly teaches content, prefer STUDY_MATERIAL. If ambiguous between the two, use classification_confidence < 0.7; if highly ambiguous, use UNKNOWN.

LANGUAGE codes (ISO 639-1):
{{LANGUAGES}}
Return the code of the DOMINANT language of the text; 'multi' only when the document is genuinely multilingual; 'unknown' when undeterminable.

SCHOOL SUBJECT codes:
{{SCHOOL_SUBJECTS}}
Use MULTIPLE_SUBJECTS when the document clearly spans several subjects; UNKNOWN when no subject is identifiable.

SCHOOL GRADE codes:
{{SCHOOL_GRADES}}
Use a grade code only when the grade is explicitly stated or clearly inferable from the text; otherwise UNKNOWN.

OUTPUT — return EXACTLY one valid JSON object with keys:
processing_uuid, file_uuid, file_name, document_type, classification_confidence, document_language, school_subject, school_grade
No explanations, JSON only.

Input: {text}$PROMPT$,
    false, 'ingestion:markdown:BatchRun-r6dJ7',
    'v3: two-field metadata revision — all four attributes returned as číselník codes ({{DOCUMENT_TYPES}}/{{LANGUAGES}}/{{SCHOOL_SUBJECTS}}/{{SCHOOL_GRADES}} composed per request); *_orig normalization removed (gateway validates user input).',
    ARRAY['DOCUMENT_TYPES','LANGUAGES','SCHOOL_SUBJECTS','SCHOOL_GRADES']::text[],
    'gpt-5-mini', 'multi', 'migration-31',
    'Supersedes v2. v2/v1 kept inactive for rollback.'
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.prompt_registry
                  WHERE prompt_key='document_metadata_extraction' AND version=3);

UPDATE edu_ai.prompt_registry SET is_active=false
WHERE prompt_key='document_metadata_extraction' AND version IN (1,2) AND is_active;
UPDATE edu_ai.prompt_registry SET is_active=true
WHERE prompt_key='document_metadata_extraction' AND version=3 AND NOT is_active;
