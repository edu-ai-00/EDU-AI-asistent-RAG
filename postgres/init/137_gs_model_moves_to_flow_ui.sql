-- 137: extract_grade_subject.llm.model moves to the Langflow UI (2026-08-12).
-- (Renumbered from 135 — collision with kolega pro infrastrukturu's 135_query_log_turn_ids.)
-- Martin's rule: every Langflow component that calls a model defines that
-- model in the Langflow UI — the flow is where operators tune models, and a
-- DB-sourced model here was the single exception among 7 LLM-calling
-- components. The insert components now carry a gs_model UI input
-- (EDU_AI_LOAD_MARKDOWN, both instances); settings keeps only BEHAVIOUR:
-- enabled / trigger / batch_size / max_chunk_chars. Dropping the key rather
-- than leaving it: config nobody reads misleads the next reader (same
-- rationale as migration 136).

UPDATE edu_ai.settings
   SET config = config #- '{chunking,extract_grade_subject,llm,model}'
 WHERE config #> '{chunking,extract_grade_subject,llm,model}' IS NOT NULL;
