"""Would ranking the OFFER by the rarity of the shared words show the right answer more often? (read-only)

Runs INSIDE the search container:

  python eval_rank.py --questions q.json --cases cases.json --results accepted.json --out rank.json

Today the order of the chips comes from ts_rank_cd first and the IDF-weighted coverage
(fraction of the QUERY's concepts the entry covers) only third; a long query dilutes the
fraction. Here the candidates are ordered by the SUM of the IDF weights of the query
concepts an entry covers (a rare shared word counts, extra words in the query do not
dilute) and only entries above a floor are offered. Merge policies as in eval_semantic.py
(fill: free places only; lex2: first two chips stay; replace). A confident answer is
never touched. Reports the gain per set and how often a chip is added to a question the
school has no answer for (set N). Sets: A, B, N, D:train (D:holdout refused).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict

CONFIDENT = ("exact", "A")
CHIPS = 5
FLOORS = [1.0, 2.0, 3.0, 4.0, 5.0]
VARIANTS = ("fill", "lex2", "replace")


def usable(c):
    return (c["set"] in ("A", "B", "N")) or (c["set"] == "D" and c.get("split") == "train")


def merge(variant, base, ranked, floor):
    cand = [k for k, sc in ranked if sc >= floor]
    base = base[:CHIPS]
    if variant == "fill":
        final = base + [k for k in cand if k not in base][:CHIPS - len(base)]
    elif variant == "lex2":
        head = base[:2]
        final = head + [k for k in cand if k not in head][:3]
        final += [k for k in base if k not in final][:CHIPS - len(final)]
    else:
        final = cand[:CHIPS] or base
    return final[:CHIPS], sum(1 for k in final[:CHIPS] if k not in base)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--cases", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--entity", default="SVOBODNA_SKOLA_PRAHA")
    a = ap.parse_args()
    from app.db import get_cursor
    from app.settings import get_active_setting, coverage_stopwords
    from app.nodes.qa_common import _COVERAGE_STOP, load_synonym_groups
    from eduai_normalize import build_forms

    questions = json.load(open(a.questions, encoding="utf-8"))
    cases = [c for c in json.load(open(a.cases, encoding="utf-8")) if usable(c)]
    res = json.load(open(a.results, encoding="utf-8"))["results"]
    cases = [c for c in cases if c["id"] in res]
    assert not any(c["set"] == "D" and c.get("split") != "train" for c in cases)
    cfg = get_active_setting(a.entity)["config"]
    stop = set(coverage_stopwords().get("cs") or _COVERAGE_STOP) | set(_COVERAGE_STOP)
    stop |= {"kdy", "jak", "kde", "co", "kolik", "kdo", "jaky", "ktery", "proc", "kam", "odkud", "cim",
             "moci", "mit", "byt", "muset", "chtit", "ten"}
    with get_cursor() as cur:
        groups, _k, _p, _t = load_synonym_groups(cur, "cs")
    group_of = defaultdict(set)
    for gid, toks in groups.items():
        for t in toks:
            group_of[t].add(gid)

    def lem(text):
        return [t for t in build_forms(text, cfg, "cs")["lemma"] if t not in stop and not t.isdigit() and len(t) > 2]

    key = lambda q: q.get("entry_key") or f"k{q['idx']}"
    keys = [key(q) for q in questions]
    ent_words = []
    for q in questions:
        ws = set()
        for t in [q["q"], *q["alts"]]:
            ws |= set(lem(t))
        ent_words.append(ws)
    N = len(keys)

    def concept(w):
        s = {w}
        for g in group_of.get(w, ()):
            s |= groups[g]
        return s

    def idf(cs):
        df = sum(1 for ws in ent_words if ws & cs)
        return math.log((N + 1) / (df + 0.5))

    def ranked_for(query):
        cs_list, seen = [], set()
        for w in lem(query):
            if w in seen:
                continue
            seen.add(w)
            cs_list.append(concept(w))
        weights = [idf(cs) for cs in cs_list]
        scored = []
        for j, ws in enumerate(ent_words):
            s = sum(wt for cs, wt in zip(cs_list, weights) if cs & ws)
            scored.append((keys[j], s))
        scored.sort(key=lambda x: -x[1])
        return scored[:10]

    rank_of = {c["id"]: ranked_for(c["query"]) for c in cases}
    right = lambda c: {c["entry_key"], *(c.get("accept") or [])} if c["entry_key"] else set()
    out = {"sets": {}}
    names = sorted({c["set"] if c["set"] != "D" else "D:train" for c in cases})
    for name in names:
        sel = [c for c in cases if (c["set"] if c["set"] != "D" else "D:train") == name]
        if name == "N":
            pol = {v: {} for v in VARIANTS}
            for v in VARIANTS:
                for fl in FLOORS:
                    n = k = 0
                    for c in sel:
                        r = res[c["id"]]
                        if r["decision"] in CONFIDENT:
                            continue
                        n += 1
                        base = [x for x in (r.get("suggestion_keys") or []) if x]
                        k += merge(v, base, rank_of[c["id"]], fl)[1] > 0
                    pol[v][fl] = round(100 * k / max(1, len(sel)), 1)
            out["sets"]["N"] = pol
            continue
        pos = [c for c in sel if c["entry_key"]]
        today, merged = [], {v: {fl: [] for fl in FLOORS} for v in VARIANTS}
        for c in pos:
            r = res[c["id"]]
            if r["decision"] in CONFIDENT:
                ok = r["verdict"] in ("přesná shoda", "odpověď")
                today.append(ok)
                for v in VARIANTS:
                    for fl in FLOORS:
                        merged[v][fl].append(ok)
                continue
            base = [x for x in (r.get("suggestion_keys") or []) if x]
            today.append(bool(right(c) & set(base)))
            for v in VARIANTS:
                for fl in FLOORS:
                    final, _ = merge(v, base, rank_of[c["id"]], fl)
                    merged[v][fl].append(bool(right(c) & set(final)))
        pct = lambda l: round(100 * sum(l) / len(l), 1)
        out["sets"][name] = {"today": pct(today), **{v: {fl: pct(merged[v][fl]) for fl in FLOORS} for v in VARIANTS}}
    json.dump(out, open(a.out, "w", encoding="utf-8"), indent=1)
    print(f"{'práh součtu vah':<30}" + "".join(f"{f:>7.1f}" for f in FLOORS))
    for name in names:
        if name == "N":
            for v in VARIANTS:
                print(f"{'N: tlačítko navíc ('+v+') %':<30}" + "".join(f"{out['sets']['N'][v][f]:>7}" for f in FLOORS))
            continue
        r = out["sets"][name]
        print(f"{name}: dnes uvidí {r['today']} %")
        for v in VARIANTS:
            print(f"{'   '+v:<30}" + "".join(f"{r[v][f]:>7}" for f in FLOORS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
