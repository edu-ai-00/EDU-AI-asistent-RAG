#!/usr/bin/env bash
# Lemmatization or stemming? compare_forms.py in the running search container.
#
#   tools/lab/variants/run_forms.sh QUESTIONS.json PARAPHRASES.json OUT_DIR [--limit N] [--with-sql FILE]
#
# One transaction, rolled back: nothing stays in the database. The inputs and
# the results are private data (a school's packs) — keep them outside the
# repository, e.g. in $EDU_AI_STATE_DIR.
set -euo pipefail

SRC=${EDU_AI_SOURCE_ROOT:-$(cd "$(dirname "$0")/../../.." && pwd)}
CT=${VARIANTS_CONTAINER:-edu_ai_search}
Q=${1:?usage: run_forms.sh QUESTIONS.json PARAPHRASES.json OUT_DIR [--limit N]}
P=${2:?paraphrases}
OUT=${3:?out dir}
shift 3
for f in "$Q" "$P"; do [ -f "$f" ] || { echo "no such file: $f" >&2; exit 1; }; done
mkdir -p "$OUT"
WORK=/tmp/lab_forms
ENV_FILE=${EDU_AI_CONFIG_DIR:-$HOME/edu-ai/config}/.env
# --with-sql FILE: copied in, run inside the rolled-back transaction
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

docker exec "$CT" mkdir -p "$WORK/lab" "$WORK/data" "$WORK/out" "$WORK/sql"
for f in "${SQL[@]}"; do docker cp -q "$f" "$CT:$WORK/sql/"; done
docker cp -q "$SRC/tools/lab/variants" "$CT:$WORK/lab/"
docker cp -q "$SRC/tools/lab/neutral_answers" "$CT:$WORK/lab/"
# the search code of THIS checkout (an experiment may not be in the image yet)
docker exec "$CT" mkdir -p "$WORK/code"
docker cp -q "$SRC/search/app" "$CT:$WORK/code/"
docker cp -q "$Q" "$CT:$WORK/data/questions.json"
docker cp -q "$P" "$CT:$WORK/data/paraphrases.json"
docker exec -w /app -e PYTHONPATH=$WORK/code:/app -e LAB_PG_USER -e LAB_PG_PASSWORD -e LAB_PG_ROLE "$CT" python "$WORK/lab/variants/compare_forms.py" \
  --questions "$WORK/data/questions.json" --paraphrases "$WORK/data/paraphrases.json" --out "$WORK/out" "${ARGS[@]}"
docker cp -q "$CT:$WORK/out/." "$OUT/"
echo "uloženo do $OUT" >&2
