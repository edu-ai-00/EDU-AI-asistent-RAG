#!/bin/sh
# last change in code by Jan Drozd (2026-08-03).
#
# Bring an EXISTING edu_ai database up to the postgres/init_clean baseline.
#
# init_clean only ever runs on an empty volume, so an install that already has
# data cannot use it. This walks postgres/init instead — the numbered history is
# exactly what that directory is for.
#
# Run it INSIDE the postgres container, with postgres/init copied to /tmp/init:
#
#     docker cp postgres/init  edu_ai_postgres:/tmp/init
#     docker cp scripts/align_existing_db.sh edu_ai_postgres:/tmp/
#     docker exec -e DB=edu_ai edu_ai_postgres sh /tmp/align_existing_db.sh
#
# Take a dump first. This is not reversible.
#
# ── Why three migrations are skipped ──────────────────────────
# The production database was clean-installed from a mid-series snapshot and
# hand-patched afterwards, so in places it is AHEAD of the numbered history.
# Three files therefore fail — not because the work is missing, but because it
# is already there in a later form:
#
#   37  wants document_type_taxonomy.label_cs; the table already has labels jsonb
#   40  is the very migration that replaces label_cs/label_en WITH labels jsonb
#   42  renames is_default -> is_ui_default; is_ui_default already exists
#
# Verified on a restore of production 2026-08-02: 62 of 66 files apply cleanly,
# these three fail as above, and 89 fails for a different reason handled below.
# Re-check SKIP against your own database before trusting it — on an install
# that did NOT drift, all three are needed.
SKIP="37 40 42"

DB="${DB:-edu_ai}"
PSQL="psql -U ${PGUSER:-edu_ai} -d $DB -v ON_ERROR_STOP=1 -q"

die() { echo "FATAL: $*" >&2; exit 1; }

[ -d /tmp/init ] || die "/tmp/init is missing — docker cp postgres/init first"

echo "== aligning database '$DB' =="
$PSQL -c "SELECT 1" >/dev/null 2>&1 || die "cannot connect to $DB"

# ── The entities have to exist before 89 ──────────────────────
# 89 inserts the UNITS of Nová Ves and points them at SKOLA_NOVA_VES as parent,
# but nothing in postgres/init ever creates that parent: which schools exist is
# deployment data, not schema, and on a fresh install it arrives with the seed.
# So it is declared here. EDIT THIS for your deployment — the codes must match
# what the units in 89 expect, and what the documents will be assigned to.
# Only the columns migration 76 created: `kind` does not exist yet — 89 adds it
# a few lines later with DEFAULT 'legal_person', which is what these rows want,
# and its unit_has_parent check is satisfied by a parentless legal person.
seed_entities() {
  echo "-- seeding parent entities (edit this block per deployment)"
  $PSQL <<'SQL'
INSERT INTO edu_ai.legal_entity
    (legal_entity_code, display_name)
VALUES
    ('SKOLA_NOVA_VES',        'Škola Nová Ves - celá organizace'),
    ('ZAKLADNI_SKOLA_LIPOVA', 'Základní škola Lipová')
ON CONFLICT (legal_entity_code) DO NOTHING;
SQL
}

applied=0; skipped=0
cd /tmp/init || die "cannot enter /tmp/init"

for f in $(ls *.sql | sort -t_ -k1,1n); do
    n=$(echo "$f" | cut -d_ -f1)
    case "$n" in ''|*[!0-9]*) continue ;; esac
    [ "$n" -le 30 ] && continue

    for s in $SKIP; do
        if [ "$n" = "$s" ]; then
            printf '%-52s SKIP (already present in a later form)\n' "$f"
            skipped=$((skipped + 1))
            continue 2
        fi
    done

    # 89 needs its parents; seeding earlier would hit a table that 76 has not
    # created yet, so it happens here, immediately before the file that needs it.
    [ "$n" = "89" ] && seed_entities

    printf '%-52s' "$f"
    if $PSQL -f "$f" >/tmp/align.log 2>&1; then
        echo "ok"
        applied=$((applied + 1))
    else
        echo "FAILED"
        echo "---------------------------------------------"
        tail -15 /tmp/align.log
        echo "---------------------------------------------"
        die "stopped at $f — $applied applied, nothing after this ran"
    fi
done

# ── Backfill: without this the migration silently empties the search ──
# 77 adds legal_entity_code to the data tables but cannot know which entity the
# existing rows belong to, so it leaves them NULL. scope.py builds every branch
# of its predicate as `legal_entity_code = ANY(...)`, and NULL matches none of
# them — so after the schema is correct, all pre-existing documents become
# invisible to search. Nothing errors; the answers just stop coming.
#
# OWNER_ENTITY must therefore name the entity the existing documents belong to.
# A wrong value here hides them just as completely as NULL does.
OWNER_ENTITY="${OWNER_ENTITY:-SKOLA_NOVA_VES}"

echo
echo "-- backfilling legal_entity_code = $OWNER_ENTITY on pre-existing rows"
$PSQL <<SQL || die "backfill failed"
DO \$\$
DECLARE t text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM edu_ai.legal_entity
                    WHERE legal_entity_code = '$OWNER_ENTITY') THEN
        RAISE EXCEPTION 'OWNER_ENTITY % is not in legal_entity', '$OWNER_ENTITY';
    END IF;
    FOREACH t IN ARRAY ARRAY['document_chunks', 'document_metadata',
                             'document_elements', 'document_tables',
                             'document_chunk_keywords', 'qa_entries',
                             'qa_variants', 'qa_embedding'] LOOP
        EXECUTE format('UPDATE edu_ai.%I SET legal_entity_code = %L
                         WHERE legal_entity_code IS NULL', t, '$OWNER_ENTITY');
    END LOOP;
END \$\$;
SQL

$PSQL -c "SELECT 'chunks without an entity: ' || count(*)
            FROM edu_ai.document_chunks WHERE legal_entity_code IS NULL;"

# ── Optional: two dead columns ────────────────────────────────
# 32 added is_default; 42 renames it to is_ui_default but only when the new name
# is absent, so a database that already had both keeps the old one forever. The
# values were verified identical and nothing in the codebase reads is_default.
# Dropping them is what makes the schema equal to init_clean; set DROP_VESTIGIAL=1.
if [ "${DROP_VESTIGIAL:-0}" = "1" ]; then
    echo "-- dropping vestigial is_default columns"
    $PSQL -c "ALTER TABLE edu_ai.code_list DROP COLUMN IF EXISTS is_default;
              ALTER TABLE edu_ai.document_type_taxonomy DROP COLUMN IF EXISTS is_default;"
fi

echo
echo "== applied $applied, skipped $skipped =="

# ── Readiness ─────────────────────────────────────────────────
# Two things the migrations cannot supply, and neither of them errors when
# missing — the stack comes up healthy and simply answers nothing:
#
#   no users     search/app/server.py verifies user_id against staff_user and
#                app_user before it retrieves anything, so an empty user table
#                turns EVERY request into 403.
#   no entity    covered by the backfill above, re-checked here.
#
# Both are deployment data: which schools exist and who works at them is not
# something a schema migration can invent. This refuses to call the job done.
echo
echo "== readiness =="
ents=$($PSQL -tAc "SELECT count(*) FROM edu_ai.legal_entity WHERE is_active")
users=$($PSQL -tAc "SELECT (SELECT count(*) FROM edu_ai.staff_user WHERE is_active)
                         + (SELECT count(*) FROM edu_ai.app_user   WHERE is_active)")
orphans=$($PSQL -tAc "SELECT count(*) FROM edu_ai.document_chunks
                       WHERE legal_entity_code IS NULL")
echo "  active entities        : $ents"
echo "  active users           : $users"
echo "  chunks with no entity  : $orphans"

problems=0
[ "$ents"    -eq 0 ] && { echo "  !! no entities — nothing can be searched";      problems=1; }
[ "$orphans" -gt 0 ] && { echo "  !! orphaned chunks — they are invisible";       problems=1; }
[ "$users"   -eq 0 ] && {
    echo "  !! NO USERS — every search will return 403."
    echo "     Insert the real people before serving traffic, e.g.:"
    echo "       INSERT INTO edu_ai.staff_user (user_id, legal_entity_code, display_name)"
    echo "       VALUES (gen_random_uuid(), '$OWNER_ENTITY', 'Ředitelka');"
    problems=1
}
[ "$problems" -eq 1 ] && die "schema is aligned, but the database is NOT ready to serve"

echo
echo "Schema aligned and populated. Diff it against a fresh init_clean install"
echo "before deploying, and re-run the search smoke test."
