# Scoped SQLite QA export contract

The exact approved layout is [sqlite-qa-export.sql](sqlite-qa-export.sql).
`pack_manifest` retains its original eleven columns in order; legacy `subject`
and `grade` are nullable and always SQL NULL. `entries` retains its original five
columns and `variants` its original six (including `stem_text`). All added metadata columns follow those
legacy prefixes in the exact DDL order. The two FTS5 definitions are unchanged.

Input `legal_entity` is required, has no default, and is validated before any
PostgreSQL connection as a safe nonempty code segment: ASCII letters, digits,
underscore or hyphen, with the first character restricted to a letter, digit or underscore (a leading hyphen is rejected). `pack_id` additionally permits internal dots, but neither
value can contain path separators, traversal segments, controls or a trailing
dot. PostgreSQL schema names must be bare SQL identifiers. All data values are
bound parameters. PostgreSQL is read through one read-only REPEATABLE READ
transaction; only the latest active manifest and scoped active entries/variants
are exported. Variant joins also require equal entry/variant entities.

The returned `pack_path` is `<resolved output_dir>/<legal_entity>/<pack_id>.sqlite`.
Entities with the same pack name receive separate files. New entity directories
and temporary artifacts are private; link escapes and nonregular destinations
are refused. The SQLite file is fully built, committed, vacuumed and fsynced
before same-directory atomic replacement. Prepublication failures preserve the
previous complete file. A directory-fsync limitation after publication is
reported in the result as unverified durability, rather than pretending rollback.

Arrays and JSON metadata retain all values as JSON text: SQL NULL remains
SQLite NULL, while an empty array becomes `[]`. UUIDs become text; dates and
timestamps use ISO text; selected active flags become integer 1. Entry and
variant metadata comes from each respective row, without manifest inheritance
or reconstruction. SQLite `entry_count` counts the exported entries.

`schema_version` remains the uploaded QA pack schema version. This contract adds
no runtime version, configuration flag, embeddings, normalization or FTS change.
PostgreSQL schema/indexes and production data remain unchanged. External SQLite
readers are not present in this repository and their compatibility is unverified.

The [QA rules and offline audit](../docs/integrations/offline-qa-rules.md) maps backend
configuration, scoring, dictionaries and scope to this layout. This data export does not
provide a complete offline QA runtime profile or prove backend/client decision parity.
