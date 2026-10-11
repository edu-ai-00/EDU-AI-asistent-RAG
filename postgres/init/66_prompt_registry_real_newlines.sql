-- last change in code by Martin Safranek (2026-08-01).
-- 66_prompt_registry_real_newlines.sql
-- Every prompt in the registry was stored as ONE line with the two characters
-- backslash-n where a line break belongs.
--
-- Measured before this migration — real newlines vs literal "\n" sequences:
--
--     document_metadata_extraction   0    30
--     chunk_keyword_extraction       0    93
--     markdown_chunker               0     2
--     pdf_extraction_judge           0    19
--     search_answer_generation v1    0    54
--     search_answer_generation v2    0    75
--     table_metadata_extraction      0   181
--     table_metadata_validator       0    19
--     web_extraction_judge           0    26
--
-- Not one real line break in the whole registry. The escapes survived from the
-- source the prompts were seeded from (a JSON/Python string that was never
-- unescaped), and since prompt_text goes to the model verbatim, every LLM in the
-- system — answer generation, document metadata, chunk keywords, table analysis,
-- both extraction judges — has been reading its instructions as a single
-- paragraph with "\n" printed inside it.
--
-- What it costs. Structure is how a prompt carries priority: numbered rules,
-- "---" section separators, a "RULES" heading. Flattened into one line, all of it
-- reads as prose. It showed up when search_answer_generation v2 asked for one
-- paragraph per document (migration 65) and gpt-4.1-nano ignored the numbered
-- rules and blended two documents anyway — the rules were not visible AS rules.
--
-- Every occurrence was inspected before this ran: all of them sit at paragraph or
-- list boundaries ("…platform.\n\nYour job is…", "- Completeness…\n- …"). None is
-- an escaped newline inside a JSON example, which is the one case where the
-- literal text would have been intended.
--
-- Idempotent (only rows that still contain the literal sequence are touched);
-- transactional.

BEGIN;

UPDATE edu_ai.prompt_registry
   SET prompt_text = replace(prompt_text, chr(92) || 'n', chr(10))
 WHERE prompt_text LIKE '%' || chr(92) || 'n%';

COMMIT;
