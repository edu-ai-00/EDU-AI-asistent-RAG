-- 213: question_answer_log.domains — WHICH thematic areas a turn searched in
-- (2026-09-06, spec.search-domain-scope §7).
--
-- The domain filter changes which sources could answer, so a log row without it
-- cannot be read: two turns with the same question, entity, role and ročník can
-- legitimately produce different answers, and nothing in the table said why.
-- It is also the column the server READS BACK — resolve_context and
-- conversation.load_seen filter earlier turns by the current scope, so a
-- follow-up asked in "Docházka" does not inherit a turn asked in "Přijímačky".
--
-- text[] of CANONICAL DOMAIN CODES, not taxonomy_rules ids. The ids differ per
-- entity for one and the same code (production: SECONDARY_ADMISSIONS is id 13
-- in the ZŠ catalog and id 42 globally), so an id column could not be compared
-- across turns without re-resolving it, and a re-tagged catalog would silently
-- change what a historical row meant. The code is stable and is what the client
-- sent.
--
-- NULL, never '{}': NULL is the whole meaning of "no thematic restriction", and
-- it is the value every existing row and every client without the new field
-- writes. A DEFAULT '{}' would make "unrestricted" indistinguishable from
-- "explicitly empty" — which the API rejects as a 422 and can therefore never
-- reach this table — and would rewrite the meaning of 3 000 historical rows.
-- No backfill for the same reason: the area a 2026-08 question was asked in is
-- not recoverable from its text, and guessing it would poison the very
-- comparison this column exists for.
--
-- No index yet. The read-back queries filter conversation_id first (an indexed
-- equality on a uuid), which leaves a handful of rows for the array predicate;
-- spec §7 defers an index until a measurement asks for one.

BEGIN;

-- IF NOT EXISTS alone does not prove the column is the RIGHT column: a
-- re-created table, a hand-patched database or an earlier attempt could leave a
-- `domains` of another type, and every INSERT would then fail at runtime with
-- the migration reporting success. So: add it when it is missing, and refuse
-- loudly when what is there is not text[].
ALTER TABLE edu_ai.question_answer_log
    ADD COLUMN IF NOT EXISTS domains text[];

DO $$
DECLARE
    v_type text;
    v_default text;
BEGIN
    SELECT format_type(a.atttypid, a.atttypmod),
           pg_get_expr(d.adbin, d.adrelid)
      INTO v_type, v_default
      FROM pg_attribute a
      LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
     WHERE a.attrelid = 'edu_ai.question_answer_log'::regclass
       AND a.attname  = 'domains'
       AND a.attnum > 0 AND NOT a.attisdropped;

    IF v_type IS NULL THEN
        RAISE EXCEPTION '213: question_answer_log.domains was not created';
    END IF;
    IF v_type <> 'text[]' THEN
        RAISE EXCEPTION '213: question_answer_log.domains is % — expected text[]. '
                        'A column of another type is not the one search writes; '
                        'inspect it and convert or drop it deliberately.', v_type;
    END IF;
    IF v_default IS NOT NULL THEN
        RAISE EXCEPTION '213: question_answer_log.domains carries DEFAULT % — the '
                        'column must default to NULL, which is what "no thematic '
                        'restriction" means.', v_default;
    END IF;
END $$;

COMMENT ON COLUMN edu_ai.question_answer_log.domains IS
  'Canonical DOMAIN codes (taxonomy_rules.type_code, trimmed/upper-cased/sorted) the turn was restricted to. NULL = no thematic restriction, which is also every row written before migration 213 and by any client that sends no `domains`. Codes and not ids: an id is per-entity and would not compare across turns. Read back by search/app/nodes/resolve_context.py and search/app/conversation.py so a follow-up stays inside the area it was asked in.';

COMMIT;
