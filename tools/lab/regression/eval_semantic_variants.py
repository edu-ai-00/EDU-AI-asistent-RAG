"""Can the local model rank better? Variants of the SCORE, measured on the ranking alone (read-only).

Runs INSIDE the search container:

  python eval_semantic_variants.py --questions q.json --packs-dir DIR --cases cases.json --out v.json

For every case the entries are ranked by a score; hit@1/3/4 says how often the right answer is among the
first 1/3/4 (equivalent answers count). No threshold is involved, so the numbers do not depend on tau.
Variants: q (the question wordings, today), a (the answer text), q+a (question + lambda * answer),
mmr (q, but a chip that is a near duplicate of a chip already shown is skipped).
Sets A, B, L, D:train (D:holdout is refused).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

import numpy as np

os.environ.setdefault("HF_HOME", "/models")
os.environ["HF_HUB_OFFLINE"] = "1"
LAMBDAS = [0.25, 0.5, 1.0]


def usable(c):
    return c["entry_key"] and (c["set"] in ("A", "B", "L") or (c["set"] == "D" and c.get("split") == "train"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--packs-dir", required=True)
    ap.add_argument("--packs", nargs="+", default=["faq-jpz", "faq-provoz"])
    ap.add_argument("--cases", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    from sentence_transformers import SentenceTransformer
    questions = json.load(open(a.questions, encoding="utf-8"))
    cases = [c for c in json.load(open(a.cases, encoding="utf-8")) if usable(c)]
    assert not any(c["set"] == "D" and c.get("split") != "train" for c in cases)
    ans = {}
    for pack in a.packs:
        with open(os.path.join(a.packs_dir, pack + ".csv"), encoding="utf-8-sig", newline="") as fh:
            head = fh.readline(); fh.seek(0)
            for r in csv.DictReader(fh, delimiter=";" if head.count(";") >= head.count(",") else ","):
                ans[r["entry_key"]] = r["answer_md"].replace("\n", " ")
    keys = [q.get("entry_key") or f"k{q['idx']}" for q in questions]
    idx = {k: i for i, k in enumerate(keys)}
    m = SentenceTransformer("intfloat/multilingual-e5-small", device="cpu")
    wording, owner = [], []
    for i, q in enumerate(questions):
        for t in [q["q"], *q["alts"]]:
            wording.append("query: " + t); owner.append(i)
    owner = np.array(owner)
    enc = lambda xs: m.encode(xs, batch_size=64, normalize_embeddings=True, show_progress_bar=False)
    W = enc(wording)
    A_q = enc(["query: " + ans.get(k, "") for k in keys])          # answer as a query-like text
    A_p = enc(["passage: " + ans.get(k, "") for k in keys])        # answer as a passage
    Q = enc(["query: " + c["query"] for c in cases])
    sq = Q @ W.T
    s_q = np.stack([sq[:, owner == j].max(axis=1) for j in range(len(keys))], axis=1)
    s_aq, s_ap = Q @ A_q.T, Q @ A_p.T
    ans_sim = A_q @ A_q.T                                            # near duplicates among the answers
    right = lambda c: {c["entry_key"], *(c.get("accept") or [])}
    scores = {"q (dnes)": s_q, "jen odpověď (query:)": s_aq, "jen odpověď (passage:)": s_ap}
    for lam in LAMBDAS:
        scores[f"q + {lam}·odpověď (query:)"] = s_q + lam * s_aq
        scores[f"q + {lam}·odpověď (passage:)"] = s_q + lam * s_ap

    def hits(order_rows, sel, k):
        return 100 * np.mean([bool(right(cases[n]) & {keys[j] for j in order_rows[n][:k]}) for n in sel])

    def mmr_order(n, base, thr, k=4):
        out = []
        for j in np.argsort(-base[n])[:20]:
            if all(ans_sim[j, i] < thr for i in out):
                out.append(int(j))
            if len(out) == k:
                break
        return out
    sets = {}
    for n, c in enumerate(cases):
        sets.setdefault(c["set"] if c["set"] != "D" else "D:train", []).append(n)
    print(f"{'varianta':<34}" + "".join(f"{s+' @1/@3/@4':>22}" for s in sets))
    res = {}
    for name, S in scores.items():
        order = np.argsort(-S, axis=1)
        row = {s: [round(hits(order, sel, k), 1) for k in (1, 3, 4)] for s, sel in sets.items()}
        res[name] = row
        print(f"{name:<34}" + "".join(f"{'/'.join(str(x) for x in row[s]):>22}" for s in sets))
    for thr in (0.97, 0.95, 0.93):
        row = {}
        for s, sel in sets.items():
            ords = {n: mmr_order(n, s_q, thr) for n in sel}
            row[s] = [round(hits(ords, sel, k), 1) for k in (1, 3, 4)]
        res[f"q + bez duplicit (odpovědi ≥ {thr})"] = row
        print(f"{'q + bez duplicit odp. ≥ ' + str(thr):<34}" + "".join(f"{'/'.join(str(x) for x in row[s]):>22}" for s in sets))
    json.dump(res, open(a.out, "w"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
