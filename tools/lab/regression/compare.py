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
    ap.add_argument("--margin-drop", type=float, default=0.03,
                    help="a confident answer that stays right but loses at least this much lead "
                         "over the second entry is listed as a silent erosion (default 0.03)")
    ap.add_argument("--by-split", action="store_true",
                    help="also the train / holdout halves of every set (hold-out = never tuned on)")
    a = ap.parse_args()
    cases = json.load(open(a.cases, encoding="utf-8"))
    base = json.load(open(a.baseline, encoding="utf-8"))
    now = json.load(open(a.now, encoding="utf-8"))
    by_id = {c["id"]: c for c in cases}

    sb = scoring.summarize(cases, base["results"], a.by_split)
    sn = scoring.summarize(cases, now["results"], a.by_split)
    print(f"{'sada':<14}{'případů':>8}{'skóre před':>12}{'skóre teď':>11}{'jistě špatně':>14}   změna skóre (95% interval)")
    for name in sn:
        b = sb.get(name, {"score": 0, "confident_wrong": 0})
        n = sn[name]
        if ":" in name:
            sel = [c for c in cases if f"{c['set']}:{c.get('split')}" == name]
        else:
            sel = cases if name == "ALL" else [c for c in cases if c["set"] == name]
        bs = scoring.bootstrap_delta(sel, base["results"], now["results"])
        verdict = "význam" if bs["significant"] else "šum"
        print(f"{name:<14}{n['cases']:>8}{b['score']:>12}{n['score']:>11}{b['confident_wrong']:>8} →{n['confident_wrong']:>4}"
              f"   {bs['delta']:+} ({bs['lo']:+} … {bs['hi']:+}) {verdict}")
    d = scoring.compare(cases, base["results"], now["results"])
    print(f"\nzhoršeno {len(d['regressions'])}, zlepšeno {len(d['improvements'])}, "
          f"NOVÉ jisté špatné {len(d['new_confident_wrong'])}"
          + (f", jen teď {len(d['only_now'])}, jen dřív {len(d['only_before'])}" if d["only_now"] or d["only_before"] else ""))
    sm = scoring.silent_margin_drops(cases, base["results"], now["results"], a.margin_drop)
    if sm["compared"]:
        print(f"tichá eroze náskoku (pokles ≥ {a.margin_drop}): {len(sm['drops'])} z {sm['compared']} "
              f"jistých správných odpovědí s náskokem v obou během")
        for r in sm["drops"][:a.show]:
            c = by_id[r["id"]]
            print(f"  {r['set']} [{c['category']}] náskok {r['margin_before']} → {r['margin_now']}   {c['query']!r}")
    else:
        print("tichá eroze náskoku: nelze určit (základ nebo běh nemá `margin`; spusť základ znovu)")
    og = scoring.offer_growth(cases, base["results"], now["results"])
    if og["cases"]:
        print(f"otázky mimo obsah (N): nabídka přibyla u {len(og['gained'])}, ubyla u {len(og['lost'])} z {og['cases']}")
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
