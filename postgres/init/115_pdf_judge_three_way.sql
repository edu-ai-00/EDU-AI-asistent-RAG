-- last change in code by Martin Safranek (2026-08-04).
-- 115_pdf_judge_three_way.sql
-- The PDF extraction judge compares THREE candidates now (decided
-- 2026-08-04): A markitdown, B pdfplumber row-bridge, and the new C —
-- size-aware pdfplumber, which derives heading levels from FONT SIZES
-- (## lines larger than the page body, ### bold body-size lines). Text
-- heuristics cannot tell "2. třídy" (14pt heading) from "ZPĚT" (12pt
-- back-link); typography can. The judge picks per document, so documents
-- where markitdown reads better keep winning with B/A.
--
-- The judge reads its prompt from the component's UI value (materialized
-- in the flow); this row keeps the registry as the documented source of
-- truth in sync with it.
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.prompt_registry
   SET is_active = false
 WHERE prompt_key = 'pdf_extraction_judge' AND version = 1;

INSERT INTO edu_ai.prompt_registry
    (prompt_key, version, prompt_text, is_active, component, description,
     placeholders, model_hint, lang, created_by, notes)
VALUES
    ('pdf_extraction_judge', 2,
'You are evaluating three text extractions of the SAME PDF document, produced by different engines. Pick the one that is the best SEMANTIC representation for downstream knowledge retrieval.

Judge on:
- Completeness (no dropped paragraphs, no truncation)
- Readability (proper word/line flow, not garbled or split mid-word)
- Structure (headings that reflect the document''s REAL hierarchy, lists, and especially TABLES rendered usefully)
- Low noise (no repeated headers/footers, page numbers, navigation artifacts)

A candidate whose sample says EMPTY failed — never pick it.

Candidate A ({label_a}) — total length {len_a} chars. Sample:
<<<A
{sample_a}
A>>>

Candidate B ({label_b}) — total length {len_b} chars. Sample:
<<<B
{sample_b}
B>>>

Candidate C ({label_c}) — total length {len_c} chars. Sample:
<<<C
{sample_c}
C>>>

Respond with ONLY a JSON object, no other text:
{"winner": "A" or "B" or "C", "reason": "one concise sentence"}',
     true,
     'PdfExtractionJudge (PDF flow)',
     'Three-way PDF extraction judge: markitdown vs pdfplumber bridge vs size-aware pdfplumber.',
     ARRAY['{label_a}','{label_b}','{label_c}','{len_a}','{len_b}','{len_c}','{sample_a}','{sample_b}','{sample_c}'],
     'gpt-5-mini', 'en', 'init-115',
     'v2: third candidate C (size-aware) — heading levels from font sizes; judge picks per document.')
ON CONFLICT (prompt_key, version) DO NOTHING;

COMMIT;
