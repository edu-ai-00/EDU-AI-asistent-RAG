#!/usr/bin/env bash
# Start the neutral answers lab inside the edu_ai_search image, so the machine
# view runs the real normalizer (eduai_normalize + simplemma) — no new dependency.
#
#   tools/lab/neutral_answers/run.sh path/to/qa_pack.xlsx
#   tools/lab/neutral_answers/run.sh path/to/workbook.xlsx --sheet NAME
#   tools/lab/neutral_answers/run.sh pack.csv --queue path/to/queue.json \
#       --topics path/to/topics.json     (topics.py; optional)
#
# Then open http://127.0.0.1:8770/ (published on localhost only).
# Decisions: $EDU_AI_STATE_DIR/neutral_answers/<workbook>.decisions.json — outside
# the repository, QA packs are private data.
set -euo pipefail

SRC=${EDU_AI_SOURCE_ROOT:-$(cd "$(dirname "$0")/../../.." && pwd)}
STATE=${EDU_AI_STATE_DIR:-$HOME/edu-ai/state}
IMAGE=${NEUTRAL_ANSWERS_IMAGE:-edu_ai_search:1.0}
PORT=${NEUTRAL_ANSWERS_PORT:-8770}
# a second instance (a trial on another port) needs another container name
NAME=${NEUTRAL_ANSWERS_NAME:-edu_ai_neutral_answers}

INPUT=${1:?usage: run.sh WORKBOOK.xlsx|PACK.csv [--sheet NAME] [--subject uchazeč|žák] [--osloveni vy|ty|beze] [--queue QUEUE.json] [--topics TOPICS.json]}
shift
[ -f "$INPUT" ] || { echo "no such file: $INPUT" >&2; exit 1; }
IN_DIR=$(cd "$(dirname "$INPUT")" && pwd)
IN_FILE=$(basename "$INPUT")
# --queue FILE (questions from the log, log_queue.py) and --topics FILE (topics.py):
# each directory mounted read-only as /queue, /topics
ARGS=() QMOUNT=()
while [ $# -gt 0 ]; do
  if [ "$1" = "--queue" ] || [ "$1" = "--topics" ]; then
    [ -f "${2:-}" ] || { echo "no such file: ${2:-}" >&2; exit 1; }
    M="/${1#--}"
    QMOUNT+=(-v "$(cd "$(dirname "$2")" && pwd):$M:ro"); ARGS+=("$1" "$M/$(basename "$2")"); shift 2
  else
    ARGS+=("$1"); shift
  fi
done
OUT="$STATE/neutral_answers"
mkdir -p "$OUT"
TTY=(); [ -t 0 ] && TTY=(-it)

exec docker run --rm "${TTY[@]}" --name "$NAME" \
  --no-healthcheck --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  -p "127.0.0.1:${PORT}:8770" \
  -v "$SRC/tools/lab/neutral_answers:/lab:ro" \
  -v "$SRC/contracts/parity:/parity:ro" \
  -v "$IN_DIR:/data:ro" \
  -v "$OUT:/out" "${QMOUNT[@]}" \
  --entrypoint python "$IMAGE" /lab/server.py \
  --input "/data/$IN_FILE" --state "/out/${IN_FILE%.*}.decisions.json" \
  --stoplist /parity/stoplist.json --host 0.0.0.0 --port 8770 "${ARGS[@]}"
