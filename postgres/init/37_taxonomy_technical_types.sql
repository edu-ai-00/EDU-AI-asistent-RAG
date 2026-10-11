-- last change in code by Martin Safranek (2026-07-28).
-- 37_taxonomy_technical_types.sql
-- Three technical document types (agreed 2026-07-28) — the knowledge base also
-- holds technical content (product docs, manuals, troubleshooting), which the
-- school-domain taxonomy had no honest home for (it fell to UNKNOWN).
-- The cross-type distinction rules live INSIDE llm_instruction, so everything
-- stays in the číselník: the {{DOCUMENT_TYPES}} placeholder pulls the new
-- types into the classification prompt automatically — no prompt migration,
-- no deploy; select menus and gateway validation pick them up via
-- /document-types within the cache TTL. Idempotent per type_code.

INSERT INTO edu_ai.document_type_taxonomy
    (type_code, label_cs, label_en, llm_instruction, sort_order, created_by, notes)
SELECT * FROM (VALUES
    ('TECHNICAL_DOCUMENTATION', 'Technická dokumentace', 'Technical documentation',
     'Descriptive documentation of a product, system or tool: overviews, reference material, architecture descriptions, API references, technical specifications. Describes WHAT a system is and how it is structured. If the content is primarily actionable numbered steps toward one goal, prefer HOW_TO_GUIDE instead.',
     62, 'migration-37', NULL),
    ('HOW_TO_GUIDE', 'Návod / postup', 'How-to guide / manual',
     'Step-by-step instructions for achieving a task: user manuals, tutorials, installation/configuration guides, walkthroughs. Describes HOW to do something, typically imperative or numbered steps. If it TEACHES school curriculum content to students, prefer STUDY_MATERIAL; if it is primarily descriptive reference, prefer TECHNICAL_DOCUMENTATION.',
     64, 'migration-37', NULL),
    ('TROUBLESHOOTING', 'Řešení problémů / FAQ', 'Troubleshooting / FAQ',
     'Content organized as problem -> solution or question -> answer: troubleshooting guides, FAQs, known issues, error-message explanations, diagnostic procedures. If it is a single linear procedure toward one goal rather than a problem/question structure, prefer HOW_TO_GUIDE.',
     66, 'migration-37',
     'Do not use as a catch-all for technical content — undeterminable stays UNKNOWN.')
) AS v(type_code, label_cs, label_en, llm_instruction, sort_order, created_by, notes)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.document_type_taxonomy
                  WHERE type_code IN ('TECHNICAL_DOCUMENTATION', 'HOW_TO_GUIDE', 'TROUBLESHOOTING'));
