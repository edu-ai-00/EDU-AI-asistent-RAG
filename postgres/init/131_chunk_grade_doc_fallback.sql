-- 131: document-declared grades as chunk fallback (2026-08-10, Martin).
-- When the uploader DECLARED grades on the document (school_grade_user) and
-- the chunk ends up with none (enrichment found nothing to anchor on), the
-- chunk inherits the document's declaration — a human statement about the
-- whole document beats an empty LLM result. The LLM's own non-empty grades
-- always win (the trigger only fires on an EMPTY chunk value, including the
-- enrichment UPDATE that writes '{}'). school_level is derived from the
-- inherited codes when the chunk carries none (1-9/9999 -> ZS, 10-13 -> SS).
-- Same philosophy as the taxonomy_binding audience fallback in answer.py.

CREATE OR REPLACE FUNCTION edu_ai.chunk_grade_doc_fallback() RETURNS trigger AS $$
DECLARE
  doc_grades bigint[];
BEGIN
  IF NEW.school_grade_ids IS NULL OR cardinality(NEW.school_grade_ids) = 0 THEN
    SELECT m.school_grade_user INTO doc_grades
    FROM edu_ai.document_metadata m
    WHERE m.file_uuid = NEW.file_uuid
      AND m.processing_uuid = NEW.processing_uuid;
    IF doc_grades IS NOT NULL AND cardinality(doc_grades) > 0 THEN
      NEW.school_grade_ids := doc_grades;
      IF NEW.school_level IS NULL OR NEW.school_level = '' THEN
        SELECT CASE
                 WHEN bool_and(cl.code ~ '^[1-9]$' OR cl.code = '9999') THEN 'ZS'
                 WHEN bool_and(cl.code ~ '^1[0-3]$') THEN 'SS'
               END
          INTO NEW.school_level
          FROM edu_ai.code_list cl
         WHERE cl.id = ANY(doc_grades);
      END IF;
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS chunk_grade_doc_fallback ON edu_ai.document_chunks;
CREATE TRIGGER chunk_grade_doc_fallback
  BEFORE INSERT OR UPDATE OF school_grade_ids ON edu_ai.document_chunks
  FOR EACH ROW EXECUTE FUNCTION edu_ai.chunk_grade_doc_fallback();
