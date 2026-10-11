-- last change in code by Martin Safranek (2026-08-02).
-- 68_clean_heading_markdown.sql
-- Strip markdown emphasis from heading text already in the table.
--
-- The chunker keeps markup out of headings from now on (_clean_heading), but the
-- chunks stored before that carry it: 123 of 465 rows had section_heading or
-- subsection_heading holding the asterisks, e.g.
--
--     **8. Pravidla, povolené pomůcky a den D**
--     **1. Jak si zvolit obor**
--
-- The heading text is not decoration. It is prepended to chunk_embed_text and it
-- is handed to the answering model as "| section: …", so the markup reaches the
-- reader: with the per-document lead-in now rendered in bold, an answer began
-- literally "**8. Pravidla".
--
-- Retrieval is NOT affected and this is not a re-ingest. Measured before running:
-- 0 rows have an asterisk in any of the six reduced columns (_norm/_lemma/_stem) —
-- normalisation drops punctuation, so the tsvectors and the coverage gate never
-- saw the markers. Only the display/prompt text is wrong, which is why it can be
-- repaired in place instead of re-ingesting every document.
--
-- chunk_text is deliberately left alone: there the asterisks are part of the
-- document as written (126 rows), and rewriting a body would change what the
-- corpus says.
--
-- Emphasis markers only (** __ * _). Ordinal periods, dashes and years stay.
--
-- Idempotent (only rows still holding a marker are touched); transactional.

BEGIN;

UPDATE edu_ai.edu_ai_document_chunks
   SET section_heading = nullif(btrim(
           regexp_replace(section_heading, '(\*\*|__|\*|_)', '', 'g')), ''),
       subsection_heading = nullif(btrim(
           regexp_replace(subsection_heading, '(\*\*|__|\*|_)', '', 'g')), '')
 WHERE section_heading ~ '(\*|_)'
    OR subsection_heading ~ '(\*|_)';

COMMIT;
