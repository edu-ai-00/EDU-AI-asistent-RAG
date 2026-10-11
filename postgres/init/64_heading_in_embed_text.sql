-- last change in code by Martin Safranek (2026-08-01).
-- 64_heading_in_embed_text.sql
-- The chunk's headings go into the text the VECTOR is computed from.
--
-- chunk_embed_text is a working value: it is what gets embedded and it is never
-- stored. chunk_text — what the database keeps and what the lexical index is built
-- from — is not touched by this.
--
-- The problem. The body of chunk 1 of the supply list reads
--
--     **Třída – 7.**
--     4x 544, 2x 460, 1x 540, 9x 564, …
--
-- which means "a list of codes concerning the 7th grade" and nowhere says
-- SUPPLIES. That word exists in this corpus in five chunk HEADINGS and in no chunk
-- body at all, and the heading was not part of the embedded text — so the vector
-- pointed at "an enumeration of goods" instead of "school supplies".
--
-- It shows up on the one query that has nothing else to go on: "Jaké pomůcky
-- potřebuje prvňáček?". The word "prvňáček" appears in no document (they say
-- "1. třída"), so no lexical form can match it and only meaning can bridge the two.
-- The best cosine in the whole corpus was 0.378 against a 0.45 floor, so rag_vector
-- contributed nothing — the step that exists for exactly this case was cut off
-- before it could help.
--
-- Why duplication is free here and not in chunk_text: this text is not stored and
-- not lexically indexed, so no index grows and ts_rank does not move. Copying
-- headings into chunk_text is what produced six near-copies of one section
-- (bullet_blocks.repeat_head) and that stays off.
--
-- A heading already present in the body is not repeated, so the FIRST chunk of a
-- section — the one that keeps its own "## …" line since migration 62 — is left
-- alone while its siblings get the context they lack.
--
-- Takes effect on the next ingest: vectors are computed when a chunk is stored.
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.search_setting s
   SET config = jsonb_set(
         s.config, '{chunking,split_text_into_chunks,headings}',
         coalesce(s.config->'chunking'->'split_text_into_chunks'->'headings',
                  '{}'::jsonb)
         || jsonb_build_object('in_embed_text', true),
         true)
 WHERE s.config ? 'chunking'
   AND NOT (s.config->'chunking'->'split_text_into_chunks'->'headings'
            ? 'in_embed_text');

COMMIT;
