-- 141: remove the orphaned chunk_grade_subject_extraction prompt (2026-08-18).
--
-- WHY IT IS GONE, so nobody a year from now reads this as an accident:
-- there were TWO implementations of the same job. The older one was a second LLM
-- pass inside the MD insert component, driven by this prompt. The newer one is
-- the enrichment BatchRun (prompt `chunk_enrichment`), which derives keywords,
-- grades, subjects and school level in ONE call. Since init 116 the insert pass
-- stood down whenever enrichment had run — which is always — so it had not
-- executed for months, and commit bc4079a removed it together with its gs_model
-- input. This prompt lost its only reader there.
--
-- GRADES AND SUBJECTS ARE STILL EXTRACTED. `chunk_enrichment` (v11) does it:
-- its TASK 2 — GRADES resolves enumerations, ranges, "první stupeň" -> 1..5,
-- pupil nouns ("deváťáci" -> 9), gymnasium years (prima -> 6) and maps SŠ years
-- onto the 10..13 axis. Deleting this row removes a duplicate specification, not
-- a capability.
--
-- Both versions go: v3 (init 106) and the active v4 (2026-08-06). No foreign key
-- references prompt_registry, so nothing else breaks. If a dedicated grade pass
-- is ever wanted again, `git show bc4079a^:langflow_flows/ingestion/EDU_AI_LOAD_MARKDOWN.json`
-- still holds the component, and this file's history holds the prompt.

DELETE FROM edu_ai.prompt_registry
 WHERE prompt_key = 'chunk_grade_subject_extraction';
