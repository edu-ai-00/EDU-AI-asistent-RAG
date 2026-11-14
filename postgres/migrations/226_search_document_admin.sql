-- 226: the search role may deactivate and permanently delete a school's documents.
--
-- The QA portal's Documents page (contracts/qa-portal/CONTRACT.md §14) deactivates,
-- activates and deletes documents through search's /internal/qa-admin/documents routes,
-- the same way it already deletes QA entries and packs. Until now edu_ai_search could only
-- read the four document tables, so those routes would fail with "permission denied".
-- A clean install gets the same privileges from postgres/role_contract/matrix.json.
--
-- Grants only: no data changes, no schema change (the schema fingerprint excludes privileges).
-- Undo: REVOKE UPDATE, DELETE ON the four tables FROM edu_ai_search (and the matrix entries).
--
-- Idempotent. No transaction control (runner, from 217).

GRANT DELETE ON edu_ai.document_chunks TO edu_ai_search;
GRANT DELETE ON edu_ai.document_elements TO edu_ai_search;
GRANT UPDATE, DELETE ON edu_ai.document_metadata TO edu_ai_search;
GRANT DELETE ON edu_ai.document_tables TO edu_ai_search;

DO $check$
BEGIN
    IF NOT (has_table_privilege('edu_ai_search', 'edu_ai.document_chunks', 'DELETE')
        AND has_table_privilege('edu_ai_search', 'edu_ai.document_elements', 'DELETE')
        AND has_table_privilege('edu_ai_search', 'edu_ai.document_metadata', 'UPDATE')
        AND has_table_privilege('edu_ai_search', 'edu_ai.document_metadata', 'DELETE')
        AND has_table_privilege('edu_ai_search', 'edu_ai.document_tables', 'DELETE')) THEN
        RAISE EXCEPTION '226: edu_ai_search did not get the document privileges';
    END IF;
END
$check$;
