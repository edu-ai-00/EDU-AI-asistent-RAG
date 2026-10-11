-- 160: Škola Nová Ves selects Czech, instead of inheriting all four languages
-- (2026-08-21).
--
-- The school had NO language rows, so resolved_code_list's selection-with-default
-- handed it the whole platform set (cs, de, en, es). prepare() then builds query
-- token forms for EVERY language whenever detect_lang cannot decide — which is
-- routine for short, diacritics-free Czech. The cs pass drops Czech stopwords, the
-- other three do not, so `se`, `k`, `na`, `jsem` survive and are appended to the
-- token list. The indexed variant text has them dropped, and qa_exact compares
-- whole strings — so the match can never happen.
--
-- Measured on production: 31 of 63 curated variants missed qa_exact and fell
-- through to qa_lexical/qa_vector. Every one of them had question_language NULL;
-- every variant that matched had 'cs'. Selecting cs makes build_langs a single
-- language, so a failed detection stops mattering — the one pass agrees with the
-- ingest by construction. Fixes the tsquery and the blocklist too, not just the
-- fast path, and cannot over-drop a token the way a cross-language stoplist can.
--
-- Inheritance does the rest: resolved_code_list walks UP to the nearest ancestor
-- holding a selection, so this one row on the legal person also covers the ZŠ and
-- SOŠ units. ZAKLADNI_SKOLA_BOROVA and ZAKLADNI_SKOLA_LIPOVA are deliberately left on the
-- platform set — they are the control group for the measurement.
--
-- NARROWING, and that is the point: this school can no longer be asked in de/en/es.
-- Its whole corpus and all ten QA packs are Czech. Undo by deactivating the row.
--
-- Flags copied from the global cs row so nothing downstream changes meaning:
-- is_processing_fallback drives the ingest's language fallback, is_ui_default the
-- upload form's preselection.

INSERT INTO edu_ai.code_list
    (list_code, code, sort_order, is_active, created_by, created_at, notes,
     is_ui_default, labels, is_processing_fallback, legal_entity_code)
SELECT 'language', 'cs', 10, true, 'init-160', now(),
       'Language selection for the school (migration 160) — without it the entity '
       'inherits the platform set and the query tokenizer unions four languages.',
       true,
       '{"cs": "Čeština", "de": "Tschechisch", "en": "Czech", "es": "Checo"}'::jsonb,
       true, 'SKOLA_NOVA_VES'
WHERE NOT EXISTS (
    SELECT 1 FROM edu_ai.code_list
     WHERE list_code = 'language' AND code = 'cs'
       AND legal_entity_code = 'SKOLA_NOVA_VES');
