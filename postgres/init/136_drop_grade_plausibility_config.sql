-- 136: drop the dead chunking.grade_plausibility config block (2026-08-12).
-- (Renumbered from 134: kolega pro infrastrukturu's 134_idempotency_key / 135_query_log_turn_ids
--  landed on the shared branch first; these three shift to 136-138.)
-- Seam audit: the block sat on the GLOBAL settings row with enabled=true and
-- a ZS/grade-9 rule — and NOTHING reads it: no flow component, no search node,
-- no script (verified by scanning all 193 flow components and the whole
-- codebase). Config that promises behaviour which does not exist is worse
-- than a missing feature: the next reader trusts it. Martin's call: delete;
-- if the plausibility guard is ever wanted, it returns TOGETHER with its
-- implementation in the insert component.

UPDATE edu_ai.settings
   SET config = config #- '{chunking,grade_plausibility}'
 WHERE config #> '{chunking,grade_plausibility}' IS NOT NULL;
