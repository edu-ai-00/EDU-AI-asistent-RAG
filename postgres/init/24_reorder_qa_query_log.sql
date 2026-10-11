-- last change in code by Martin Safranek (2026-07-23).
-- 24_reorder_qa_query_log.sql
-- Cosmetic: recreate qa_query_log with columns in a logical reading order
-- (stem_query moved next to norm/lemma; answer_source_text/strategies_text moved
-- next to their raw counterparts). Postgres can't reorder columns in place.
-- Preserves data, the IDENTITY generator + its value, the PK, all 4 secondary
-- indexes, both FKs (setting_id out, qa_feedback.log_id in) and the
-- qa_conversation_v view. Transactional — all-or-nothing.

BEGIN;

-- dependents that block a table swap
DROP VIEW IF EXISTS edu_ai.qa_conversation_v;
ALTER TABLE edu_ai.qa_feedback DROP CONSTRAINT IF EXISTS qa_feedback_log_id_fkey;

-- new table in the desired column order
CREATE TABLE edu_ai.qa_query_log_new (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at         timestamptz NOT NULL DEFAULT now(),
    conversation_id    uuid,
    session_id         text,
    user_id            text,
    user_school_grade  integer,
    channel            text,
    raw_query          text NOT NULL,
    norm_query         text,
    lemma_query        text,
    stem_query         text,
    stage              text,
    answer_source      text,
    answer_source_text text,
    strategies         text[],
    strategies_text    text,
    matched_entry_id   uuid,
    match_score        real,
    answer_md          text,
    sources            jsonb,
    answer_language    text,
    latency_ms         integer,
    setting_id         uuid
);

-- copy data (explicit id → OVERRIDING SYSTEM VALUE for GENERATED ALWAYS)
INSERT INTO edu_ai.qa_query_log_new
    (id, created_at, conversation_id, session_id, user_id, user_school_grade, channel,
     raw_query, norm_query, lemma_query, stem_query, stage, answer_source, answer_source_text,
     strategies, strategies_text, matched_entry_id, match_score, answer_md, sources,
     answer_language, latency_ms, setting_id)
OVERRIDING SYSTEM VALUE
SELECT
     id, created_at, conversation_id, session_id, user_id, user_school_grade, channel,
     raw_query, norm_query, lemma_query, stem_query, stage, answer_source, answer_source_text,
     strategies, strategies_text, matched_entry_id, match_score, answer_md, sources,
     answer_language, latency_ms, setting_id
FROM edu_ai.qa_query_log;

-- swap
DROP TABLE edu_ai.qa_query_log;
ALTER TABLE edu_ai.qa_query_log_new RENAME TO qa_query_log;

-- resume the identity above the highest copied id (is_called = there were rows)
SELECT setval(
    pg_get_serial_sequence('edu_ai.qa_query_log', 'id'),
    coalesce((SELECT max(id) FROM edu_ai.qa_query_log), 1),
    (SELECT max(id) IS NOT NULL FROM edu_ai.qa_query_log)
);

-- secondary indexes (PK is inline)
CREATE INDEX qa_query_log_conv_idx    ON edu_ai.qa_query_log USING btree (conversation_id, created_at);
CREATE INDEX qa_query_log_created_idx ON edu_ai.qa_query_log USING btree (created_at);
CREATE INDEX qa_query_log_source_idx  ON edu_ai.qa_query_log USING btree (answer_source, stage);
CREATE INDEX qa_query_log_setting_idx ON edu_ai.qa_query_log USING btree (setting_id);

-- foreign keys (out + in)
ALTER TABLE edu_ai.qa_query_log
    ADD CONSTRAINT qa_query_log_setting_id_fkey
    FOREIGN KEY (setting_id) REFERENCES edu_ai.search_setting (setting_id) ON UPDATE CASCADE;
ALTER TABLE edu_ai.qa_feedback
    ADD CONSTRAINT qa_feedback_log_id_fkey
    FOREIGN KEY (log_id) REFERENCES edu_ai.qa_query_log (id);

-- view (identical definition)
CREATE VIEW edu_ai.qa_conversation_v AS
 SELECT conversation_id,
        min(created_at) AS started_at,
        max(created_at) AS last_activity_at,
        count(*)        AS turn_count,
        max(user_id)    AS user_id,
        max(user_school_grade) AS user_school_grade
   FROM edu_ai.qa_query_log
  WHERE conversation_id IS NOT NULL
  GROUP BY conversation_id;

COMMIT;
