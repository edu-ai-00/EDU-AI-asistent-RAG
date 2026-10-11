-- 161: the remaining two schools select Czech as well (2026-08-21).
--
-- Migration 160 gave SKOLA_NOVA_VES a language selection and deliberately left
-- ZAKLADNI_SKOLA_BOROVA and ZAKLADNI_SKOLA_LIPOVA on the platform set as the CONTROL GROUP
-- for its measurement. This migration ends that control group, because the
-- measurement is in hand.
--
-- What it showed, on production, with one de-accented Czech question
-- ("Kdy se dozvim vysledky prijimaciho rizeni?" — no diacritics, which is how a
-- phone keyboard is actually used):
--
--   SKOLA_NOVA_VES_ZS      (selection, mig 160)  -> qa_exact
--   ZAKLADNI_SKOLA_BOROVA          (platform set)        -> qa_lexical
--   ZAKLADNI_SKOLA_LIPOVA  (platform set)        -> qa_lexical
--
-- and the same two schools recover qa_exact the moment the request carries
-- language='cs' explicitly. So the axis really is the language selection, not the
-- corpus and not the spelling: detect_lang returns NULL for short diacritics-free
-- Czech, prepare() then unions token forms across every language the entity
-- resolves to, and the union keeps stopwords that the cs pass alone would drop
-- ('se', 'je', 'v'). qa_exact compares whole strings, so one surviving token is a
-- miss. A school with ONE language has no union to be poisoned by.
--
-- Sampled more broadly (60 of the 345 probe cases that contain diacritics, asked
-- twice — as written and de-accented): 52 answered from the same step, 8 fell from
-- qa_exact to qa_lexical, 0 lost their answer. So this is precision, not an
-- outage: the answers were still found, by a weaker step, with a weaker guarantee.
--
-- Inheritance does the rest — resolved_code_list walks UP to the nearest ancestor
-- holding a selection, so one row per legal person also covers its units. Only the
-- two legal persons are listed here; SKOLA_NOVA_VES already has its row from 160.
--
-- NARROWING, deliberately: these schools can no longer be asked in de/en/es. Both
-- are Czech schools with Czech corpora. Undo by deactivating the row, which puts
-- the entity back on the platform set.
--
-- Flags copied from the global cs row so nothing downstream changes meaning:
-- is_processing_fallback drives the ingest's language fallback, is_ui_default the
-- upload form's preselection.
--
-- NOT fixed here, and worth knowing: qa_vector embeds the RAW question, so a
-- de-accented query still produces a different vector — measured 0.677 -> 0.421
-- on "jaké pomůcky pro 6. ročník?". No language selection touches that; it needs
-- the packs re-embedded from a normalised form on both sides.

INSERT INTO edu_ai.code_list
    (list_code, code, sort_order, is_active, created_by, created_at, notes,
     is_ui_default, labels, is_processing_fallback, legal_entity_code)
SELECT 'language', 'cs', 10, true, 'init-161', now(),
       'Language selection for the school (migration 161) — without it the entity '
       'inherits the platform set and the query tokenizer unions four languages.',
       true,
       '{"cs": "Čeština", "de": "Tschechisch", "en": "Czech", "es": "Checo"}'::jsonb,
       true, 'ZAKLADNI_SKOLA_LIPOVA'
WHERE NOT EXISTS (
    SELECT 1 FROM edu_ai.code_list
     WHERE list_code = 'language' AND code = 'cs'
       AND legal_entity_code = 'ZAKLADNI_SKOLA_LIPOVA');

INSERT INTO edu_ai.code_list
    (list_code, code, sort_order, is_active, created_by, created_at, notes,
     is_ui_default, labels, is_processing_fallback, legal_entity_code)
SELECT 'language', 'cs', 10, true, 'init-161', now(),
       'Language selection for the school (migration 161) — without it the entity '
       'inherits the platform set and the query tokenizer unions four languages.',
       true,
       '{"cs": "Čeština", "de": "Tschechisch", "en": "Czech", "es": "Checo"}'::jsonb,
       true, 'ZAKLADNI_SKOLA_BOROVA'
WHERE NOT EXISTS (
    SELECT 1 FROM edu_ai.code_list
     WHERE list_code = 'language' AND code = 'cs'
       AND legal_entity_code = 'ZAKLADNI_SKOLA_BOROVA');

-- Report the STATE, not the fact that it ran: both INSERTs are guarded, so
-- "INSERT 0 0" is the normal answer on a second application and says nothing
-- about whether the state is right. Every active legal person must resolve to
-- exactly one language after this.
DO $$
DECLARE
    r record;
    bad int := 0;
BEGIN
    FOR r IN
        SELECT e.legal_entity_code,
               (SELECT count(*) FROM edu_ai.resolved_code_list(e.legal_entity_code,
                                                               'language')) AS langs
          FROM edu_ai.legal_entity e
         WHERE e.is_active
         ORDER BY e.legal_entity_code
    LOOP
        RAISE NOTICE '161: % resolves to % language(s)', r.legal_entity_code, r.langs;
        IF r.langs <> 1 THEN
            bad := bad + 1;
        END IF;
    END LOOP;
    IF bad > 0 THEN
        RAISE NOTICE '161: % entity/entities still resolve to more than one '
                     'language — check for a legal person without a selection',
                     bad;
    END IF;
END $$;
