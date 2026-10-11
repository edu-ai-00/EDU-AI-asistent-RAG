-- last change in code by Martin Safranek (2026-08-02).
-- 92_visibility_policy_settings.sql
-- Two policy switches for how visibility behaves in a hierarchy. Both are
-- deployment decisions, not facts about the data, so they belong in the settings
-- row rather than in code.
--
--   child_may_publish_organization   may a UNIT choose `organization`?
--                                    Checked at INGEST (/ingest-principal): when
--                                    off, the request is refused with 400, not
--                                    silently downgraded, and /v1/entities reports
--                                    may_publish_organization=false so the form
--                                    never offers the choice. At its widest the
--                                    document reaches the unit's own parent and
--                                    sibling units — organization_entities() never
--                                    crosses to another legal person, switch or no
--                                    switch.
--
--   entity_visible_to_parent         is a unit's `entity` document also readable by
--                                    the users of the legal person above it?
--                                    Checked at QUERY time (scope.where): it decides
--                                    whether the `entity` branch matches the
--                                    searcher alone or the searcher and everything
--                                    under them.
--
-- Both default to true, because that is the behaviour that was described: a unit
-- publishing to its whole school is ordinary, and a head office that cannot see
-- what its own units publish would be odd. Turning either off makes the hierarchy
-- stricter, never looser — which is the safe direction for a switch to move in.
--
-- What neither one changes: siblings never see each other's `entity` documents,
-- the head office's own `entity` documents never reach the units, and nothing —
-- no value, no switch — reaches another organisation under another parent.
--
-- Idempotent; transactional.

BEGIN;

UPDATE edu_ai.settings s
   SET config = jsonb_set(
         s.config, '{visibility}',
         coalesce(s.config->'visibility', '{}'::jsonb)
         || jsonb_build_object(
              'note', 'child_may_publish_organization is enforced at ingest '
                   || '(/ingest-principal): true lets a unit choose organization, '
                   || 'which reaches its parent and sibling units — never another '
                   || 'legal person. entity_visible_to_parent is enforced at query '
                   || 'time (scope.where). Both tighten when false.',
              'child_may_publish_organization', true,
              'entity_visible_to_parent', true),
         true)
-- coalesce, not `NOT (config->'visibility' ? key)`: when the object is absent the
-- ? operator yields NULL, NOT NULL is NULL, and the row is silently skipped — the
-- guard would make the script a no-op on exactly the rows it is meant to fix.
 WHERE NOT (coalesce(s.config->'visibility', '{}'::jsonb) ? 'entity_visible_to_parent');

COMMIT;
