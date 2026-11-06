#!/usr/bin/env bash
# Check after a step: does everything still work? (the automatic half of METHOD.md §4)
#
#   tools/lab/regression/check_step.sh [--regression]
#
# Always: Python compiles, unit tests of the lab tools and of the migration
# runner, make docs-check, the search-image tests of this checkout's code
# (negation, stop-words, diacritics, query repair), the normalization parity
# check, the git state. With --regression also the regression set against the
# accepted baseline ($EDU_AI_STATE_DIR/regression/accepted.json), with the
# migrations 218-221 applied in the rolled-back transaction.
# Exit code 0 = all checks passed; the last lines say what did not.
set -uo pipefail

SRC=${EDU_AI_SOURCE_ROOT:-$(cd "$(dirname "$0")/../../.." && pwd)}
CT=${VARIANTS_CONTAINER:-edu_ai_search}
STATE=${EDU_AI_STATE_DIR:-$HOME/edu-ai/state}
cd "$SRC" || exit 2
FAILED=()
ok()   { echo "  ok    $1"; }
fail() { echo "  FAIL  $1"; FAILED+=("$1"); }
step() { echo; echo "== $1"; }

step "Python compiles"
if python3 -m py_compile $(git ls-files '*.py' 'search/*.py' | grep -E '^(search/app|tools/lab|tests/test_(lab|query|neutral)|scripts)' ) 2>/tmp/check_step.err; then
  ok "py_compile"; else fail "py_compile: $(head -c 200 /tmp/check_step.err)"; fi

step "Unit tests on the host"
for t in test_lab_regression test_lab_variants test_neutral_answers test_migrate_lint test_migrate_fingerprint; do
  if [ -f "tests/$t.py" ]; then
    if python3 "tests/$t.py" >/tmp/check_step.out 2>&1; then ok "$t ($(grep -E '^Ran ' /tmp/check_step.out | head -1))"
    else fail "$t"; tail -5 /tmp/check_step.out; fi
  fi
done

step "Documentation"
if make -s docs-check >/tmp/check_step.out 2>&1; then ok "docs-check"; else fail "docs-check"; tail -5 /tmp/check_step.out; fi

if docker ps --format '{{.Names}}' | grep -qx "$CT"; then
  # tests piped over stdin take their repository root from the working dir's PARENT
  # (test_stopword_surface reads <root>/contracts/parity/stoplist.json)
  ROOT=/tmp/check_step; W=$ROOT/run
  docker exec "$CT" rm -rf "${ROOT:?}" >/dev/null 2>&1
  docker exec "$CT" mkdir -p $W $ROOT/contracts && docker cp -q search/app "$CT:$W/" && docker cp -q tests "$CT:$W/" \
    && docker cp -q contracts/parity "$CT:$ROOT/contracts/parity" && docker cp -q contracts/parity "$CT:$W/parity-src"
  step "Tests in the search image (the code of THIS checkout)"
  for t in test_negation_lemma test_stopword_surface test_diacritics_restore test_query_repair; do
    [ -f "tests/$t.py" ] || continue
    if docker exec -i -w $W -e PYTHONPATH=$W:/app "$CT" python - < "tests/$t.py" >/tmp/check_step.out 2>&1 \
       && tail -1 /tmp/check_step.out | grep -q '^OK'; then ok "$t"
    else fail "$t"; tail -6 /tmp/check_step.out; fi
  done
  step "Normalization parity (AGENTS.md: before and after any normalization change)"
  if docker exec -i -w $W/parity-src -e PYTHONPATH=$W:/app "$CT" python - $W/parity-src \
       < contracts/parity/check_vectors.py >/tmp/check_step.out 2>&1; then ok "parity vectors"
  else fail "parity vectors"; tail -6 /tmp/check_step.out; fi
  docker exec "$CT" rm -rf "${ROOT:?}" >/dev/null 2>&1
else
  step "Search image"
  fail "container $CT is not running: image tests and parity not checked"
fi

if [ "${1:-}" = "--regression" ]; then
  step "Regression set against the accepted baseline"
  R=$STATE/regression; M=postgres/migrations
  if [ ! -f "$R/accepted.json" ] || [ ! -f "$R/cases.json" ]; then
    fail "missing $R/accepted.json or cases.json (see README.md, 'Use')"
  else
    tools/lab/regression/run.sh "$STATE/stem_vs_lemma/otazky_idx.json" "$R/cases.json" "$R/check_now.json" \
      --with-sql $M/218_coverage_stop_politeness.sql --with-sql $M/219_qa_form_lemma_only.sql \
      --with-sql $M/220_qa_lexical_query_repair.sql \
      --with-sql $M/221_qa_lexical_fetch_k.sql 2>&1 | grep -E '^(A|B|ALL) '
    if python3 tools/lab/regression/compare.py --cases "$R/cases.json" --baseline "$R/accepted.json" \
         --now "$R/check_now.json" --show 5 --by-split; then ok "regression: no new confident wrong answer"
    else fail "regression: a case turned into a confident wrong answer (listed above)"; fi
    # the gold set from the curator's decisions about logged questions (build_gold.py): the confirmed
    # cases (gold_verified.json) if there are any, else the weak topic-level labels (gold_cases.json)
    G=gold_cases; [ -f "$R/gold_verified.json" ] && G=gold_verified
    if [ -f "$R/$G.json" ] && [ -f "$R/${G}_accepted.json" ]; then
      tools/lab/regression/run.sh "$STATE/stem_vs_lemma/otazky_idx.json" "$R/$G.json" "$R/${G}_now.json" \
        --with-sql $M/218_coverage_stop_politeness.sql --with-sql $M/219_qa_form_lemma_only.sql \
        --with-sql $M/220_qa_lexical_query_repair.sql --with-sql $M/221_qa_lexical_fetch_k.sql 2>&1 | grep -E '^G '
      if python3 tools/lab/regression/compare.py --cases "$R/$G.json" --baseline "$R/${G}_accepted.json" \
           --now "$R/${G}_now.json" --show 5 --by-split; then ok "gold set ($G): no new confident wrong answer"
      else fail "gold set ($G): a case turned into a confident wrong answer (listed above)"; fi
    fi
  fi
fi

step "Git state"
git status --short | sed 's/^/  /'
[ -z "$(git status --short)" ] && ok "working tree clean" || echo "  (uncommitted changes above: intended?)"
echo "  commits not pushed: $(git rev-list --count @{u}..HEAD 2>/dev/null || git rev-list --count main..HEAD)"
for c in b03e_pg_a edu_ai_prodcopy; do
  docker ps --format '{{.Names}}' | grep -qx "$c" && echo "  note: container $c is running (leftover from a measurement?)"
done

echo
if [ ${#FAILED[@]} -eq 0 ]; then echo "ALL CHECKS PASSED"; exit 0; fi
echo "FAILED (${#FAILED[@]}):"; printf '  - %s\n' "${FAILED[@]}"; exit 1
