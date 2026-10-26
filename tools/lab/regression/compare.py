"""Compare a run with a saved baseline (host, stdlib only).

  python3 compare.py --cases cases.json --baseline base.json --now now.json [--out diff.csv]

Prints the score per set and the cases that got worse (new confident wrong
answers first). Exit code 1 when a case turned into a confident wrong answer,
so a change can be accepted or rejected by the numbers.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scoring                              # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--now", required=True)
    ap.add_argument("--out", help="CSV of the changed cases (contains the query texts: keep it private)")
    ap.add_argument("--show", type=int, default=10, help="how many regressions to list (default 10)")
    a = ap.parse_args()
    cases = json.load(open(a.cases, encoding="utf-8"))
    base = json.load(open(a.baseline, encoding="utf-8"))
    now = json.load(open(a.now, encoding="utf-8"))
    by_id = {c["id"]: c for c in cases}

    sb, sn = scoring.summarize(cases, base["results"]), scoring.summarize(cases, now["results"])
    print(f"{'sada':<6}{'případů':>8}{'skóre před':>12}{'skóre teď':>11}{'jistě špatně před→teď':>25}")
    for name in sn:
        b = sb.get(name, {"score": 0, "confident_wrong": 0})
        n = sn[name]
        print(f"{name:<6}{n['cases']:>8}{b['score']:>12}{n['score']:>11}{b['confident_wrong']:>14} →{n['confident_wrong']:>4}")
    d = scoring.compare(cases, base["results"], now["results"])
    print(f"\nzhoršeno {len(d['regressions'])}, zlepšeno {len(d['improvements'])}, "
          f"NOVÉ jisté špatné {len(d['new_confident_wrong'])}"
          + (f", jen teď {len(d['only_now'])}, jen dřív {len(d['only_before'])}" if d["only_now"] or d["only_before"] else ""))
    for r in d["regressions"][:a.show]:
        c = by_id[r["id"]]
        print(f"  {r['set']} [{c['category']}] {r['before']} → {r['now']}   {c['query']!r}")
    if a.out:
        with open(a.out, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["zmena", "sada", "kategorie", "dotaz", "pred", "ted", "nalezeno_pred", "nalezeno_ted"])
            for kind, rows in (("zhorseno", d["regressions"]), ("zlepseno", d["improvements"])):
                for r in rows:
                    c = by_id[r["id"]]
                    w.writerow([kind, r["set"], c["category"], c["query"], r["before"], r["now"],
                                r["found_before"] or "", r["found_now"] or ""])
        print(f"\nuloženo: {a.out}")
    return 1 if d["new_confident_wrong"] else 0


if __name__ == "__main__":
    sys.exit(main())
