#!/bin/sh
# last change in code by Jan Drozd (2026-09-06).
#
# Regenerate postgres/init_clean/ from a CONTROLLED REFERENCE DATABASE.
#
#     sh scripts/gen_clean_install.sh                    # dry run: writes to /tmp, diffs
#     sh scripts/gen_clean_install.sh --go               # overwrite postgres/init_clean/
#     sh scripts/gen_clean_install.sh --seed-only --go   # preserve schema; dump seed only
#
# Organization-only regeneration: apply seed_clean_organizations.py output to
# an isolated 00/01/02/04 reference with the role .psql includes. Do not run 03
# on that dump reference: it remains a separate fresh-install data input.
# Verify the result on a second empty reference that runs all 00-04 inputs.
#
# WHY THIS FILE EXISTS. AGENTS.md forbids hand-editing the clean-install dumps
# and requires regenerating them — and until now there was nothing to regenerate
# them WITH. The two files carried a "dumped from the verified live database"
# header and no command, so the only way to add a column was the hand edit the
# rule forbids, which is why the consolidated install has been drifting behind
# postgres/init since about migration 205 (documented in
# docs/deployments/2026-09-05_stoplist_and_normalize_3.md).
#
# ── WHAT A "CONTROLLED REFERENCE DATABASE" IS, AND IS NOT ────────────────────
#
# IS:  a database built by 00_roles_and_databases.sql + 01_schema.sql +
#      02_seed_config.sql on an EMPTY volume, then brought forward by ONLY the
#      named migrations of the change being consolidated, applied one by one.
#      Never the whole postgres/init history: it is not an install path (201/202
#      must not run). That is reproducible from this repository alone and
#      contains only the committed seed and explicitly approved public metadata
#      from postgres/seed/clean_organizations.json (never a production restore).
#
# IS NOT: production, or a restore of it. Never. 02_seed_config.sql is DATA and
#      it goes into a public repository — a pg_dump of production here would
#      publish staff or curated answers. Public organization metadata travels
#      only through the reviewed seven-field seed input, without source config.
#
# The default target is the container named by PG (the local dev stack unless
# overridden); prefer an isolated reference built as the guard below describes.
# It refuses to run against a database that looks like production.
#
# ── WHY THE DEFAULT IS A DRY RUN ────────────────────────────────────────────
#
# The regenerated schema carries EVERYTHING the reference database has, which is
# every migration applied to it — not only the change you are
# working on. That is the correct end state, and it is also a diff nobody can
# review next to a feature. So this writes to /tmp and shows the diff, and
# `--go` is a separate, deliberate act with its own approval.
#
set -e

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${OUT:-/tmp/init_clean}
PG=${PG:-edu_ai_postgres}
GO=0
SEED_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --go) GO=1 ;;
        --seed-only) SEED_ONLY=1 ;;
        *) echo "usage: $0 [--seed-only] [--go]" >&2; exit 2 ;;
    esac
done

mkdir -p "$OUT"

# ── the guard: never dump a database holding real content ───────────────────
# Not a permission check — a mistake check. 02_seed_config.sql is committed, so
# dumping unapproved content is an irreversible disclosure. The
# test is content-based rather than a hostname, because a restore of production
# on a dev machine has a dev hostname.
REAL=$(docker exec -i "$PG" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "
  SELECT (SELECT count(*) FROM edu_ai.document_metadata)
       + (SELECT count(*) FROM edu_ai.qa_entries)
       + (SELECT count(*) FROM edu_ai.question_answer_log)"' | tr -d '[:space:]')
FIXTURES=$(docker exec -i "$PG" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "
  SELECT count(*) FROM edu_ai.qa_entries WHERE entry_key LIKE '"'"'ds-%'"'"'"' | tr -d '[:space:]')
echo "reference database: content rows=$REAL (test fixtures among them: $FIXTURES)"
if [ "${REAL:-0}" -gt 0 ]; then
    echo
    echo "REFUSING: this database holds content rows (documents / QA answers / query log)."
    echo "02_seed_config.sql is COMMITTED, so a dump of real content would publish it."
    echo "Build the reference database from an empty, isolated volume instead:"
    echo
    echo "  python tests/migrate_test_env.py up b03e_ref        # clean install, own container"
    echo "  # then ONLY the named migrations of this change, one by one:"
    echo "  docker exec -i b03e_ref psql -U edu_ai -d edu_ai -v ON_ERROR_STOP=1 < postgres/init/<NNN>_<name>.sql"
    echo "  PG=b03e_ref sh scripts/gen_clean_install.sh"
    echo "  (never the whole postgres/init history)"
    echo
    echo "Set FORCE=1 to override — only for a database you know is synthetic."
    [ "${FORCE:-0}" = "1" ] || exit 1
    echo "FORCE=1 given; continuing."
fi

# ── schema ──────────────────────────────────────────────────────────────────
# --schema-only + the same exclusions the committed header documents. Ordering
# is pg_dump's, so two runs against the same database produce the same bytes.
if [ "$SEED_ONLY" = "0" ]; then
echo "dumping schema…"
docker exec -i "$PG" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
      --schema-only --schema=edu_ai --no-owner --no-privileges \
      --exclude-table="edu_ai.langchain_pg_*"' > "$OUT/01_schema.body.sql"
fi

# ── configuration seed ──────────────────────────────────────────────────────
# The table list is the one 02_seed_config.sql's own header names: configuration
# and registries, never content. --column-inserts so a column added later does
# not shift a positional INSERT; --disable-triggers for the legal_entity
# self-reference cycle.
SEED_TABLES="code_list taxonomy_rules taxonomy_binding embedding_registry
             prompt_registry system_message ui_label qa_stopword qa_synonym
             qa_blocklist chunk_exclusion_rule answer_exclusion_rule
             grade_term subject_term validity_period settings legal_entity
             staff_user"
ARGS=""
for t in $SEED_TABLES; do ARGS="$ARGS --table=edu_ai.$t"; done
echo "dumping configuration seed ($(echo $SEED_TABLES | wc -w) tables)…"
docker exec -i "$PG" sh -c "pg_dump -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" \
      --data-only --column-inserts --disable-triggers --no-owner \
      --no-privileges $ARGS" > "$OUT/02_seed_config.body.sql"

# ── splice: hand-written prologue + generated body ──────────────────────────
#
# The schema keeps its existing hand-written prologue (header, SET block,
# CREATE SCHEMA and extensions). The seed prologue is a versioned input in
# postgres/seed/02_seed_config.prologue.sql, so repeated data-only generation
# cannot preserve an obsolete metadata description or accumulate date stamps.
# The generated seed body is kept whole from pg_dump's header onward.
#
prologue_schema() {  # everything before pg_dump's first object banner
    awk '/^-- Name: /{exit} {print}' "$1"
}
body_schema() {
    # pg_dump from the first object banner AFTER its own CREATE SCHEMA.
    #
    # Starting at the first `-- Name: ` banner is one line too early: that
    # banner is pg_dump's own `-- Name: edu_ai; Type: SCHEMA`, whose body is a
    # second `CREATE SCHEMA edu_ai;` — and the hand-written prologue has already
    # issued it. Verified by actually installing the result: the container died
    # with `ERROR: schema "edu_ai" already exists`. The committed file begins at
    # `-- Name: SCHEMA edu_ai; Type: COMMENT`, i.e. exactly here.
    awk '
        /^CREATE SCHEMA edu_ai;/ { seen_schema = 1; next }
        seen_schema && /^-- Name: / { p = 1 }
        p { print }
    ' "$1"
}
stamp_line() {
    printf -- "--\n-- Everything below this line is GENERATED by scripts/gen_clean_install.sh (%s).\n-- Do not edit it by hand — regenerate.\n--\n\n" "$(date -u +%Y-%m-%d)"
}

if [ "$SEED_ONLY" = "0" ]; then
    prologue_schema "$ROOT/postgres/init_clean/01_schema.sql" > "$OUT/01_schema.sql"
    stamp_line >> "$OUT/01_schema.sql"
    body_schema "$OUT/01_schema.body.sql" >> "$OUT/01_schema.sql"
else
    # A data-only change must not rewrite the schema or its generation stamp.
    cp "$ROOT/postgres/init_clean/01_schema.sql" "$OUT/01_schema.sql"
fi

cat "$ROOT/postgres/seed/02_seed_config.prologue.sql" > "$OUT/02_seed_config.sql"
printf '\n' >> "$OUT/02_seed_config.sql"
stamp_line >> "$OUT/02_seed_config.sql"
cat "$OUT/02_seed_config.body.sql" >> "$OUT/02_seed_config.sql"
rm -f "$OUT/01_schema.body.sql" "$OUT/02_seed_config.body.sql"

echo
echo "── diff against the committed files ──"
for n in 01_schema 02_seed_config; do
    if diff -q "$ROOT/postgres/init_clean/$n.sql" "$OUT/$n.sql" >/dev/null 2>&1; then
        echo "  $n.sql: identical"
    else
        A=$(diff "$ROOT/postgres/init_clean/$n.sql" "$OUT/$n.sql" | grep -c '^>' || true)
        R=$(diff "$ROOT/postgres/init_clean/$n.sql" "$OUT/$n.sql" | grep -c '^<' || true)
        echo "  $n.sql: +$A / -$R lines  (full diff: diff postgres/init_clean/$n.sql $OUT/$n.sql)"
    fi
done

if [ "$GO" = "1" ]; then
    [ "$SEED_ONLY" = "1" ] || cp "$OUT/01_schema.sql" "$ROOT/postgres/init_clean/01_schema.sql"
    cp "$OUT/02_seed_config.sql" "$ROOT/postgres/init_clean/02_seed_config.sql"
    echo
    echo "WRITTEN to postgres/init_clean/."
    echo "NOW VERIFY BOTH PATHS, or the install is unproven:"
    echo "  fresh : initialise another EMPTY isolated reference with all 00-04 SQL and .psql inputs"
    echo "          (admin bootstrap role and five distinct generated role passwords)."
    echo "  existing installations are NOT upgraded by init_clean and no supported upgrade is"
    echo "          implied by this regeneration (takeover is a separate decision, D11)."
    echo "  manifest: the files changed, so postgres/baseline/B216.json is stale. Commit them, install"
    echo "          them on an EMPTY synthetic database, then regenerate it:"
    echo "          python scripts/migrate.py gen-baseline --container <name> --confirm-isolated"
else
    echo
    echo "DRY RUN — nothing written. Files are in $OUT."
    echo "Review the diff first: it carries everything the reference has beyond the"
    echo "committed files, not only the change you are working on. Then --go."
fi
