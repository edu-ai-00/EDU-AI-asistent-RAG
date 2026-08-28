-- last change in code by Martin Safranek (2026-08-23).
--
-- Chunks of one document, in the order the document reads.
--
-- ORDER: output_row_number is 1:1 with file_item_uuid (one element, one value),
-- so the document's reading order is (output_row_number, chunk_index).
-- Do NOT order by id: id is insertion order, which does not follow the document
-- when the ingestion flow runs branches in parallel.
--
-- Pick the document in the WHERE clause (source_url or original_file_name).
SELECT c.file_uuid                                           AS file_uuid,
       c.file_item_uuid                                      AS file_item_uuid,
       c.output_row_number                                   AS element,
       c.chunk_index                                         AS ci,
       c.id                                                  AS chunk_id,
       length(trim(coalesce(c.chunk_text, '')))              AS len,
       CASE
         WHEN length(trim(coalesce(c.chunk_text, ''))) = 0                     THEN 'EMPTY'
         WHEN trim(coalesce(c.chunk_text, '')) IN (trim(coalesce(c.section_heading, '')),
                                                   trim(coalesce(c.subsection_heading, ''))) THEN 'HEADING ONLY'
         WHEN length(trim(coalesce(c.chunk_text, ''))) < 60                    THEN 'SHORT'
         WHEN length(trim(coalesce(c.chunk_text, ''))) > 900                   THEN 'TRUNCATED FOR THE JUDGE'
         ELSE ''
       END                                                   AS flag,
       coalesce(c.school_level, '-')                         AS level,
       coalesce((SELECT string_agg(cl.code, ',' ORDER BY cl.code::text)
                   FROM edu_ai.code_list cl
                  WHERE cl.id = ANY(c.school_grade_ids)), '-') AS grades,
       coalesce(c.owning_entity_code, '-')                   AS owner,
       coalesce(array_length(c.keywords, 1), 0)              AS keywords,
       (c.embedding IS NOT NULL)                             AS has_embedding,
       coalesce(c.langdetect_result, '-')                     AS lang,
       left(coalesce(c.heading_path, ''), 70)                AS heading_path,
       left(replace(coalesce(c.chunk_text, ''), E'\n', ' '), 90) AS sample
  FROM edu_ai.document_chunks c
  JOIN edu_ai.document_metadata m
    ON c.file_uuid = m.file_uuid AND c.processing_uuid = m.processing_uuid
 WHERE m.is_active
   AND m.source_url LIKE '%skolni_rad%'      -- <<< the document goes here
 ORDER BY c.output_row_number, c.chunk_index;
