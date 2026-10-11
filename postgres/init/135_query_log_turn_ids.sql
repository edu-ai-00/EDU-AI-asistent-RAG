-- 135: turn_seq, query_id, selected_answer_key on question_answer_log (2026-08-13, Jan).
-- The client-side identifiers of ONE question, from the QA webchat contract
-- conversation_id already groups a conversation;
-- these say WHERE in it a row sits and WHICH client-side question it was, so a
-- report can be traced from the app's own log to this table without matching on
-- question text.
--
-- All three are nullable and nothing reads them yet — the row is the point. Adding
-- the columns with the fields, rather than later, is what keeps the fields from
-- being accepted and silently dropped.
--
-- selected_answer_key is the exception worth spelling out: the request field is
-- accepted and STORED but NOT yet honoured by the cascade, which still re-matches
-- on the question text. Storing it is how that gets measured — the column shows
-- what the client asked for next to matched_entry_id, which is what it actually
-- got, so the day the cascade starts honouring it there is a before/after to check
-- rather than a hope.

ALTER TABLE edu_ai.question_answer_log
    ADD COLUMN IF NOT EXISTS turn_seq            integer,
    ADD COLUMN IF NOT EXISTS query_id            text,
    ADD COLUMN IF NOT EXISTS selected_answer_key text;

COMMENT ON COLUMN edu_ai.question_answer_log.turn_seq IS
  'Client-side position of this question within conversation_id (1..N). Advisory: the client owns it, the server only records it.';

COMMENT ON COLUMN edu_ai.question_answer_log.query_id IS
  'Client-side id of one question, for tracing a report from the app''s log to this row. Also the value a client is expected to send as Idempotency-Key, though the server treats that header as opaque and does not require the two to match.';

COMMENT ON COLUMN edu_ai.question_answer_log.selected_answer_key IS
  'The suggestion the user picked, as sent by the client. ACCEPTED AND STORED BUT NOT HONOURED — the cascade still re-matches on question text. Compare against matched_entry_id to see how often the two would disagree.';
