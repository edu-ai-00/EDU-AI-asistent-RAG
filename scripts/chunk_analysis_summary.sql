-- last change in code by Martin Safranek (2026-08-23).
--
-- Chunk quality per document: what is too short, what carries no audience, what
-- gets truncated before the judge. Without a WHERE clause it sweeps the corpus.
SELECT m.file_uuid                                           AS file_uuid,
       regexp_replace(coalesce(m.source_url, m.original_file_name, '?'),
                      '^https?://(www\.)?', '')               AS document,
       count(*)                                               AS chunks,
       round(avg(length(trim(coalesce(c.chunk_text, ''))))) AS avg_len,
       min(length(trim(coalesce(c.chunk_text, ''))))          AS min_len,
       max(length(trim(coalesce(c.chunk_text, ''))))          AS max_len,
       count(*) FILTER (WHERE length(trim(coalesce(c.chunk_text, ''))) < 60)  AS under_60_chars,
       count(*) FILTER (WHERE trim(coalesce(c.chunk_text, ''))
                              IN (trim(coalesce(c.section_heading, '')),
                                  trim(coalesce(c.subsection_heading, ''))))  AS heading_only,
       count(*) FILTER (WHERE length(trim(coalesce(c.chunk_text, ''))) > 900) AS truncated_for_judge,
       count(*) FILTER (WHERE cardinality(coalesce(c.school_grade_ids,
                                                   ARRAY[]::bigint[])) = 0)   AS no_grade,
       count(*) FILTER (WHERE c.school_level IS NULL)                         AS no_level,
       count(*) FILTER (WHERE c.embedding IS NULL)                            AS no_embedding,
       count(*) FILTER (WHERE coalesce(array_length(c.keywords, 1), 0) = 0)   AS no_keywords
  FROM edu_ai.document_chunks c
  JOIN edu_ai.document_metadata m
    ON c.file_uuid = m.file_uuid AND c.processing_uuid = m.processing_uuid
 WHERE m.is_active
 GROUP BY 1, 2
 ORDER BY under_60_chars DESC, chunks DESC;
