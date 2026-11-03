"""Experiment: do more alternatives per entry make the right answer easier to find?

  python3 make_alt_experiment.py --idx otazky_idx.json --cases cases.json --out-dir DIR
                                 [--pool 5] [--k 2 5]

The written paraphrases of set A (7 per entry) stand in for the alternatives a curator or a
generator would write. Per entry the first `--pool` of them (by case id, deterministic) form
the POOL; for every k the first k of the pool are appended to the entry's alternatives.

The test set is the same for every k and does not contain the pool: the other paraphrases of
A (the near-twins of the pool, so their gain is inflated), and the independent sets B, D, L, N, C.
`k = 0` is the baseline: the entries as they are, scored on the same cases.

`--select f1|f2` (inside the search image, it needs the lemmatizer) takes from the pool only the
paraphrases the linter (alt_lint.py) would keep, up to `--pool`, and writes DIR/idx_<mode>.json:
  f1  adds at least one lemma the entry does not hold yet, and as a query it is covered by no
      OTHER entry at >= `--t-collision` (0.9, IDF-weighted)
  f2  as f1, but the new lemma must be SPECIFIC: held by at most `--df-max` entries (default 3)

Writes DIR/idx_k<k>.json, DIR/cases_test.json and prints how many alternatives were added.
The files hold question texts: keep them outside the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def select_pool(idx: list[dict], cases: list[dict], pool: int, lemmatize, mode: str,
                t_collision: float = 0.9, df_max: int = 3) -> tuple[list[dict], dict]:
    """The idx with, per entry, the pool paraphrases the linter would keep (see the module doc)."""
    import alt_lint
    _o, _t, _i = build(idx, cases, pool, [pool])           # the pool per entry = what k = pool adds
    pools = {}
    for e_old, e_new in zip(idx, _o[pool]):
        pools[(e_old["pack"], e_old["entry_key"])] = e_new["alts"][len(e_old["alts"]):]
    unions = []
    for e in idx:
        u = set(lemmatize(e["q"]))
        for a in e["alts"]:
            u |= set(lemmatize(a))
        unions.append(u)
    w = alt_lint.idf_weights(unions)
    df = {}
    for u in unions:
        for lem in u:
            df[lem] = df.get(lem, 0) + 1
    out, kept, offered = [], 0, 0
    for i, e in enumerate(idx):
        union = set(unions[i])
        alts = list(e["alts"])
        for text in pools[(e["pack"], e["entry_key"])]:
            offered += 1
            lem = set(lemmatize(text))
            new = lem - union
            if mode == "f2":
                new = {x for x in new if df.get(x, 0) <= df_max}
            if not new:
                continue
            if any(alt_lint.weighted_cover(lem, unions[j], w) >= t_collision
                   for j in range(len(idx)) if j != i):
                continue
            alts.append(text)
            union |= lem
            kept += 1
        out.append({**e, "alts": alts})
    return out, {"offered": offered, "kept": kept}


def build(idx: list[dict], cases: list[dict], pool: int, ks: list[int]):
    """-> ({k: new_idx}, test_cases, info). `idx`: [{pack, entry_key, q, alts, idx}]."""
    by_entry: dict[tuple, list[dict]] = {}
    for c in cases:
        if c["set"] == "A" and c.get("entry_key") is not None:
            by_entry.setdefault((c["pack"], c["entry_key"]), []).append(c)
    pools = {}
    for key, cs in by_entry.items():
        cs = sorted(cs, key=lambda c: c["id"])
        pools[key] = cs[:pool] if len(cs) > pool else cs[:max(len(cs) - 1, 0)]   # always leave one to test on
    pool_ids = {c["id"] for cs in pools.values() for c in cs}
    out = {}
    added = {}
    for k in ks:
        new = []
        n_added = 0
        for e in idx:
            alts = list(e["alts"])
            seen = {_norm(e["q"]), *(_norm(a) for a in alts)}
            for c in pools.get((e["pack"], e["entry_key"]), [])[:k]:
                if _norm(c["query"]) not in seen:
                    alts.append(c["query"])
                    seen.add(_norm(c["query"]))
                    n_added += 1
            new.append({**e, "alts": alts})
        out[k] = new
        added[k] = n_added
    test = [c for c in cases if c["id"] not in pool_ids]
    info = {"pool_cases": len(pool_ids), "test_cases": len(test), "entries": len(idx),
            "added": added, "A_left": sum(1 for c in test if c["set"] == "A")}
    return out, test, info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--idx", required=True)
    ap.add_argument("--cases", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--pool", type=int, default=5)
    ap.add_argument("--k", type=int, nargs="+", default=[2, 5])
    ap.add_argument("--select", nargs="+", choices=["f1", "f2"], default=[],
                    help="also write the linter-selected variants (needs eduai_normalize: run in the search image)")
    ap.add_argument("--stoplist", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "contracts", "parity", "stoplist.json"))
    ap.add_argument("--t-collision", type=float, default=0.9)
    ap.add_argument("--df-max", type=int, default=3)
    a = ap.parse_args()
    idx = json.load(open(a.idx, encoding="utf-8"))
    cases = json.load(open(a.cases, encoding="utf-8"))
    ks = sorted(set(a.k) | {0})
    new, test, info = build(idx, cases, a.pool, ks)
    os.makedirs(a.out_dir, exist_ok=True)
    for k, e in new.items():
        json.dump(e, open(os.path.join(a.out_dir, f"idx_k{k}.json"), "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(test, open(os.path.join(a.out_dir, "cases_test.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print(f"pool {info['pool_cases']} přepisů z A, test {info['test_cases']} případů "
          f"(z toho zbylé A {info['A_left']}), přidáno alternativ: {info['added']}")
    if a.select:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "neutral_answers"))
        import machine
        machine.configure(a.stoplist)
        if not machine.available():
            print("eduai_normalize není dostupné: pouštěj v obrazu vyhledávání", file=sys.stderr)
            return 2
        lem = lambda t: set((machine.analyse(t) or {}).get("lemma") or [])          # noqa: E731
        for mode in a.select:
            sel, st = select_pool(idx, cases, a.pool, lem, mode, a.t_collision, a.df_max)
            json.dump(sel, open(os.path.join(a.out_dir, f"idx_{mode}.json"), "w", encoding="utf-8"), ensure_ascii=False)
            print(f"{mode}: z {st['offered']} přepisů ponecháno {st['kept']} ({100 * st['kept'] / max(st['offered'], 1):.0f} %)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
