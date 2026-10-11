-- last change in code by Martin Safranek (2026-08-02).
-- 84_drop_private_public_flag.sql
-- private_public_flag goes; `visibility` replaced it (migration 78).
--
-- It was never enforced. Search read it in exactly ZERO places for its whole
-- life, so a document marked private was returned to anyone — the flag recorded
-- an intention nothing acted on. It also could not express what was needed: two
-- values, in a world that now has three (private / entity / public), and in the
-- single-tenant past its 'public' meant "everyone in this school", which is
-- 'entity' here, not 'public'.
--
-- Safe to drop now, and the data says so: after the flow components switched to
-- `visibility`, every row kept the column's DEFAULT 'private' while carrying
-- visibility='entity' — nothing writes it any more.
--
-- Also gone from api/app/main.py (the form field and the derived value),
-- scripts/smoke.py and all six flows.
--
-- Idempotent; transactional.

BEGIN;

ALTER TABLE edu_ai.document_metadata DROP COLUMN IF EXISTS private_public_flag;
ALTER TABLE edu_ai.document_tables   DROP COLUMN IF EXISTS private_public_flag;

COMMIT;
