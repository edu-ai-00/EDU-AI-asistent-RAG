# Migrations from 217 on

New installations start from baseline **B216** (`postgres/init_clean`); the
numbered history before it stays in `postgres/init/` and is never run on them.
Files here are applied by `scripts/migrate.py apply` and recorded one by one in
`edu_ai.schema_migration`. Rules and behaviour: `specs/active/spec.migration-registry.md`.

- Name: `NNN_short_name.sql`, number >= 217, ascending and unique.
- **Never control the transaction.** No `BEGIN`, `COMMIT`, `END`, `ROLLBACK`,
  `CREATE INDEX CONCURRENTLY`, `VACUUM`, psql `\` commands. The runner wraps each
  file and its registry row in one transaction; `migrate.py plan` lints the file.
- `BEGIN ... END` inside function bodies and `DO` blocks is fine.
- A migration that changes **data** needs its own `RAISE EXCEPTION` self-check;
  the schema fingerprint cannot see data.
- After adding or changing a file, regenerate the release manifest on an isolated
  reference: `migrate.py gen-release --release NAME` (never edit it by hand).
