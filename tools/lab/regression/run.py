"""Run the regression cases through the curated-QA cascade. INSIDE the search container.

Like variants/compare_forms.py: the real nodes, the live synonyms and stop-words,
the school's packs as temporary packs, one transaction that is ROLLED BACK.

  python run.py --questions otazky.json --cases cases.json --out results.json
                [--entity E] [--config '{"form":["norm","lemma"],"query_repair":["typo"]}']
                [--with-sql what_if.sql]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "variants"))
import scoring                              # noqa: E402
import compare_forms as cf                  # noqa: E402  (_insert, _ask_list; sets up measure)
import measure                              # noqa: E402

from app import config                      # noqa: E402
from app.settings import get_active_setting  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--cases", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--entity", default="SVOBODNA_SKOLA_PRAHA")
    ap.add_argument("--config", default="{}", help="JSON of qa_lexical params overriding the active setting")
    ap.add_argument("--with-sql", action="append", default=[], metavar="FILE")
    a = ap.parse_args()
    import logging
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)

    questions = json.load(open(a.questions, encoding="utf-8"))
    cases = json.load(open(a.cases, encoding="utf-8"))
    override = json.loads(a.config)
    import psycopg2
    measure._conn = psycopg2.connect(host=config.PG_HOST, port=config.PG_PORT, dbname=config.PG_DATABASE,
                                     user=os.environ.get("LAB_PG_USER", config.PG_USER),
                                     password=os.environ.get("LAB_PG_PASSWORD", config.PG_PASSWORD))
    measure._conn.autocommit = False
    measure._patch_cursors()
    results = {}
    try:
        with measure._conn.cursor() as cur:
            if os.environ.get("LAB_PG_ROLE"):
                cur.execute("SET LOCAL ROLE " + os.environ["LAB_PG_ROLE"])
            # the SQL goes in FIRST: a migration that changes the settings is read
            # back below, so the run measures what the migration really does
            for path in a.with_sql:
                with open(path, encoding="utf-8") as fh:
                    cur.execute(fh.read())
        setting = get_active_setting(a.entity, force=True)   # sees the transaction
        cfg = setting["config"]
        p_exact, _ = measure._step_params(cfg, "qa_exact")
        p_lex, _ = measure._step_params(cfg, "qa_lexical")
        p_lex = {**p_lex, **override}
        form = p_lex.get("form") or ["norm", "lemma", "stem"]
        thresholds = (float(p_lex.get("t_answer", config.QA_T_ANSWER)),
                      float(p_lex.get("t_suggest", config.QA_T_SUGGEST)),
                      float(p_lex.get("m_margin", config.QA_M_MARGIN)))
        with measure._conn.cursor() as cur:
            ids, pack_ids = cf._insert(cur, questions, a.entity, cfg, thresholds)
        key_of = {ids[q["idx"]]: (q.get("entry_key") or f"k{q['idx']}") for q in questions}
        id_of = {(q["pack"], q.get("entry_key") or f"k{q['idx']}"): ids[q["idx"]] for q in questions}
        packs = list(pack_ids.values())
        skipped = 0
        for n, c in enumerate(cases, 1):
            if c["entry_key"] is not None and (c["pack"], c["entry_key"]) not in id_of:
                skipped += 1                     # the curated answer is not in this question set
                continue
            r = cf._ask_list(c["query"], a.entity, cfg, {**p_exact, "form": form},
                             {**p_lex, "form": form}, packs)
            found = key_of.get(r.get("entry"))
            sugg = {key_of.get(s) for s in r.get("suggestions") or []}
            results[c["id"]] = {"verdict": scoring.verdict(c["entry_key"], r, found, sugg),
                                "found_key": found, "decision": r["decision"]}
            if n % 500 == 0:
                print(f"[regression] {n}/{len(cases)}", file=sys.stderr)
    finally:
        measure._conn.rollback()
        measure._conn.close()
        print("[regression] ROLLBACK — v databázi nic nezůstalo", file=sys.stderr)

    meta = {"created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "setting_id": setting["setting_id"], "entity": a.entity, "config_override": override,
            "qa_lexical": {k: v for k, v in p_lex.items() if k in ("form", "t_answer", "t_suggest", "m_margin", "suggest_top_n", "query_repair")},
            "with_sql": [os.path.basename(p) for p in a.with_sql], "skipped": skipped}
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "results": results}, fh, ensure_ascii=False)
    for name, s in scoring.summarize(cases, results).items():
        print(f"{name:<6} {s['cases']:>5} případů  skóre {s['score']:>8}  jistě správně {s['confident_right']:>5}"
              f"  v nabídce {s['offered']:>5}  JISTĚ ŠPATNĚ {s['confident_wrong']:>4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
