-- last change in code by Martin Safranek (2026-07-19).
-- ─────────────────────────────────────────────────────────────────────────
-- Seed: general-language synonym groups (survive a data-table reset because
-- they live in init, not in the QA-pack ingest that a test round truncates).
--
-- qa_synonym is read by rag_lexical / qa_lexical (load_synonym_groups):
--   * kind='entity' groups are EXPANDED into the tsquery AND into the coverage
--     concepts, so a query token and a chunk token that mean the same thing
--     bridge each other.
--   * The loader unions term_norm + term_lemma per term; term_stem is stored so
--     the group also covers the stem tsvector.
--
-- Reduced forms below were computed with the SHARED normalization contract
-- (simplemma cs lemma + Dolamic-Savoy light stem + NFC/lowercase/diacritics
-- strip) — the same code as search/app/text_norm.py. Keep them in sync if the
-- contract changes.
--
-- Idempotent: re-running replaces each group.
-- ─────────────────────────────────────────────────────────────────────────

-- Group: "start / begin" — bridges a question like
--   "kdy ZAČÍNÁ školní rok"   (lemma: zacinat)
-- with a table/heading chunk that says
--   "ZAHÁJENÍ školního roku"  (lemma: zahajeny)
DELETE FROM edu_ai.qa_synonym WHERE group_id = 'syn_start_zahajeni';

INSERT INTO edu_ai.qa_synonym
  (group_id, kind, term, term_norm, term_lemma, term_stem, is_key, lang, source, is_active)
VALUES
  ('syn_start_zahajeni','entity','zahájení','zahajeni','zahajeny','zahajen', true,  'cs','seed', true),
  ('syn_start_zahajeni','entity','zahájit', 'zahajit', 'zahajit', 'zahajit', false, 'cs','seed', true),
  ('syn_start_zahajeni','entity','zahájen', 'zahajen', 'zahajit', 'zahajen', false, 'cs','seed', true),
  ('syn_start_zahajeni','entity','začátek', 'zacatek', 'zacatek', 'zacatek', false, 'cs','seed', true),
  ('syn_start_zahajeni','entity','začít',   'zacit',   'zacinat', 'zacit',   false, 'cs','seed', true),
  ('syn_start_zahajeni','entity','začíná',  'zacina',  'zacinat', 'zacin',   false, 'cs','seed', true),
  ('syn_start_zahajeni','entity','začínat', 'zacinat', 'zacit',   'zacin',   false, 'cs','seed', true);
