"""Exercise migration 214 against PostgreSQL in a rolled-back isolated schema.

Run from the host: python tests/test_school_domains_migration.py
Only uses the standard library and the local dev PostgreSQL container.
"""
from pathlib import Path
import subprocess
import uuid


ROOT = Path(__file__).resolve().parents[1]


def main():
    migration = (ROOT / "postgres/init/214_school_operations_safety_domains.sql").read_text(encoding="utf-8")
    bootstrap = (ROOT / "postgres/init_clean/03_school_operations_safety_domains.sql").read_text(encoding="utf-8")
    assert migration == bootstrap, "Clean-install overlay differs from migration"
    schema = "test_domains_" + uuid.uuid4().hex
    # Keep all three applications inside the test transaction; no real catalog writes.
    body = "\n".join(line for line in migration.splitlines() if line not in ("BEGIN;", "COMMIT;"))
    body = body.replace("edu_ai.", schema + ".")
    sql = f"""
BEGIN;
CREATE SCHEMA {schema};
CREATE TABLE {schema}.taxonomy_rules
    (LIKE edu_ai.taxonomy_rules INCLUDING ALL);
CREATE TABLE {schema}.legal_entity
    (LIKE edu_ai.legal_entity INCLUDING ALL);
DO $test$
BEGIN
    EXECUTE replace(pg_get_functiondef(
        'edu_ai.resolved_taxonomy_rules(text,text)'::regprocedure),
        'edu_ai.', '{schema}.');
END $test$;
INSERT INTO {schema}.legal_entity (legal_entity_code, display_name, parent_code, kind)
VALUES ('own', 'Own catalog', NULL, 'legal_person'),
       ('inherited', 'Inherited catalog', 'own', 'unit'),
       ('global', 'Global catalog', NULL, 'legal_person'),
       ('retired', 'Retired code', NULL, 'legal_person');
INSERT INTO {schema}.taxonomy_rules
    (type_code, taxonomy_type_code, sort_order, labels, llm_instruction, legal_entity_code)
VALUES ('ATTENDANCE', 'DOMAIN', 5, '{{}}', 'fixture', NULL),
       ('ATTENDANCE', 'DOMAIN', 5, '{{}}', 'fixture', 'own'),
       ('ATTENDANCE', 'DOMAIN', 5, '{{}}', 'fixture', 'retired');
INSERT INTO {schema}.taxonomy_rules
    (type_code, taxonomy_type_code, sort_order, labels, llm_instruction, legal_entity_code, is_active)
VALUES ('SCHOOL_SAFETY', 'DOMAIN', 9, '{{"cs":"Retired"}}', 'fixture', 'retired', false);
{body}
CREATE TEMP TABLE first_pass AS SELECT * FROM {schema}.taxonomy_rules;
{body}
DO $test$
DECLARE ent text;
BEGIN
    IF EXISTS ((SELECT * FROM first_pass EXCEPT SELECT * FROM {schema}.taxonomy_rules)
        UNION ALL (SELECT * FROM {schema}.taxonomy_rules EXCEPT SELECT * FROM first_pass)) THEN
        RAISE EXCEPTION 'Repeat application changed rows';
    END IF;
    FOREACH ent IN ARRAY ARRAY['own', 'inherited', 'global'] LOOP
        IF (SELECT count(*) FROM {schema}.resolved_taxonomy_rules(ent, 'DOMAIN')
            WHERE type_code IN ('SCHOOL_OPERATIONS', 'SCHOOL_SAFETY')) <> 2 THEN
            RAISE EXCEPTION 'Missing effective codes for %', ent;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM {schema}.resolved_taxonomy_rules(ent, 'DOMAIN')
                       WHERE type_code = 'ATTENDANCE') THEN
            RAISE EXCEPTION 'Existing catalog lost for %', ent;
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM {schema}.taxonomy_rules
               WHERE legal_entity_code IN ('inherited', 'global')) THEN
        RAISE EXCEPTION 'Created a partial catalog for an inheriting school';
    END IF;
    IF EXISTS (SELECT 1 FROM {schema}.resolved_taxonomy_rules('retired', 'DOMAIN')
               WHERE type_code = 'SCHOOL_SAFETY') THEN
        RAISE EXCEPTION 'Reactivated a retired code';
    END IF;
    IF EXISTS (SELECT 1 FROM {schema}.taxonomy_rules WHERE created_by = 'migration-214'
               AND (NOT labels ?& ARRAY['cs','en','de','es']
                    OR search_role <> 'normal' OR domain_keywords IS NOT NULL)) THEN
        RAISE EXCEPTION 'Missing labels or unintended routing settings';
    END IF;
END $test$;
-- A repeat after a curator edit must preserve both the ID and all edited fields.
UPDATE {schema}.taxonomy_rules SET labels = '{{"cs":"Custom safety"}}', search_role = 'excluded'
WHERE legal_entity_code = 'own' AND type_code = 'SCHOOL_SAFETY';
TRUNCATE first_pass;
INSERT INTO first_pass SELECT * FROM {schema}.taxonomy_rules;
{body}
DO $test$
BEGIN
    IF EXISTS ((SELECT * FROM first_pass EXCEPT SELECT * FROM {schema}.taxonomy_rules)
        UNION ALL (SELECT * FROM {schema}.taxonomy_rules EXCEPT SELECT * FROM first_pass)) THEN
        RAISE EXCEPTION 'Curator edits overwritten';
    END IF;
END $test$;
ROLLBACK;
"""
    result = subprocess.run(
        ["docker", "exec", "-i", "edu_ai_postgres", "sh", "-c",
         'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -q'],
        input=sql, text=True, encoding="utf-8", capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    print("PASS: bootstrap parity, effective catalogs, inheritance, repeat application, retired codes and curator edits")


if __name__ == "__main__":
    main()
