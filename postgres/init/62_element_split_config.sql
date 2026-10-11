-- last change in code by Martin Safranek (2026-08-01).
-- 62_element_split_config.sql
-- The document's two division levels become configuration.
--
-- The rule the system now follows explicitly:
--
--   level 1  a heading divides the document into ELEMENTS (file_item_uuid)
--            — MarkdownSectionSplitter ("Split Markdown into Pages")
--   level 2  a heading inside an element divides it into CHUNKS
--            — DataFrameRecursiveTextSplitter ("Split Text into Chunks")
--
-- Level 1 already worked this way but its parameters were reachable only in the
-- flow JSON, so the boundary that decides file_item_uuid — the coarsest, most
-- persistent decision in the whole index — was the one thing that could not be
-- changed as data. It joins the config here as
-- `chunking.split_markdown_into_pages`. Every component parameter is EMPTIED in
-- the flow at the same time: an IntInput/BoolInput is always "filled" and would
-- shadow this config forever (resolution order: a FILLED component input -> this
-- config -> the code default).
--
-- Level 2 is new: `headings.split_at`. Without it one chunk could span two inner
-- headings ("Třída – 7." and "Třída – 8.") while `subsection_heading` holds only
-- one of them, so a query for the 8th grade could not reach that chunk through
-- the weighted heading columns from migration 61 at all. Cutting at the heading
-- makes the mapping exact: one chunk, one subsection. The chunks come out
-- smaller, which is affordable precisely because the heading now lives in its own
-- searchable column instead of being copied into the body.
--
-- Values equal the current effective behaviour, so activating this changes
-- nothing on its own except the deliberate `headings.split_at`.
--
-- Note on max_chars: 0 = off. Non-zero cuts an oversized element into several
-- element rows, which BREAKS the level-1 invariant — the extra rows do not start
-- with a heading and get their own file_item_uuid. Left at 0 on purpose.
--
-- Idempotent (both keys guarded); transactional.

BEGIN;

-- ── level 1: element boundaries ───────────────────────────────────────────────
UPDATE edu_ai.search_setting s
   SET config = jsonb_set(
         s.config, '{chunking,split_markdown_into_pages}',
         jsonb_build_object(
           'note', 'A line with 1..heading_level "#" opens a new page, and one '
                || 'page is one element = one file_item_uuid. Tables are emitted '
                || 'as their own element rows, so a page with text and a table '
                || 'yields several file_item_uuid sharing page_number. '
                || 'max_chars > 0 cuts oversized elements into more rows and thus '
                || 'breaks "an element starts with its heading" — keep 0.',
           'heading_level',        2,
           'strip_page_artifacts', true,
           'merge_page_tables',    true,
           'min_text_chars',       3,
           'max_chars',            0),
         true)
 WHERE s.config ? 'chunking'
   AND NOT (s.config->'chunking' ? 'split_markdown_into_pages');

-- ── level 2: a heading is a hard chunk boundary ────────────────────────────────
UPDATE edu_ai.search_setting s
   SET config = jsonb_set(
         s.config, '{chunking,split_text_into_chunks,headings}',
         coalesce(s.config->'chunking'->'split_text_into_chunks'->'headings',
                  '{}'::jsonb)
         || jsonb_build_object(
              'note', 'split_at=true -> an inner heading ends the preceding chunk '
                   || 'and opens a new one, so one chunk never spans two headings '
                   || 'and subsection_heading is unambiguous. max_len applies to '
                   || 'BOLD-only lines (a longer one is prose, not a heading); '
                   || 'markdown "#" headings are not length-limited.',
              'split_at', true),
         true)
 WHERE s.config ? 'chunking'
   AND NOT (s.config->'chunking'->'split_text_into_chunks'->'headings'
            ? 'split_at');

COMMIT;
