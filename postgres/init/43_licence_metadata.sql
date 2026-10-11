-- 43_licence_metadata.sql
-- Licence and authorship as DATABASE METADATA, not just as files in the repo.
--
-- Why bother: the schema outlives the checkout it came from. A dump restored
-- somewhere else, a pgAdmin session, or a database handed over on its own carries
-- none of LICENSE / NOTICE / the component headers — but it does carry COMMENTs.
-- `\dn+`, `\l+` and the object browser all show them, so whoever opens the
-- database sees who wrote it and under what terms.
--
-- Keep this in sync with NOTICE if the authors or the licence ever change.
-- Idempotent by nature: COMMENT ON overwrites.

BEGIN;

COMMENT ON DATABASE edu_ai IS
    'edu_ai — multilingual RAG + Q&A platform. '
    'Licensed under the Apache License 2.0. '
    'Commissioned by EDU-AI, o.p.s. (IČO 01674846). See LICENSE and NOTICE in the source repository.';

COMMENT ON SCHEMA edu_ai IS
    'edu_ai application schema — documents, chunks, Q&A packs, and the číselníky that '
    'drive behaviour without code changes (code_list, document_type_taxonomy, '
    'prompt_registry, search_setting, system_message, qa_stopword/synonym/blocklist). '
    'Apache License 2.0 — see LICENSE and NOTICE in the repository.';

-- The tables a newcomer opens first, so the description is where they look.
COMMENT ON TABLE edu_ai.code_list IS
    'Číselník: the single registry of languages, school subjects and grades. '
    'Adding a language STARTS here — insert with is_active = false, fill the other '
    'layers, activate last. is_ui_default = presentation only; '
    'is_processing_fallback = what the pipeline uses when detection cannot tell.';

COMMENT ON TABLE edu_ai.search_setting IS
    'Versioned retrieval configuration: cascade steps, thresholds, models and the '
    'per-language reducers (config->engines). One active row; chunks and qa_variants '
    'record which setting produced them via setting_id.';

COMMENT ON TABLE edu_ai.prompt_registry IS
    'Every LLM prompt in the solution, versioned. Read per request (UI > DB > code), '
    'so a change takes effect without a restart or a redeploy.';

COMMENT ON TABLE edu_ai.document_type_taxonomy IS
    'Číselník of document types plus the instruction given to the classification LLM. '
    'Central control of classification: adding a type is one row, not a code change.';

COMMIT;
