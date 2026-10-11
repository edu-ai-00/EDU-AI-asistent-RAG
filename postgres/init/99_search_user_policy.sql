-- last change in code by Martin Safranek (2026-08-03).
-- 99_search_user_policy.sql
-- GLOBAL switch (decided 2026-08-03 — deliberately unlike the other visibility
-- switches, which are per organisation): must /search know WHO is asking, or is
-- the school alone enough? Whether the platform admits anonymous callers is one
-- property of the deployment — for every existing school and any school added
-- later — so the value lives on the GLOBAL settings row only and the code reads
-- it from there; an organisation's own row cannot override it.
--
--   visibility.search_requires_user = true   user_id is mandatory and verified
--                                            against the register — the entity
--                                            in the request is a fact
--   visibility.search_requires_user = false  (default, decided 2026-08-03: the
--                                            public web chat has no user
--                                            identity yet) the school alone
--                                            suffices; the caller searches
--                                            anonymously
--
-- What false does NOT relax:
--   * a SUPPLIED user_id is still verified — a wrong id is 403, never silently
--     downgraded to anonymous (that downgrade was the measured cross-tenant
--     leak's cousin: an unverified claim quietly winning)
--   * private documents stay invisible — the owner branch of the predicate
--     needs a verified user, an anonymous caller simply has none
--   * organisations stay isolated from each other — the search scope is built
--     from the entity either way
-- What it DOES open: anyone who can reach the API and knows the entity code
-- reads that organisation's entity/organization content. That is the price of
-- a userless channel (the public web chat), and it is per organisation — each
-- school decides for itself, on its own settings row.
--
-- The 'anonymous' ui_label rows (deactivated in init 97 when user_id became
-- unconditionally required) come back: the search test page offers the option
-- again, but only for organisations whose settings allow it.
--
-- Idempotent; transactional.

BEGIN;

-- the key lives ONLY on the global row; org rows are full config copies, so
-- strip it there — a stale copy would read as an override that does not exist
UPDATE edu_ai.settings s
   SET config = jsonb_set(
         s.config, '{visibility}',
         coalesce(s.config->'visibility', '{}'::jsonb)
         || jsonb_build_object('search_requires_user', false))
 WHERE s.legal_entity_code IS NULL
   AND NOT coalesce(s.config->'visibility', '{}'::jsonb) ? 'search_requires_user';

UPDATE edu_ai.settings s
   SET config = jsonb_set(s.config, '{visibility}',
                          (s.config->'visibility') - 'search_requires_user')
 WHERE s.legal_entity_code IS NOT NULL
   AND coalesce(s.config->'visibility', '{}'::jsonb) ? 'search_requires_user';

UPDATE edu_ai.ui_label SET is_active = true
 WHERE scope = 'search_test' AND label_key = 'anonymous' AND NOT is_active;

COMMIT;
