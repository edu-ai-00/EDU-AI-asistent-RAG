-- 227: the search role may change the validity dates of a school's documents.
--
-- The QA portal's Documents page (contracts/qa-portal/CONTRACT.md §14.8) edits valid_from
-- and valid_to through search's PATCH /internal/qa-admin/documents/{file_uuid}/validity.
-- The dates live on every version in all four document tables; edu_ai_search already
-- has UPDATE on document_metadata (226) but could only read or delete in the other three,
-- so the route would fail with "permission denied".
-- A clean install gets the same privileges from postgres/role_contract/matrix.json.
--
-- Grants only: no data changes, no schema change (the schema fingerprint excludes privileges).
-- Undo: REVOKE UPDATE ON document_chunks, document_elements, document_tables FROM
-- edu_ai_search (and the matrix entries).
--
-- Idempotent. No transaction control (runner, from 217).

GRANT UPDATE ON edu_ai.document_chunks TO edu_ai_search;
GRANT UPDATE ON edu_ai.document_elements TO edu_ai_search;
GRANT UPDATE ON edu_ai.document_tables TO edu_ai_search;

DO $check$
BEGIN
    IF NOT (has_table_privilege('edu_ai_search', 'edu_ai.document_chunks', 'UPDATE')
        AND has_table_privilege('edu_ai_search', 'edu_ai.document_elements', 'UPDATE')
        AND has_table_privilege('edu_ai_search', 'edu_ai.document_tables', 'UPDATE')) THEN
        RAISE EXCEPTION '227: edu_ai_search did not get UPDATE on the document tables';
    END IF;
END
$check$;
