-- last change in code by Martin Safranek (2026-08-02).
-- 76_legal_entity_and_users.sql
-- The registries multi-tenancy stands on: the legal entity, and the two kinds of
-- user that belong to one. See specs/active/spec.multi-entity.md.
--
-- "legal entity", never "school": the same platform is meant to serve companies,
-- and a column called school_id would have to be renamed the first time it does.
--
-- Nothing existed before this. A repository-wide search for a users, roles,
-- permissions or tenant table found nothing; the only identity ever stored is two
-- free-text columns on qa_query_log (user_role, user_school, migration 29) that
-- nothing validates.
--
-- ── the code, not a UUID ────────────────────────────────────────────────────
-- legal_entity_code travels through the ingest API, the flow parameters, the logs
-- and the diagnostics, and a human reads it in all four. The price is that a code
-- cannot be renamed without a data migration, so everything mutable lives in
-- display_name.
--
-- ── two user tables, not one ────────────────────────────────────────────────
-- A staff user is a named person in a back office who authenticates and WRITES:
-- a handful per entity, lasting as long as an employment. An app user is a mobile
-- installation known by a technical id and nothing else, which only READS: tens of
-- thousands per entity, and deliberately anonymous, because a pupil's questions
-- are a behavioural record and a name would make it a personal one.
--
-- They were briefly one table with a user_kind discriminator. That was wrong. No
-- code path ever needs both — search resolves app users, ingestion resolves staff
-- — and the discriminator's CHECK ("a name is required, but only for this kind")
-- is the schema pointing out that there are two entities in one table. Kept
-- together, the columns diverge (SSO subject and permissions against push token
-- and app version, half of them NULL for half the rows), the retention rules
-- diverge (an employment against data minimisation for a minor), and the grants
-- cannot: with two tables the search role has no SELECT on staff at all.
--
-- Administration that genuinely wants both gets the view at the end.
--
-- ── the id is globally unique, and the pair is still enforced ───────────────
-- PRIMARY KEY (user_id): an identifier identifies one person, everywhere. A
-- support ticket, a log line or an audit record quoting "u-4471" must not need the
-- entity beside it to mean anything.
--
-- The composite UNIQUE (legal_entity_code, user_id) stays as well, and it is not
-- redundant: it is what documents and QA reference. A foreign key on user_id alone
-- would let a document of entity A name an owner from entity B, and the database
-- would accept it. With the pair as the target, that row is rejected.
--
-- Because ids are global, they need to not collide by accident — and they must not
-- be GUESSABLE. A readable convention like <ENTITY_CODE>:admin collides with
-- nothing, but it is guessable on the first try, and while /v1/ingest still takes
-- user_id from a form without authenticating the sender, a guessable id is a route
-- to impersonating whoever ingested a document. Seeded users therefore get a
-- random UUID; an identity provider will later bring its own opaque subject, which
-- is why the column is text and not uuid.
--
-- Readability lives in display_name, not in the id. The entity code is the
-- opposite case: it IS readable on purpose (it travels through the API, the flow
-- parameters and the logs) and that is safe, because a school's name is not a
-- secret and the code is not a capability — knowing ZS_PANSKA must not grant
-- access to anything.
--
-- ── no foreign key from the logs ────────────────────────────────────────────
-- qa_query_log.user_id stays a plain column. The log is append-only and written
-- after the answer has been sent; a foreign key would let an unknown user turn a
-- successful answer into a failed write, and it has to survive the user being
-- deleted, which under GDPR is a thing that happens.
--
-- Idempotent; transactional.

BEGIN;

CREATE TABLE IF NOT EXISTS edu_ai.legal_entity (
    legal_entity_code  text PRIMARY KEY,
    parent_code        text REFERENCES edu_ai.legal_entity (legal_entity_code),
    display_name       text NOT NULL,
    ico                text,
    domains            text[],
    is_active          boolean NOT NULL DEFAULT true,
    created_at         timestamptz NOT NULL DEFAULT now(),
    notes              text
);

COMMENT ON TABLE edu_ai.legal_entity IS
    'The school / company whose documents, QA and users these are. Referenced by '
    'every data table (migration 77).';
COMMENT ON COLUMN edu_ai.legal_entity.parent_code IS
    'Who this entity belongs under, for reporting and administration. Carries NO '
    'permission: search never reads it, and one entity never sees another''s '
    'documents through it.';
COMMENT ON COLUMN edu_ai.legal_entity.domains IS
    'Origins of the web chat that resolve to this entity, so the entity can be '
    'derived server-side instead of being asserted by the client.';

CREATE INDEX IF NOT EXISTS legal_entity_parent_idx
    ON edu_ai.legal_entity (parent_code) WHERE parent_code IS NOT NULL;

-- ── staff: named people who ingest documents and administer the entity ──────
CREATE TABLE IF NOT EXISTS edu_ai.staff_user (
    user_id            text PRIMARY KEY,
    legal_entity_code  text NOT NULL
        REFERENCES edu_ai.legal_entity (legal_entity_code),
    display_name       text NOT NULL,
    email              text,
    is_active          boolean NOT NULL DEFAULT true,
    created_at         timestamptz NOT NULL DEFAULT now(),
    last_login_at      timestamptz,
    notes              text,
    -- the target of the owner foreign keys: it is what makes "the owner belongs to
    -- the same entity as the document" a database rule and not a hope
    UNIQUE (legal_entity_code, user_id)
);

COMMENT ON TABLE edu_ai.staff_user IS
    'A named person of one legal entity who ingests documents or administers it. '
    'The name is required and lands on the documents they ingest.';
COMMENT ON COLUMN edu_ai.staff_user.user_id IS
    'Identifier issued by the back office or the identity provider. Globally '
    'unique: it identifies one person everywhere, so a log line quoting it needs '
    'nothing beside it.';

CREATE INDEX IF NOT EXISTS staff_user_entity_active_idx
    ON edu_ai.staff_user (legal_entity_code) WHERE is_active;

-- ── app: mobile installations that only ask questions ───────────────────────
CREATE TABLE IF NOT EXISTS edu_ai.app_user (
    user_id            text PRIMARY KEY,
    legal_entity_code  text NOT NULL
        REFERENCES edu_ai.legal_entity (legal_entity_code),
    is_active          boolean NOT NULL DEFAULT true,
    created_at         timestamptz NOT NULL DEFAULT now(),
    last_seen_at       timestamptz,
    notes              text,
    UNIQUE (legal_entity_code, user_id)
);

COMMENT ON TABLE edu_ai.app_user IS
    'A mobile installation of one legal entity, known by a technical id only. No '
    'name on purpose: the questions asked are a behavioural record, and a name '
    'would make it a personal one.';
COMMENT ON COLUMN edu_ai.app_user.is_active IS
    'A deactivated user is refused, not silently downgraded — the check belongs in '
    'front of the search, not inside it.';

CREATE INDEX IF NOT EXISTS app_user_entity_active_idx
    ON edu_ai.app_user (legal_entity_code) WHERE is_active;

-- ── the one place that wants both ───────────────────────────────────────────
-- Administration lists everyone; nothing else should. Keeping this a view rather
-- than a table is what lets the two keep diverging.
CREATE OR REPLACE VIEW edu_ai.user_v AS
    SELECT legal_entity_code, user_id, 'staff'::text AS user_kind,
           display_name, is_active, created_at, last_login_at AS last_active_at
      FROM edu_ai.staff_user
    UNION ALL
    SELECT legal_entity_code, user_id, 'app'::text,
           NULL, is_active, created_at, last_seen_at
      FROM edu_ai.app_user;

COMMIT;
