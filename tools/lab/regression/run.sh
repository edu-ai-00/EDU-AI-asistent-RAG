#!/usr/bin/env bash
# Regression set: run the cases in the running search container.
#
#   tools/lab/regression/run.sh QUESTIONS.json CASES.json OUT.json [--config JSON] [--with-sql FILE]
#
# One transaction, rolled back: nothing stays in the database. The inputs and the
# results are private data (a school's questions) — keep them outside the
# repository, e.g. in $EDU_AI_STATE_DIR/regression.
set -euo pipefail

SRC=${EDU_AI_SOURCE_ROOT:-$(cd "$(dirname "$0")/../../.." && pwd)}
CT=${VARIANTS_CONTAINER:-edu_ai_search}
Q=${1:?usage: run.sh QUESTIONS.json CASES.json OUT.json [--config JSON] [--with-sql FILE]}
C=${2:?cases}
OUT=${3:?out file}
shift 3
for f in "$Q" "$C"; do [ -f "$f" ] || { echo "no such file: $f" >&2; exit 1; }; done
WORK=/tmp/lab_regression
ENV_FILE=${EDU_AI_CONFIG_DIR:-$HOME/edu-ai/config}/.env
ARGS=() SQL=()
while [ $# -gt 0 ]; do
  if [ "$1" = "--with-sql" ]; then
    [ -f "${2:-}" ] || { echo "no such file: ${2:-}" >&2; exit 1; }
    SQL+=("$2"); ARGS+=(--with-sql "$WORK/sql/$(basename "$2")"); shift 2
  else
    ARGS+=("$1"); shift
  fi
done
# the search role is read-only: the temporary packs go in as the QA ingest role;
# what-if SQL needs the migrator acting as the owner (SET LOCAL ROLE, rolled back)
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

docker exec "$CT" mkdir -p "$WORK/lab" "$WORK/data" "$WORK/sql" "$WORK/code"
for f in "${SQL[@]}"; do docker cp -q "$f" "$CT:$WORK/sql/"; done
docker cp -q "$SRC/tools/lab/regression" "$CT:$WORK/lab/"
docker cp -q "$SRC/tools/lab/variants" "$CT:$WORK/lab/"
docker cp -q "$SRC/tools/lab/neutral_answers" "$CT:$WORK/lab/"
# the search code of THIS checkout (an experiment may not be in the image yet)
docker cp -q "$SRC/search/app" "$CT:$WORK/code/"
docker cp -q "$Q" "$CT:$WORK/data/questions.json"
docker cp -q "$C" "$CT:$WORK/data/cases.json"
docker exec -w /app -e PYTHONPATH=$WORK/code:/app -e LAB_PG_USER -e LAB_PG_PASSWORD -e LAB_PG_ROLE "$CT" \
  python "$WORK/lab/regression/run.py" --questions "$WORK/data/questions.json" \
  --cases "$WORK/data/cases.json" --out "$WORK/results.json" "${ARGS[@]}"
mkdir -p "$(dirname "$OUT")"
docker cp -q "$CT:$WORK/results.json" "$OUT"
echo "uloženo do $OUT" >&2
