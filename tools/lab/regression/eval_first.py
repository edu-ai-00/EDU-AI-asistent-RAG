"""Success of the FIRST answer: what the asker gets first (a confident answer, else the first chip).

Runs INSIDE the search container (read-only, local model):

  python eval_first.py --questions q.json --cases cases.json --results accepted.json \
        --model intfloat/multilingual-e5-small --prefix "query: " --out first.json

Variants (a confident answer of today's cascade is kept in all of them except `model`):
  today     the cascade as deployed: confident answer, else its first chip
  model     the model's nearest entry alone
  oracle    right if today's first OR the model's nearest is right (the ceiling of ANY way of
            choosing between these two signals, not reachable in practice)
  rerank    among today's chips (the cascade's candidates) the one the model likes best first
  rule@tau  when the cascade has no confident answer and the model's nearest entry reaches
            similarity tau, that entry goes first, else today's first chip
Sets: A, B, L, D:train (D:holdout refused). No chip is added: this is about the ORDER.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

os.environ.setdefault("HF_HOME", "/models")
os.environ["HF_HUB_OFFLINE"] = "1"
CONFIDENT = ("exact", "A")
TAUS = [0.80, 0.84, 0.86, 0.88, 0.90, 0.92]


def usable(c):
    return c["set"] in ("A", "B", "L") or (c["set"] == "D" and c.get("split") == "train")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--cases", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--model", default="intfloat/multilingual-e5-small")
    ap.add_argument("--prefix", default="query: ")
    ap.add_argument("--packs-dir", help="the pack CSVs: with --lam the ANSWER text joins the model's score")
    ap.add_argument("--packs", nargs="+", default=["faq-jpz", "faq-provoz"])
    ap.add_argument("--lam", type=float, default=0.0, help="weight of the answer-text similarity (0 = question only)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    from sentence_transformers import SentenceTransformer
    questions = json.load(open(a.questions, encoding="utf-8"))
    cases = [c for c in json.load(open(a.cases, encoding="utf-8")) if usable(c) and c["entry_key"]]
    assert not any(c["set"] == "D" and c.get("split") != "train" for c in cases)
    res = json.load(open(a.results, encoding="utf-8"))["results"]
    cases = [c for c in cases if c["id"] in res]
    keys = [q.get("entry_key") or f"k{q['idx']}" for q in questions]
    idx = {k: i for i, k in enumerate(keys)}
    wording, owner = [], []
    for i, q in enumerate(questions):
        for t in [q["q"], *q["alts"]]:
            wording.append(a.prefix + t); owner.append(i)
    owner = np.array(owner)
    m = SentenceTransformer(a.model, device="cpu")
    W = m.encode(wording, batch_size=64, normalize_embeddings=True, show_progress_bar=False)
    Q = m.encode([a.prefix + c["query"] for c in cases], batch_size=32, normalize_embeddings=True,
                 show_progress_bar=False)
    sim = Q @ W.T
    ent = np.stack([sim[:, owner == j].max(axis=1) for j in range(len(keys))], axis=1)
    if a.lam and a.packs_dir:
        import csv
        ans = {}
        for pack in a.packs:
            with open(os.path.join(a.packs_dir, pack + ".csv"), encoding="utf-8-sig", newline="") as fh:
                head = fh.readline(); fh.seek(0)
                for r in csv.DictReader(fh, delimiter=";" if head.count(";") >= head.count(",") else ","):
                    ans[r["entry_key"]] = r["answer_md"].replace("\n", " ")
        AP = m.encode(["passage: " + ans.get(k, "") for k in keys], batch_size=64, normalize_embeddings=True,
                      show_progress_bar=False)
        ent = ent + a.lam * (Q @ AP.T)
    right = lambda c: {c["entry_key"], *(c.get("accept") or [])}
    names = sorted({c["set"] if c["set"] != "D" else "D:train" for c in cases})
    out = {}
    rows = {}
    for n, c in enumerate(cases):
        r = res[c["id"]]
        conf = r["decision"] in CONFIDENT
        chips = [k for k in (r.get("suggestion_keys") or []) if k]
        today_first = r.get("found_key") if conf else (chips[0] if chips else None)
        top = int(np.argmax(ent[n])); mkey = keys[top]; msim = float(ent[n, top])
        res_c = {"today": today_first in right(c), "model": mkey in right(c),
                 "oracle": (today_first in right(c)) or (mkey in right(c))}
        if conf:
            res_c["rerank"] = today_first in right(c)
        else:
            cand = [k for k in chips if k in idx]
            res_c["rerank"] = bool(cand) and max(cand, key=lambda k: ent[n, idx[k]]) in right(c)
        for t in TAUS:
            first = today_first if (conf or msim < t) else mkey
            res_c[f"rule@{t}"] = first in right(c)
        rows.setdefault(c["set"] if c["set"] != "D" else "D:train", []).append(res_c)
    cols = ["today", "model", "oracle", "rerank"] + [f"rule@{t}" for t in TAUS]
    print(f"{'první odpověď správně':<24}" + "".join(f"{x:>11}" for x in cols))
    for s in names:
        v = rows[s]
        print(f"{s:<9}({len(v):>4})         " + "".join(f"{100 * np.mean([x[col] for x in v]):>10.1f}%" for col in cols))
        out[s] = {col: round(100 * float(np.mean([x[col] for x in v])), 1) for col in cols}
    json.dump(out, open(a.out, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
