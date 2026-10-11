-- last change in code by Martin Safranek (2026-07-26).
-- 27_search_answer_config.sql
-- Move the answer-LLM behavior from code/env into DB configuration:
--   1. the system prompt of the terminal `answer` node → edu_ai.prompt_registry
--      (key `search_answer_generation`) — the search service reads it PER REQUEST
--      (DB > code fallback), so prompt edits apply immediately, no restart.
--      This closes the one exception to the "all LLM prompts live in
--      prompt_registry" rule (it was code-only in answer_generator.py).
--   2. model + max_tokens → params of the `answer` step (pipeline index 7) in
--      the active search_setting (v5) — same config-driven pattern as the
--      qa_vector/rag_vector gates. NOTE: search_setting is cached in-process →
--      changing these needs `docker restart edu_ai_search`; the prompt does not.
-- Idempotent: prompt inserted only if the key does not exist; params set only
-- if the answer step has none. Transactional.

BEGIN;

INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
SELECT
    'search_answer_generation', 1, $PROMPT$You are an answer generation agent for an educational AI platform.

Your job is to answer the user's question using ONLY the provided context.

---

INPUT FORMAT

You receive a message containing:
- Context: retrieved document chunks (markdown formatted)
- Sources: JSON array with file_uuid, file_item_uuid, file_name, rank
- Question: the user's original query

---

RULES

1. Answer ONLY from the provided context
2. Do NOT use external knowledge
3. Do NOT hallucinate or invent information
4. If the context does not contain enough information to answer:
   → say so clearly
   → suggest what the user could search for instead
5. Be concise but thorough
6. LANGUAGE RULE (CRITICAL):
   → Always respond in the SAME LANGUAGE as the user's question
   → If the question is in English → answer in English (even if context is in Czech or other language)
   → If the question is in Czech → answer in Czech (even if context is in English)
   → NEVER let the language of the context documents override the language of the question

---

SOURCE CITATION

1. Write a clean, self-contained answer. Do NOT append a "Sources" section and do
   NOT print file names, file_uuid values or "(Document N)" tags — the application
   shows the source documents as clickable links below your answer.
2. If it aids clarity you may refer to a source naturally in prose (e.g. "podle
   metodického materiálu…"), but never as a technical citation.
3. Only rely on documents actually present in the context.

---

OUTPUT FORMAT

[Your grounded answer only, in the user's language — NO Sources section, NO
file names, NO uuids.]

---

LENGTH

Be concise. Answer in the language of the question, in at most ~120 words
(a short paragraph or a compact list). Do not restate the question or
summarize the context; give the answer directly.$PROMPT$,
    true, 'search:answer',
    'System prompt of the terminal answer node (the search cascade''s only LLM). Read per request — edits apply without restart.',
    ARRAY[]::text[], 'gpt-4.1-mini', 'multi', 'migration-27',
    'Includes the LENGTH (brevity) section from the wave-1 latency work: shorter answers = faster answers; max_tokens on the answer step is the hard stop.'
WHERE NOT EXISTS (
    SELECT 1 FROM edu_ai.prompt_registry WHERE prompt_key = 'search_answer_generation'
);

-- answer step = pipeline[7] in the active v5 config
UPDATE edu_ai.search_setting
SET config = jsonb_set(
        config, '{pipeline,7,params}',
        '{"model": "gpt-4.1-mini", "max_tokens": 350}'::jsonb, true)
WHERE label LIKE 'v5:%'
  AND config->'pipeline'->7->>'step' = 'answer'
  AND NOT (config->'pipeline'->7 ? 'params');

COMMIT;
