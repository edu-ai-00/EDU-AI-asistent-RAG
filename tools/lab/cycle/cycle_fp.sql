-- Content + schema fingerprint of ONE database (read-only, run in a REPEATABLE READ READ ONLY transaction by cycle.py).
-- Output: T|schema.table|rows|md5 of the sorted row md5s  Q|schema.sequence|last_value  L|large_objects|n|md5  S|<schema item>
-- Hashes, counts and object names only; no row content. search_path is fixed so that the deparsed text is comparable.
--   T|schema.table|rows|md5 over the sorted md5 of every row's text form
--   Q|schema.sequence|last_value        (empty = never called)
--   L|large_objects|count|md5 over (oid, md5(data))
--   S|<category>|<object line>           (schema items, compared as a set)
\set QUIET on
\pset format unaligned
\pset tuples_only on
\pset fieldsep '|'
\pset null ''
SET search_path = public;
SET TimeZone = 'UTC';
SET extra_float_digits = 3;
SET DateStyle = 'ISO, MDY';
SET IntervalStyle = 'iso_8601';
SET bytea_output = 'hex';
SET lc_monetary = 'C';

SELECT format(
  'SELECT %L, count(*), coalesce(md5(string_agg(h, '''' ORDER BY h COLLATE "C")), ''-'') FROM (SELECT md5(t::text) AS h FROM %I.%I AS t) s',
  'T|' || n.nspname || '.' || c.relname, n.nspname, c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'm')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
ORDER BY n.nspname, c.relname
\gexec

SELECT 'Q|' || schemaname || '.' || sequencename || '|' || coalesce(last_value::text, '')
FROM pg_sequences ORDER BY schemaname, sequencename;

SELECT 'L|large_objects|' || count(*) || '|' ||
       coalesce(md5(string_agg(oid::text || ':' || md5(lo_get(oid)), ',' ORDER BY oid)), '-')
FROM pg_largeobject_metadata;

WITH sysns AS (
  SELECT oid FROM pg_namespace
  WHERE nspname IN ('pg_catalog', 'information_schema') OR nspname LIKE 'pg_toast%' OR nspname LIKE 'pg_temp%'
), ext_objs AS (
  SELECT objid FROM pg_depend WHERE deptype = 'e'
)
SELECT 'S|' || x FROM (
  SELECT 'ext|' || extname || '|' || extversion AS x FROM pg_extension
  UNION ALL SELECT 'schema|' || nspname || '|' || pg_get_userbyid(nspowner) || '|' || coalesce(nspacl::text, '')
    FROM pg_namespace WHERE oid NOT IN (SELECT oid FROM sysns)
  UNION ALL SELECT 'rel|' || n.nspname || '.' || c.relname || '|' || c.relkind::text || '|' || pg_get_userbyid(c.relowner) || '|' ||
             coalesce(c.relacl::text, '') || '|' || c.relpersistence::text || '|' || coalesce(c.reloptions::text, '')
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.oid NOT IN (SELECT oid FROM sysns) AND c.oid NOT IN (SELECT objid FROM ext_objs)
  UNION ALL SELECT 'col|' || n.nspname || '.' || c.relname || '|' || (row_number() OVER (PARTITION BY c.oid ORDER BY a.attnum)) ||
             '|' || a.attname || '|' || format_type(a.atttypid, a.atttypmod) || '|' || a.attnotnull::text || '|' ||
             coalesce(pg_get_expr(d.adbin, d.adrelid), '') || '|' || a.attidentity::text || '|' || a.attgenerated::text || '|' ||
             coalesce(a.attacl::text, '')
    FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND n.oid NOT IN (SELECT oid FROM sysns) AND c.oid NOT IN (SELECT objid FROM ext_objs)
  UNION ALL SELECT 'idx|' || schemaname || '.' || indexname || '|' || indexdef FROM pg_indexes
    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
  UNION ALL SELECT 'con|' || n.nspname || '.' || c.relname || '.' || k.conname || '|' || k.contype::text || '|' || pg_get_constraintdef(k.oid)
    FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.oid NOT IN (SELECT oid FROM sysns)
  UNION ALL SELECT 'trg|' || n.nspname || '.' || c.relname || '.' || t.tgname || '|' || pg_get_triggerdef(t.oid)
    FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE NOT t.tgisinternal AND n.oid NOT IN (SELECT oid FROM sysns)
  UNION ALL SELECT 'fn|' || n.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')|' ||
             md5(pg_get_functiondef(p.oid)) || '|' || p.prosecdef::text || '|' || pg_get_userbyid(p.proowner) || '|' || coalesce(p.proacl::text, '')
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE p.prokind IN ('f', 'p') AND n.oid NOT IN (SELECT oid FROM sysns) AND p.oid NOT IN (SELECT objid FROM ext_objs)
  UNION ALL SELECT 'view|' || n.nspname || '.' || c.relname || '|' || md5(pg_get_viewdef(c.oid))
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('v', 'm') AND n.oid NOT IN (SELECT oid FROM sysns) AND c.oid NOT IN (SELECT objid FROM ext_objs)
  UNION ALL SELECT 'type|' || n.nspname || '.' || t.typname || '|' || t.typtype::text FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE t.typtype IN ('e', 'd', 'c') AND n.oid NOT IN (SELECT oid FROM sysns) AND t.oid NOT IN (SELECT objid FROM ext_objs)
      AND NOT EXISTS (SELECT 1 FROM pg_class r WHERE r.reltype = t.oid)
  UNION ALL SELECT 'enum|' || t.typname || '|' || string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
    FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid GROUP BY t.typname
  UNION ALL SELECT 'db|' || datname || '|' || pg_get_userbyid(datdba) || '|' || pg_encoding_to_char(encoding) || '|' || datcollate || '|' || datctype
    FROM pg_database WHERE datname = current_database()
  UNION ALL SELECT 'dbacl|' || coalesce(datacl::text, '') FROM pg_database WHERE datname = current_database()
) q ORDER BY 1;
