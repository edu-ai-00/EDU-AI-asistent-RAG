#!/usr/bin/env bash
# Start the neutral answers lab inside the edu_ai_search image, so the machine
# view runs the real normalizer (eduai_normalize + simplemma) — no new dependency.
#
#   tools/lab/neutral_answers/run.sh ~/Downloads/faq_jpz_9-26.xlsx
#   tools/lab/neutral_answers/run.sh "~/Downloads/Otázky EDU.xlsx" --sheet Přijímačkymaturita
#
# Then open http://127.0.0.1:8770/ (published on localhost only).
# Decisions: $EDU_AI_STATE_DIR/neutral_answers/<workbook>.decisions.json — outside
# the repository, QA packs are private data.
set -euo pipefail

SRC=${EDU_AI_SOURCE_ROOT:-$(cd "$(dirname "$0")/../../.." && pwd)}
STATE=${EDU_AI_STATE_DIR:-$HOME/edu-ai/state}
IMAGE=${NEUTRAL_ANSWERS_IMAGE:-edu_ai_search:1.0}
PORT=${NEUTRAL_ANSWERS_PORT:-8770}

INPUT=${1:?usage: run.sh WORKBOOK.xlsx [--sheet NAME] [--subject uchazeč|žák]}
shift
[ -f "$INPUT" ] || { echo "no such file: $INPUT" >&2; exit 1; }
IN_DIR=$(cd "$(dirname "$INPUT")" && pwd)
IN_FILE=$(basename "$INPUT")
OUT="$STATE/neutral_answers"
mkdir -p "$OUT"
TTY=(); [ -t 0 ] && TTY=(-it)

exec docker run --rm "${TTY[@]}" --name edu_ai_neutral_answers \
  --no-healthcheck --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  -p "127.0.0.1:${PORT}:8770" \
  -v "$SRC/tools/lab/neutral_answers:/lab:ro" \
  -v "$SRC/contracts/parity:/parity:ro" \
  -v "$IN_DIR:/data:ro" \
  -v "$OUT:/out" \
  --entrypoint python "$IMAGE" /lab/server.py \
  --input "/data/$IN_FILE" --state "/out/${IN_FILE%.*}.decisions.json" \
  --stoplist /parity/stoplist.json --host 0.0.0.0 --port 8770 "$@"
