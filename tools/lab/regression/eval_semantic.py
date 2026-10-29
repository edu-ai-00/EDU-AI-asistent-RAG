"""Phase 3: would a local embedding model add the right answer to the offer? (read-only)

Runs INSIDE the search container:

  python eval_semantic.py --questions q.json --cases cases.json --results accepted.json \
        --model intfloat/multilingual-e5-small --prefix "query: " --out sem.json

For every case without a confident answer the offer is today's chips (the order the
cascade returned them, up to 5). The model FILLS the free places with its nearest
entries whose similarity reaches tau; a confident answer is never touched. Reports,
per set and tau: how often the asker sees the right answer (confident, or among the
chips) against today, and on the negatives N how often the model adds a chip to a
question the school has no answer for (pollution).

Sets used: A, B, N, L (weak labels from the log) and D:train. D:holdout (the frozen half) is refused.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time

import numpy as np

os.environ.setdefault("HF_HOME", "/models")
os.environ["HF_HUB_OFFLINE"] = "1"
CONFIDENT = ("exact", "A")
TAUS = [0.78, 0.80, 0.82, 0.84, 0.86, 0.88, 0.90, 0.92]
CHIPS = 5
VARIANTS = ("fill", "lex1", "lex2", "rrf")


def merge(variant, base, model_ranked, tau):
    """base: today's chips (keys, ordered); model_ranked: [(key, sim)] best first.
    Returns the final chips (≤ CHIPS) and how many of them come from the model only."""
    mod = [k for k, sv in model_ranked if sv >= tau]
    base = base[:CHIPS]
    if variant == "fill":
        final = base + [k for k in mod if k not in base][:CHIPS - len(base)]
    elif variant == "lex1":          # the first slovní chip stays, the model's best chips follow
        head = base[:1]
        final = head + [k for k in mod if k not in head][:CHIPS - 1]
        final += [k for k in base if k not in final][:CHIPS - len(final)]
        final += [k for k in mod if k not in final][:CHIPS - len(final)]
    elif variant == "lex2":          # the model's best chips go right after the first two slovní chips
        head = base[:2]
        mid = [k for k in mod if k not in head][:3]
        final = head + mid
        final += [k for k in base if k not in final][:CHIPS - len(final)]
        final += [k for k in mod if k not in final][:CHIPS - len(final)]
    else:                            # rrf: reciprocal-rank fusion of both lists
        score = {}
        for r, k in enumerate(base):
            score[k] = score.get(k, 0) + 1.0 / (2 + r)
        for r, k in enumerate(mod[:10]):
            score[k] = score.get(k, 0) + 1.0 / (2 + r)
        final = sorted(score, key=lambda k: -score[k])[:CHIPS]
    final = final[:CHIPS]
    return final, sum(1 for k in final if k not in base)


def usable(c: dict) -> bool:
    if c["set"] == "D":
        return c.get("split") == "train"           # D:holdout is frozen
    return c["set"] in ("A", "B", "N", "L")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--cases", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--model", default="intfloat/multilingual-e5-small")
    ap.add_argument("--prefix", default="query: ")
    ap.add_argument("--taus", type=float, nargs="+", help="the similarity thresholds to try (scales differ per model)")
    ap.add_argument("--chips", type=int, default=5, help="how many chips the offer may hold (default 5)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    global TAUS, CHIPS
    if a.taus:
        TAUS = a.taus
    CHIPS = a.chips
    from sentence_transformers import SentenceTransformer

    questions = json.load(open(a.questions, encoding="utf-8"))
    cases = [c for c in json.load(open(a.cases, encoding="utf-8")) if usable(c)]
    assert not any(c["set"] == "D" and c.get("split") != "train" for c in cases), "D:holdout must stay frozen"
    res = json.load(open(a.results, encoding="utf-8"))["results"]
    cases = [c for c in cases if c["id"] in res]

    key = lambda q: q.get("entry_key") or f"k{q['idx']}"
    keys = [key(q) for q in questions]
    idx = {k: i for i, k in enumerate(keys)}
    wording, owner = [], []
    for i, q in enumerate(questions):
        for t in [q["q"], *q["alts"]]:
            wording.append(a.prefix + t); owner.append(i)
    owner = np.array(owner)

    t0 = time.time()
    m = SentenceTransformer(a.model, device="cpu")
    load_s = time.time() - t0
    W = m.encode(wording, batch_size=64, normalize_embeddings=True, show_progress_bar=False)
    t0 = time.time()
    Q = m.encode([a.prefix + c["query"] for c in cases], batch_size=32, normalize_embeddings=True,
                 show_progress_bar=False)
    per_q_ms = 1000 * (time.time() - t0) / len(cases)
    sim = Q @ W.T
    ent_sim = np.stack([sim[:, owner == j].max(axis=1) for j in range(len(keys))], axis=1)
    order = np.argsort(-ent_sim, axis=1)

    def right_set(c):
        return {c["entry_key"], *(c.get("accept") or [])} if c["entry_key"] else set()

    out = {"model": a.model, "load_s": round(load_s, 1), "ms_per_query": round(per_q_ms, 1),
           "rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024), "sets": {}}
    names = sorted({c["set"] if c["set"] != "D" else "D:train" for c in cases})
    for name in names:
        sel = [n for n, c in enumerate(cases) if (c["set"] if c["set"] != "D" else "D:train") == name]
        if name == "N":
            def pollution(rows, v, tau):
                cnt = 0
                for n in rows:
                    r = res[cases[n]["id"]]
                    if r["decision"] in CONFIDENT:
                        continue
                    base = [k for k in (r.get("suggestion_keys") or []) if k]
                    ranked = [(keys[j], float(ent_sim[n, j])) for j in order[n][:10]]
                    cnt += merge(v, base, ranked, tau)[1] > 0
                return round(100 * cnt / max(1, len(rows)), 1)
            halves = {h: [n for n in sel if cases[n].get("split") == h] for h in ("train", "holdout")}
            out["sets"]["N"] = {
                "cases": len(sel),
                "model_chip_among_final_%": {v: {tau: pollution(sel, v, tau) for tau in TAUS} for v in VARIANTS},
                "by_half": {h: {v: {tau: pollution(rows, v, tau) for tau in TAUS} for v in VARIANTS}
                            for h, rows in halves.items()}}
            continue
        pos = [n for n in sel if cases[n]["entry_key"]]
        top = lambda k: round(100 * np.mean([bool(right_set(cases[n]) & {keys[j] for j in order[n][:k]}) for n in pos]), 1)
        row = {"cases": len(pos), "model_alone_top1_%": top(1), "model_alone_top3_%": top(3), "model_alone_top5_%": top(5)}
        today = []
        for n in pos:
            r = res[cases[n]["id"]]
            if r["decision"] in CONFIDENT:
                today.append(r["verdict"] in ("přesná shoda", "odpověď"))
            else:
                today.append(bool(right_set(cases[n]) & {k for k in (r.get("suggestion_keys") or []) if k}))
        row["today_visible_%"] = round(100 * np.mean(today), 1)
        merged = {v: {} for v in VARIANTS}
        for v in VARIANTS:
            for tau in TAUS:
                ok = []
                for n, t in zip(pos, today):
                    r = res[cases[n]["id"]]
                    if r["decision"] in CONFIDENT:
                        ok.append(t); continue
                    base = [k for k in (r.get("suggestion_keys") or []) if k]
                    ranked = [(keys[j], float(ent_sim[n, j])) for j in order[n][:10]]
                    final, _ = merge(v, base, ranked, tau)
                    ok.append(bool(right_set(cases[n]) & set(final)))
                merged[v][tau] = round(100 * np.mean(ok), 1)
        row["merged_visible_%_by_variant_tau"] = merged
        out["sets"][name] = row
    json.dump(out, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"model {a.model}: načtení {out['load_s']} s, {out['ms_per_query']} ms na dotaz, špička paměti {out['rss_mb']} MB")
    print(f"{'práh τ':<26}" + "".join(f"{t:>7.2f}" for t in TAUS))
    for name in names:
        if name == "N":
            for v in VARIANTS:
                print(f"{'N: model-chip ('+v+') %':<26}" + "".join(f"{out['sets']['N']['model_chip_among_final_%'][v][t]:>7}" for t in TAUS))
            for h in ("train", "holdout"):
                print(f"{'   N:'+h+' (lex1) %':<26}" + "".join(f"{out['sets']['N']['by_half'][h]['lex1'][t]:>7}" for t in TAUS))
            continue
        r = out["sets"][name]
        print(f"{name}: dnes uvidí {r['today_visible_%']} % | model sám top1/3/5: {r['model_alone_top1_%']}/{r['model_alone_top3_%']}/{r['model_alone_top5_%']} %")
        for v in VARIANTS:
            print(f"{'   '+v:<26}" + "".join(f"{r['merged_visible_%_by_variant_tau'][v][t]:>7}" for t in TAUS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
