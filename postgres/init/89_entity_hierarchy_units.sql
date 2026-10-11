-- last change in code by Martin Safranek (2026-08-02).
-- 89_entity_hierarchy_units.sql
-- The registered units of a legal person become rows of legal_entity, under it.
--
-- Škola Nová Ves is ONE legal person (RED-IZO 1234) operating six units,
-- each with its own IZO. The alternative was a separate school_unit table plus a
-- school_units[] column on documents and chunks; using parent_code instead is
-- strictly less machinery, because everything already keyed on legal_entity_code —
-- documents, chunks, users, config, exclusion rules, visibility, RLS — starts
-- working at unit granularity with no new column anywhere.
--
-- ── what has to come back for it to work ────────────────────────────────────
-- Inheritance DOWNWARD. An entity sees its own documents, its ANCESTORS' documents,
-- and everything public. Without it the school rules have nowhere to live: put them
-- on the legal person and neither school sees them; put them on the primary school
-- and the secondary one does not.
--
-- This does not reopen "documents and QA must never mix", which was about different
-- SCHOOLS. Inheritance here is one-directional inside one legal person: the primary
-- school sees what the organisation issued, and NOT what the secondary school
-- issued. Siblings never see each other — which is the separation between ZŠ and SOŠ
-- that was wanted in the first place.
--
-- ── the two new columns ─────────────────────────────────────────────────────
-- kind distinguishes the two sorts of row, because they carry different
-- identifiers: a legal_person has an IČO and a RED-IZO, a unit has an IZO. Without
-- it nobody can tell which is which, and `may_publish_public` (when it lands) must
-- only ever be grantable to a legal_person — a unit must not publish to the whole
-- platform.
--
-- level is what finally makes a bare grade number resolvable. Inside the legal
-- person "2" is ambiguous (primary, secondary, kindergarten); inside a unit it is
-- not. The search API can accept a number and resolve it against the unit's level
-- instead of guessing from the numeric range, which is what _grade_code does today.
--
-- ── which units are created ─────────────────────────────────────────────────
-- ZŠ, SOŠ, the after-school club and the canteen. The kindergarten (IZO 1234)
-- gets no row: it will not use the system. It stays recorded in
-- legal_entity.units, because the register should describe the organisation, not
-- only the part of it that logs in.
--
-- The child codes repeat the parent's (SKOLA_NOVA_VES_SOS). The parent's own code
-- says "ZS" for something that is NOT the primary school — it is the legal person —
-- so it is misleading, but 170 chunks, both users and the metadata reference it and
-- renaming is a data migration, not a rename. Worth doing separately if at all.
--
-- Idempotent; transactional.

BEGIN;

ALTER TABLE edu_ai.legal_entity
    ADD COLUMN IF NOT EXISTS kind  text NOT NULL DEFAULT 'legal_person',
    ADD COLUMN IF NOT EXISTS level text;

ALTER TABLE edu_ai.legal_entity DROP CONSTRAINT IF EXISTS legal_entity_kind_check;
ALTER TABLE edu_ai.legal_entity
    ADD CONSTRAINT legal_entity_kind_check
        CHECK (kind IN ('legal_person', 'unit'));

ALTER TABLE edu_ai.legal_entity DROP CONSTRAINT IF EXISTS legal_entity_level_check;
ALTER TABLE edu_ai.legal_entity
    ADD CONSTRAINT legal_entity_level_check
        CHECK (level IS NULL OR level IN ('MS', 'ZS', 'SS'));

-- a unit hangs under something; a legal person does not hang under anything
ALTER TABLE edu_ai.legal_entity DROP CONSTRAINT IF EXISTS legal_entity_unit_has_parent;
ALTER TABLE edu_ai.legal_entity
    ADD CONSTRAINT legal_entity_unit_has_parent
        CHECK ((kind = 'unit') = (parent_code IS NOT NULL));

COMMENT ON COLUMN edu_ai.legal_entity.kind IS
    'legal_person (has IČO + RED-IZO, is the contractual party) | unit (has IZO, '
    'is a registered school or facility of one). Only a legal_person may ever be '
    'granted the right to publish platform-wide.';
COMMENT ON COLUMN edu_ai.legal_entity.level IS
    'MS | ZS | SS for units that have school years, NULL for those that do not '
    '(canteen, after-school club) and for legal persons spanning several. This is '
    'what lets the API accept a bare grade number and resolve it — see migration 89.';

-- ── the units of Škola Nová Ves ───────────────────────────────────────────
INSERT INTO edu_ai.legal_entity
    (legal_entity_code, parent_code, kind, level, display_name, red_izo, domains, notes)
SELECT v.code, 'SKOLA_NOVA_VES', 'unit', v.level, v.name, NULL, NULL, v.note
FROM (VALUES
  ('SKOLA_NOVA_VES_ZS',  'ZS',  'Škola Nová Ves — základní škola',   'IZO 1234'),
  ('SKOLA_NOVA_VES_SOS', 'SS',  'Škola Nová Ves — střední odborná škola', 'IZO 1234'),
  ('SKOLA_NOVA_VES_SD',  NULL,  'Škola Nová Ves — školní družina',   'IZO 1234; bez ročníků'),
  ('SKOLA_NOVA_VES_SJ',  NULL,  'Škola Nová Ves — školní jídelna',   'IZO 1234; bez ročníků')
) AS v(code, level, name, note)
WHERE NOT EXISTS (SELECT 1 FROM edu_ai.legal_entity e WHERE e.legal_entity_code = v.code);

COMMIT;
