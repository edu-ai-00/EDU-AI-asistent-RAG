#!/usr/bin/env bash
# Lint of QA entries and their alternatives before an upload, in the running search container.
#
#   tools/lab/regression/alt_lint.sh PACKS_DIR [--out report.csv] [--only KEY ...] [--t-collision 0.9]
#
# Read-only: it reads the pack tables in PACKS_DIR, lemmatizes with the same `eduai_normalize`
# as the search service and prints what each alternative adds to the entry (see alt_lint.py).
# The packs and the report are private data: keep them outside the repository.
set -euo pipefail

SRC=${EDU_AI_SOURCE_ROOT:-$(cd "$(dirname "$0")/../../.." && pwd)}
CT=${VARIANTS_CONTAINER:-edu_ai_search}
DIR=${1:?usage: alt_lint.sh PACKS_DIR [--out report.csv] [--only KEY ...] [--t-collision 0.9]}
shift
[ -d "$DIR" ] || { echo "no such directory: $DIR" >&2; exit 1; }
OUT=""
ARGS=()
while [ $# -gt 0 ]; do
  if [ "$1" = "--out" ]; then OUT=${2:?--out FILE}; ARGS+=(--out /tmp/lab_altlint/report.csv); shift 2
  else ARGS+=("$1"); shift; fi
done
WORK=/tmp/lab_altlint
cleanup() { docker exec "$CT" rm -rf "$WORK" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker exec "$CT" mkdir -p "$WORK/lab" "$WORK/packs" "$WORK/parity"
docker cp -q "$SRC/tools/lab/regression" "$CT:$WORK/lab/"
docker cp -q "$SRC/tools/lab/neutral_answers" "$CT:$WORK/lab/"
docker cp -q "$SRC/contracts/parity/stoplist.json" "$CT:$WORK/parity/stoplist.json"
docker cp -q "$DIR/." "$CT:$WORK/packs/"
docker exec -w /app "$CT" python "$WORK/lab/regression/alt_lint.py" "$WORK/packs" \
  --stoplist "$WORK/parity/stoplist.json" "${ARGS[@]}"
if [ -n "$OUT" ]; then mkdir -p "$(dirname "$OUT")"; docker cp -q "$CT:$WORK/report.csv" "$OUT"; echo "uloženo do $OUT" >&2; fi
