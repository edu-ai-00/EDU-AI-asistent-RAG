"""The registry table `edu_ai.schema_migration` (v1): DDL and its own definition check."""
from __future__ import annotations

VERSION = 1
# Session advisory lock held for a whole apply/init ('EDUMIGR1' as a bigint).
LOCK_KEY = 0x4544554D49475231

DDL = """\
CREATE TABLE edu_ai.schema_migration (
    migration_id text PRIMARY KEY,
    kind         text NOT NULL,
    checksum     text NOT NULL,
    release      text,
    applied_at   timestamptz NOT NULL DEFAULT now(),
    applied_by   text,
    notes        text,
    CONSTRAINT schema_migration_kind_check CHECK (kind IN ('baseline', 'migration', 'adopted'))
);
CREATE UNIQUE INDEX schema_migration_one_baseline ON edu_ai.schema_migration ((true))
    WHERE kind IN ('baseline', 'adopted');
COMMENT ON TABLE edu_ai.schema_migration IS 'EDU-AI migration registry v1';
"""

# One line per column / constraint / index / trigger / comment, read from pg_catalog.
DESCRIBE_SQL = """\
SELECT line FROM (
  SELECT 1 AS k, a.attnum::int AS o,
         'column ' || a.attname || ' ' || format_type(a.atttypid, a.atttypmod)
         || CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END
         || COALESCE(' DEFAULT ' || pg_get_expr(d.adbin, d.adrelid), '') AS line
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
   WHERE n.nspname = 'edu_ai' AND c.relname = 'schema_migration'
     AND a.attnum > 0 AND NOT a.attisdropped
  UNION ALL
  -- contype 'n': PostgreSQL 18 also catalogs NOT NULL as constraints; the column lines carry it
  SELECT 2, 0, 'constraint ' || co.conname || ' ' || pg_get_constraintdef(co.oid)
    FROM pg_constraint co WHERE co.conrelid = to_regclass('edu_ai.schema_migration') AND co.contype <> 'n'
  UNION ALL
  SELECT 3, 0, pg_get_indexdef(i.indexrelid)
    FROM pg_index i WHERE i.indrelid = to_regclass('edu_ai.schema_migration')
  UNION ALL
  SELECT 4, 0, 'trigger ' || pg_get_triggerdef(t.oid)
    FROM pg_trigger t WHERE t.tgrelid = to_regclass('edu_ai.schema_migration') AND NOT t.tgisinternal
  UNION ALL
  SELECT 5, 0, 'table kind=' || c.relkind::text || ' persistence=' || c.relpersistence::text
         || ' comment=' || COALESCE(obj_description(c.oid, 'pg_class'), '')
    FROM pg_class c WHERE c.oid = to_regclass('edu_ai.schema_migration')
) x ORDER BY k, o, line;
"""

# Captured from pg_catalog (PostgreSQL 16 and 18) after running DDL; the runner tests keep it honest.
EXPECTED_V1: list[str] = [
    "column migration_id text NOT NULL",
    "column kind text NOT NULL",
    "column checksum text NOT NULL",
    "column release text",
    "column applied_at timestamp with time zone NOT NULL DEFAULT now()",
    "column applied_by text",
    "column notes text",
    "constraint schema_migration_kind_check CHECK ((kind = ANY (ARRAY['baseline'::text, "
    "'migration'::text, 'adopted'::text])))",
    "constraint schema_migration_pkey PRIMARY KEY (migration_id)",
    "CREATE UNIQUE INDEX schema_migration_one_baseline ON edu_ai.schema_migration USING btree "
    "((true)) WHERE (kind = ANY (ARRAY['baseline'::text, 'adopted'::text]))",
    "CREATE UNIQUE INDEX schema_migration_pkey ON edu_ai.schema_migration USING btree (migration_id)",
    "table kind=r persistence=p comment=EDU-AI migration registry v1",
]


def drift(actual: list[str]) -> list[str]:
    """Differences between the live definition and registry v1 (empty list = in order)."""
    if not actual:
        return ["registry table edu_ai.schema_migration does not exist"]
    out = [f"missing: {line}" for line in EXPECTED_V1 if line not in actual]
    out += [f"unexpected: {line}" for line in actual if line not in EXPECTED_V1]
    return out
