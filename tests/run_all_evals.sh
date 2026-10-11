#!/usr/bin/env bash
# Run all three synthetic public question sets through /search,
# print each set's outcome distribution, then export the combined per-query table
# from qa_query_log to eval_search_report.csv.
#
# Usage (from anywhere):   bash tests/run_all_evals.sh
# Needs: the dev stack running + `requests` on the host. Hits search on loopback
# (127.0.0.1:8090), so it bypasses the nginx auth edge.

set -euo pipefail
cd "$(dirname "$0")/.."

FILES=(eval_prijimacky_questions.txt eval_ucitele_questions.txt eval_cjm_questions.txt)

for f in "${FILES[@]}"; do
  echo
  echo "════════════════════════════════════════════════════════════"
  echo "  $f"
  echo "════════════════════════════════════════════════════════════"
  python3 tests/eval_search.py "tests/$f" | sed -n '/^==== /,$p'   # summary only
done

echo
echo "════════════════════════════════════════════════════════════"
echo "  Export do eval_search_report.csv (z qa_query_log)"
echo "════════════════════════════════════════════════════════════"
# latest logged run per (e-mail, otázka) for the three eval subjects
docker exec -i edu_ai_postgres psql -U edu_ai -d edu_ai -c "\copy (
  SELECT session_id AS \"Predmet_emailu\", raw_query AS \"Otazka\",
         norm_query AS \"Slova_norm\", lemma_query AS \"Slova_lemma\",
         stage AS \"Vysledek\", answer_source AS \"Strategie\",
         match_score AS \"Skore\", latency_ms AS \"Doba_ms\"
  FROM (
    SELECT DISTINCT ON (session_id, raw_query) *
    FROM edu_ai.qa_query_log
    WHERE session_id LIKE 'RAG EDU-AI%'
    ORDER BY session_id, raw_query, id DESC
  ) q ORDER BY session_id, raw_query
) TO STDOUT WITH CSV HEADER" > eval_search_report.csv

echo "CSV: $(( $(wc -l < eval_search_report.csv) - 1 )) řádků → $(pwd)/eval_search_report.csv"
