"""Set L: real queries with a WEAK label from the production log.

  python3 make_log_set.py --container edu_ai_prodcopy --out log_L.json
        [--entity CODE] [--packs faq-jpz faq-provoz]

The production answered these queries through the semantic QA step (OpenAI vectors):
the entry it chose is taken as a weak label. It is not the truth — the cosine of
half of them is borderline — so the set is a test (never tuned on) and each case
carries the cosine (`category` = band) and how often the query was asked.
One row per distinct wording (lower-cased, trimmed); the most frequent entry wins.

The output holds QUERY TEXTS of real users: it stays outside the repository and only
counts are printed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

SQL = """
SELECT coalesce(json_agg(t), '[]'::json) FROM (
  SELECT query, entry_key, pack, n, round(best::numeric, 3) AS best FROM (
    SELECT lower(trim(l.raw_query)) AS query, e.entry_key, e.pack_id AS pack, count(*) AS n,
           max(l.match_score) AS best,
           row_number() OVER (PARTITION BY lower(trim(l.raw_query)) ORDER BY count(*) DESC, max(l.match_score) DESC) AS rk
      FROM edu_ai.question_answer_log l JOIN edu_ai.qa_entries e ON e.entry_id = l.matched_entry_id
     WHERE l.legal_entity_code = {entity} AND l.answer_source = 'qa_vector' AND l.stage = 'answer'
       AND e.pack_id = ANY({packs})
       AND lower(trim(l.raw_query)) <> lower(trim(e.canonical_q))      -- not a click on a chip
     GROUP BY 1, 2, 3) x WHERE rk = 1) t;
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="edu_ai_prodcopy")
    ap.add_argument("--db", default="edu_ai")
    ap.add_argument("--user", default="edu_ai_admin")
    ap.add_argument("--entity", default="ZS_KAVCI_HORY_ZS")
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
    cases = []
    for r in rows:
        band = "silna" if float(r["best"]) >= 0.75 else "hranicni"
        cases.append({"set": "L", "pack": r["pack"], "entry_key": r["entry_key"], "query": r["query"],
                      "category": band, "asked": r["n"], "cosine": float(r["best"])})
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(cases, fh, ensure_ascii=False, indent=1)
    per = {}
    for c in cases:
        per[c["category"]] = per.get(c["category"], 0) + 1
    print(f"{len(cases)} různých znění (zeptáno celkem {sum(c['asked'] for c in cases)}×): {per} → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
