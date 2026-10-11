-- 205: suggestions_offered on question_answer_log (2026-09-03, Jan).
-- WHICH chips a turn actually showed. Everything else needed to stop a chat
-- offering the same question twice is already in this table — conversation_id
-- groups the turns, raw_query says what was ASKED (a clicked chip arrives as the
-- next turn's raw_query, so "clicked" needs nothing extra) — and this is the one
-- missing half: what was OFFERED and ignored.
--
-- A column and not a table. The row is written once per turn either way, so this
-- costs no extra write, no second retention rule and no lifecycle of its own;
-- a side table keyed by conversation_id would duplicate the grouping this table
-- already has and would need someone to decide who deletes from it.
--
-- Nullable and additive: an older writer, the eval harness and any turn logged
-- while suggestions were empty all leave it NULL, which reads as "nothing was
-- offered" — the same conclusion, so no backfill.
--
-- Shape: the suggestions array exactly as the client received it
-- ([{kind, question, entry_id, score}, …], contracts/schemas/search-response
-- .schema.json). Stored whole rather than as bare question strings so that
-- "which kind do people come back to" stays answerable later.

ALTER TABLE edu_ai.question_answer_log
    ADD COLUMN IF NOT EXISTS suggestions_offered jsonb;

COMMENT ON COLUMN edu_ai.question_answer_log.suggestions_offered IS
  'The suggestions this turn returned to the client, verbatim ([{kind, question, entry_id, score}, …]). NULL = none offered or an older writer. Read back within a conversation so a later turn does not offer the same question again; see search/app/conversation.py.';
