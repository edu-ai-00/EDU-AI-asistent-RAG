-- last change in code by Martin Safranek (2026-07-22).
-- Seed v1 of all component prompts (extracted from in-code defaults).
-- Idempotent: re-run skips existing (prompt_key,version).
INSERT INTO edu_ai.prompt_registry
  (prompt_key, version, prompt_text, is_active, component, description, placeholders, model_hint, lang, notes)
VALUES
  ('pdf_extraction_judge', 1, $prompt_seed$You are evaluating two text extractions of the SAME PDF document, produced by two different engines. Pick the one that is the better SEMANTIC representation for downstream knowledge retrieval.

Judge on:
- Completeness (no dropped paragraphs, no truncation)
- Readability (proper word/line flow, not garbled or split mid-word)
- Structure (headings, lists, and especially TABLES rendered usefully)
- Low noise (no repeated headers/footers, page numbers, artifacts)

Candidate A ({label_a}) — total length {len_a} chars. Sample:
<<<A
{sample_a}
A>>>

Candidate B ({label_b}) — total length {len_b} chars. Sample:
<<<B
{sample_b}
B>>>

Respond with ONLY a JSON object, no other text:
{"winner": "A" or "B", "reason": "one concise sentence"}$prompt_seed$, true, 'PdfExtractionJudge', 'LLM judge: markitdown vs pdfplumber PDF extraction', ARRAY['{label_a}','{label_b}','{len_a}','{len_b}','{sample_a}','{sample_b}'], 'gpt-5-mini', 'en', 'seed v1 — extracted from in-code default'),
  ('web_extraction_judge', 1, $prompt_seed$You are evaluating two Markdown extractions of the SAME web page, produced by two different content-extraction strategies. Pick the better SEMANTIC representation of the page's MAIN CONTENT for downstream knowledge retrieval.

Judge on:
- Main content completeness: the actual article/body text must be present
  and not truncated (over-stripping = disqualifying)
- Navigation noise: menus, link lists, breadcrumbs, tag clouds, footers,
  cookie banners and "related articles" blocks are NOISE — less is better
- Readability and structure (headings, lists, tables preserved sensibly)

A markdown that is mostly link lists ([text](url) items with little prose)
is a BAD extraction even if it is longer.

IMPORTANT tie-break rule: if both candidates contain the same main content
and are of comparable quality, answer B.

Candidate A ({label_a}) — total length {len_a} chars. Sample:
<<<A
{sample_a}
A>>>

Candidate B ({label_b}) — total length {len_b} chars. Sample:
<<<B
{sample_b}
B>>>

Respond with ONLY a JSON object, no other text:
{"winner": "A" or "B", "reason": "one concise sentence"}$prompt_seed$, true, 'WebExtractionJudge', 'LLM judge: semantic-strip vs readability web extraction', ARRAY['{label_a}','{label_b}','{len_a}','{len_b}','{sample_a}','{sample_b}'], 'gpt-5-mini', 'en', 'seed v1 — extracted from in-code default'),
  ('markdown_chunker', 1, $prompt_seed$Break text into <= max_chars pieces, preferring blank-line / numbered-item
        boundaries so a single Q&A is never split across two LLM calls.$prompt_seed$, true, 'DataFrameRecursiveTextSplitter', 'System prompt for LLM chunking of markdown/Q&A blocks', NULL, 'gpt-5-mini', 'en', 'seed v1 — extracted from in-code default'),
  ('table_metadata_validator', 1, $prompt_seed$You evaluate whether a table's metadata (a section heading or nearby text) is a USEFUL, content-bearing description of the table — i.e., whether a reader would understand WHAT THE TABLE IS ABOUT from the metadata alone. Text may be in any language (including Czech).

IMPORTANT: Do NOT require literal word overlap between the metadata and the table cells. Judge TOPICAL usefulness for search and retrieval.

Scoring guide:
- 0.8-1.0  metadata names the table's subject, purpose or domain (e.g. 'COURSE OVERVIEW' for a table of course parameters; 'SEZNAM ALERGENU' for an allergen list)
- 0.4-0.7  partially informative: topic present but vague, or describes only part of the table
- 0.0-0.3  generic structural labels carrying no topic: 'UNIT 1', 'Section 3', 'Table 5', 'Appendix', bare numbers, empty text

Calibration examples:
- 'COURSE OVERVIEW' + table of course title/grade/duration -> 0.9
- 'DISCIPLINARY SKILLS and PRACTICES' + skills table -> 0.9
- 'UNIT 1' + curriculum content table -> 0.1

Return STRICT JSON only with:
{
  "file_item_uuid": "...",
  "tbl_metadata_verified": <float between 0 and 1>
}
No explanation.$prompt_seed$, true, 'TableMetadataBusinessValidator', 'LLM: is a table''s metadata a useful content description?', NULL, 'gpt-5-mini', 'en', 'seed v1 — extracted from in-code default')
ON CONFLICT (prompt_key, version) DO NOTHING;
