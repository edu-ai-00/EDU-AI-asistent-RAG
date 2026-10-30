#!/usr/bin/env bash
# Measure rule-based question variants against the curated-QA path.
#
#   tools/lab/variants/run.sh path/to/qa_pack.xlsx [--limit 5]
#
# Runs in the RUNNING edu_ai_search container (it needs the database), inside
# one transaction that is rolled back: nothing stays in the database.
# Results: $EDU_AI_STATE_DIR/variants/<workbook>.variants.csv + .summary.json —
# outside the repository, the QA set is private data.
set -euo pipefail

SRC=${EDU_AI_SOURCE_ROOT:-$(cd "$(dirname "$0")/../../.." && pwd)}
STATE=${EDU_AI_STATE_DIR:-$HOME/edu-ai/state}
CT=${VARIANTS_CONTAINER:-edu_ai_search}

INPUT=${1:?usage: run.sh WORKBOOK.xlsx [--sheet NAME] [--limit N] [--entity CODE] [--with-sql FILE]}
shift
[ -f "$INPUT" ] || { echo "no such file: $INPUT" >&2; exit 1; }
OUT="$STATE/variants"
mkdir -p "$OUT"
WORK=/tmp/lab_variants
ENV_FILE=${EDU_AI_CONFIG_DIR:-$HOME/edu-ai/config}/.env
# --with-sql FILE: copy the file in and hand measure.py its container path
ARGS=() SQL=()
while [ $# -gt 0 ]; do
  if [ "$1" = "--with-sql" ]; then
    [ -f "${2:-}" ] || { echo "no such file: ${2:-}" >&2; exit 1; }
    SQL+=("$2"); ARGS+=(--with-sql "$WORK/sql/$(basename "$2")"); shift 2
  else
    ARGS+=("$1"); shift
  fi
done
# Credentials never echoed. The search role is read-only, so the rows go in as
# the QA ingest role; a --with-sql migration needs the migrator, which acts as
# edu_ai_owner (SET LOCAL ROLE, inside the rolled-back transaction).
if [ ${#SQL[@]} -gt 0 ]; then
  LAB_PG_USER=edu_ai_migrator LAB_PG_ROLE=edu_ai_owner
  LAB_PG_PASSWORD=$(sed -n 's/^POSTGRES_MIGRATOR_PASSWORD=//p' "$ENV_FILE")
else
  LAB_PG_USER=$(sed -n 's/^POSTGRES_DB_USER=//p' "$ENV_FILE") LAB_PG_ROLE=
  LAB_PG_PASSWORD=$(sed -n 's/^POSTGRES_PASSWORD=//p' "$ENV_FILE")
fi
export LAB_PG_USER LAB_PG_PASSWORD LAB_PG_ROLE

cleanup() { docker exec "$CT" rm -rf "$WORK" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker exec "$CT" mkdir -p "$WORK/lab" "$WORK/data" "$WORK/out" "$WORK/sql"
for f in "${SQL[@]}"; do docker cp -q "$f" "$CT:$WORK/sql/"; done
docker cp -q "$SRC/tools/lab/variants" "$CT:$WORK/lab/"
docker cp -q "$SRC/tools/lab/neutral_answers" "$CT:$WORK/lab/"
docker cp -q "$INPUT" "$CT:$WORK/data/"
docker exec -w /app -e PYTHONPATH=/app -e LAB_PG_USER -e LAB_PG_PASSWORD -e LAB_PG_ROLE "$CT" python "$WORK/lab/variants/measure.py" \
  --input "$WORK/data/$(basename "$INPUT")" --out "$WORK/out" "${ARGS[@]}"
docker cp -q "$CT:$WORK/out/." "$OUT/"
echo "uloženo do $OUT" >&2
