-- last change in code by Martin Safranek (2026-08-05).
-- 127_audience_roles.sql
-- Audience ROLE (produktový vlastník, 2026-08-05): a document is DECLARED at upload to be
-- for one or more of STUDENT / TEACHER / PARENT (>=1). At search the caller
-- states their role (mandatory, validated); a document is visible only if its
-- audience includes that role — a HARD filter, applied with the visibility
-- scope. Role is a THIRD orthogonal axis: visibility = which org nodes,
-- grade = which grade, role = which audience.
--
-- role is a PLATFORM registry (code_list, legal_entity_code NULL) like
-- 'language', not a per-school list — resolved_code_list is taught to serve
-- it globally. Readable codes, NOT UUIDs: the picker is served to every
-- client, so an opaque id adds no security (real access control = verifying
-- the caller's role, D8), only unreadable logs/config.
--
-- Idempotent; transactional.

BEGIN;

-- 1) resolved_code_list: 'role' is global (NULL owner), like 'language'
CREATE OR REPLACE FUNCTION edu_ai.resolved_code_list(p_entity text, p_list text)
RETURNS SETOF edu_ai.code_list LANGUAGE sql STABLE AS $$
    WITH RECURSIVE up AS (
        SELECT legal_entity_code, parent_code, 0 AS depth
        FROM edu_ai.legal_entity WHERE legal_entity_code = p_entity
        UNION ALL
        SELECT l.legal_entity_code, l.parent_code, up.depth + 1
        FROM edu_ai.legal_entity l JOIN up ON l.legal_entity_code = up.parent_code
    ), owner AS (
        SELECT u.legal_entity_code
        FROM up u
        WHERE EXISTS (SELECT 1 FROM edu_ai.code_list c
                      WHERE c.is_active AND c.list_code = p_list
                        AND c.legal_entity_code = u.legal_entity_code)
        ORDER BY u.depth
        LIMIT 1
    )
    SELECT c.* FROM edu_ai.code_list c
    WHERE c.is_active AND c.list_code = p_list
      AND c.legal_entity_code IS NOT DISTINCT FROM (
          CASE
              WHEN p_list = 'role' THEN NULL              -- platform registry
              WHEN p_list = 'language' THEN
                   (SELECT legal_entity_code FROM owner)  -- selection-with-default
              ELSE
                   COALESCE((SELECT legal_entity_code FROM owner), '(nobody)')
          END)
$$;

-- 2) the three roles, global, four languages
INSERT INTO edu_ai.code_list
    (list_code, code, sort_order, is_active, created_by, is_ui_default,
     is_processing_fallback, labels, notes, legal_entity_code)
SELECT 'role', v.code, v.ord, true, 'init-127', (v.code = 'STUDENT'), false,
       v.labels::jsonb, 'audience role (produktový vlastník 2026-08-05)', NULL
FROM (VALUES
    ('STUDENT', 1, '{"cs":"Student","en":"Student","de":"Schüler","es":"Estudiante"}'),
    ('TEACHER', 2, '{"cs":"Učitel","en":"Teacher","de":"Lehrer","es":"Profesor"}'),
    ('PARENT',  3, '{"cs":"Rodič","en":"Parent","de":"Elternteil","es":"Padre"}')
) AS v(code, ord, labels)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.code_list
                  WHERE list_code = 'role' AND code = v.code
                    AND legal_entity_code IS NULL);

-- 3) audience_roles on the document — text[] of role codes, >=1 by contract
--    (enforced at the API). NULL is a DEFECT, not a default: the search role
--    filter treats an untagged row as invisible (no legacy fallback).
ALTER TABLE edu_ai.document_metadata
    ADD COLUMN IF NOT EXISTS audience_roles text[];

-- GIN index for the && overlap filter on the search path
CREATE INDEX IF NOT EXISTS metadata_audience_roles_idx
    ON edu_ai.document_metadata USING gin (audience_roles);

-- DENORMALISED onto the chunk too — the role filter must apply DURING the scan
-- (rag_vector orders by distance before any metadata join), exactly like
-- visibility. NULL = not declared -> visible (scope.where treats it so), so the
-- filter is a no-op until ingest starts stamping real audiences onto chunks.
ALTER TABLE edu_ai.document_chunks
    ADD COLUMN IF NOT EXISTS audience_roles text[];
CREATE INDEX IF NOT EXISTS chunks_audience_roles_idx
    ON edu_ai.document_chunks USING gin (audience_roles);

-- QA belongs under the organisation exactly like documents (same
-- legal_entity_code + visibility), and the role filter must reach it too —
-- "při jakémkoliv hledání v tabulce chunků" includes the QA retrieval. The
-- role is denormalised onto the tables scope.where scans (qa_variants,
-- qa_embedding) and kept on qa_entries as the source of truth. STRICT like
-- documents: an untagged QA row is invisible to a role-stated caller.
ALTER TABLE edu_ai.qa_entries   ADD COLUMN IF NOT EXISTS audience_roles text[];
ALTER TABLE edu_ai.qa_variants  ADD COLUMN IF NOT EXISTS audience_roles text[];
ALTER TABLE edu_ai.qa_embedding ADD COLUMN IF NOT EXISTS audience_roles text[];
CREATE INDEX IF NOT EXISTS qa_variants_audience_roles_idx
    ON edu_ai.qa_variants USING gin (audience_roles);
CREATE INDEX IF NOT EXISTS qa_embedding_audience_roles_idx
    ON edu_ai.qa_embedding USING gin (audience_roles);

-- (No backfill: legacy compatibility is explicitly NOT wanted — an untagged
--  document is invisible to the role filter, not visible-to-all. Documents
--  without a declared audience are removed / re-ingested, never defaulted.)

-- 5) search requires a role — GLOBAL policy (like search_requires_user, init
--    99): the caller must state a valid role or /search answers "we don't
--    know your role". One value for the whole platform; kept on the global row.
UPDATE edu_ai.settings
   SET config = jsonb_set(config, '{visibility,search_requires_role}', 'true'::jsonb)
 WHERE is_active AND legal_entity_code IS NULL;

-- 6) role_required message (4 languages)
INSERT INTO edu_ai.system_message (message_key, lang, message, created_by, notes)
SELECT 'role_required', v.l, v.m, 'init-127', 'search: caller role missing/invalid'
FROM (VALUES
  ('cs', 'Neznáme tvoji roli. Řekni prosím, zda jsi student, učitel, nebo rodič — podle toho ti ukážu ty správné informace.'),
  ('en', 'We don''t know your role. Please tell us whether you are a student, a teacher, or a parent — that decides which information you see.'),
  ('de', 'Wir kennen deine Rolle nicht. Bitte sag uns, ob du Schüler, Lehrer oder Elternteil bist — danach richten sich die angezeigten Informationen.'),
  ('es', 'No conocemos tu rol. Indícanos si eres estudiante, profesor o padre — eso determina qué información verás.')
) AS v(l, m)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.system_message
                  WHERE message_key = 'role_required' AND lang = v.l);

COMMIT;
