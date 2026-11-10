"""Set C: weak real labels from the query log — which offered answer the asker CLICKED.

  python3 make_clicks.py --container edu_ai_prodcopy --out clicks.json
        --entity CODE [--packs faq-jpz faq-provoz]

A click is not recorded (`selected_answer_key` is empty), but the chat sends the
text of a clicked button as the NEXT question of the conversation. So: a logged
query that offered suggestions, followed in the same conversation by a question
that equals one of the offered questions = that suggestion was chosen. The pair
(the first query → the chosen entry) is a weak label: biased towards what the old
version offered, and it knows nothing about those who left without a click.

Only offers that stand in for an answer count (stage `suggest` or `no_answer`):
beside a confident answer the chat also shows RELATED questions, and a click on
one of those is a follow-up interest, not the right answer to the first query
(measured: counting them made 92 of 129 "clicks" look like wrong answers).

The output holds QUERY TEXTS of real users: it stays outside the repository
(e.g. $EDU_AI_STATE_DIR/regression/) and only counts are printed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

SQL = """
WITH o AS (
  SELECT id, conversation_id, created_at, raw_query, suggestions_offered
    FROM edu_ai.question_answer_log
   WHERE legal_entity_code = {entity}
     AND stage IN ('suggest', 'no_answer')     -- the offer STANDS IN for an answer
     AND conversation_id IS NOT NULL
     AND jsonb_typeof(suggestions_offered) = 'array' AND jsonb_array_length(suggestions_offered) > 0),
nxt AS (
  SELECT o.*, (SELECT n.raw_query FROM edu_ai.question_answer_log n
                WHERE n.conversation_id = o.conversation_id AND n.created_at > o.created_at
                ORDER BY n.created_at LIMIT 1) AS next_q
    FROM o),
hit AS (
  SELECT n.raw_query AS query, e.ord AS pos, e.v->>'entry_id' AS entry_id
    FROM nxt n, jsonb_array_elements(n.suggestions_offered) WITH ORDINALITY e(v, ord)
   WHERE n.next_q IS NOT NULL AND lower(trim(e.v->>'question')) = lower(trim(n.next_q)))
SELECT coalesce(json_agg(json_build_object('query', h.query, 'pos', h.pos,
                                           'pack', q.pack_id, 'entry_key', q.entry_key)), '[]'::json)
  FROM hit h JOIN edu_ai.qa_entries q ON q.entry_id::text = h.entry_id
 WHERE q.pack_id = ANY({packs});
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="edu_ai_prodcopy")
    ap.add_argument("--db", default="edu_ai")
    ap.add_argument("--user", default="edu_ai_admin")
    ap.add_argument("--entity", required=True, help="the school whose log is read")
    ap.add_argument("--packs", nargs="+", default=["faq-jpz", "faq-provoz"])
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    packs = "ARRAY[" + ",".join("'" + p.replace("'", "''") + "'" for p in a.packs) + "]::text[]"
    sql = SQL.format(entity="'" + a.entity.replace("'", "''") + "'", packs=packs)
    p = subprocess.run(["docker", "exec", "-i", a.container, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                        "-U", a.user, "-d", a.db], input=sql, capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        sys.exit("psql failed: " + p.stderr.strip()[:300])
    rows = json.loads(p.stdout.strip() or "[]")
    cases, seen = [], set()
    for r in rows:
        key = (r["query"].strip().lower(), r["entry_key"])
        if key in seen:
            continue
        seen.add(key)
        cases.append({"set": "C", "pack": r["pack"], "entry_key": r["entry_key"], "query": r["query"].strip(),
                      "category": f"klik_{r['pos']}"})
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(cases, fh, ensure_ascii=False, indent=1)
    per = {}
    for c in cases:
        per[c["category"]] = per.get(c["category"], 0) + 1
    print(f"{len(cases)} kliků (po odstranění duplicit): {dict(sorted(per.items()))} → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
