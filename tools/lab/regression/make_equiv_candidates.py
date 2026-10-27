"""Candidates for EQUIVALENT answers: pairs of curated entries that say nearly the same.

Runs INSIDE the search container (needs the local model in /models):

  python make_equiv_candidates.py --questions q.json --packs-dir DIR --out cand.xlsx [--top 40]

For two entries the regression set can only name ONE right answer, so a search
that returns the other one is counted as a confident wrong answer even though a
person would accept it. The pairs are proposed from the similarity of the
questions (all wordings) and of the answers; a person only confirms yes/no in the
column `shodne` ("ano" = equally right). build_cases.py --equiv-csv reads the
confirmed ones. Nothing is written or decided automatically.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xlsx_write

os.environ.setdefault("HF_HOME", "/models")
os.environ["HF_HUB_OFFLINE"] = "1"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--packs-dir", required=True)
    ap.add_argument("--packs", nargs="+", default=["faq-jpz", "faq-provoz"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--top", type=int, default=40)
    a = ap.parse_args()
    from sentence_transformers import SentenceTransformer
    questions = json.load(open(a.questions, encoding="utf-8"))
    ans = {}
    for pack in a.packs:
        with open(os.path.join(a.packs_dir, pack + ".csv"), encoding="utf-8-sig", newline="") as fh:
            head = fh.readline()
            fh.seek(0)
            for r in csv.DictReader(fh, delimiter=";" if head.count(";") >= head.count(",") else ","):
                ans[r["entry_key"]] = r["answer_md"].replace("\n", " ")
    ents = [q for q in questions if (q.get("entry_key") or f"k{q['idx']}") in ans]
    key = lambda q: q.get("entry_key") or f"k{q['idx']}"
    m = SentenceTransformer("intfloat/multilingual-e5-small", device="cpu")
    wording, owner = [], []
    for i, q in enumerate(ents):
        for t in [q["q"], *q["alts"]]:
            wording.append("query: " + t); owner.append(i)
    owner = np.array(owner)
    W = m.encode(wording, batch_size=64, normalize_embeddings=True, show_progress_bar=False)
    A = m.encode(["query: " + ans[key(q)] for q in ents], batch_size=64, normalize_embeddings=True,
                 show_progress_bar=False)
    n = len(ents)
    sq = np.zeros((n, n))
    wsim = W @ W.T
    for i in range(n):
        for j in range(i + 1, n):
            sq[i, j] = wsim[np.ix_(owner == i, owner == j)].max()
    sa = A @ A.T
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            pairs.append((max(sq[i, j], sa[i, j]), sq[i, j], sa[i, j], i, j))
    pairs.sort(reverse=True)
    rows = [["klic_A", "klic_B", "otazka_A", "odpoved_A", "otazka_B", "odpoved_B",
             "podobnost_otazek", "podobnost_odpovedi", "shodne (ano/ne)"]]
    for score, s_q, s_a, i, j in pairs[:a.top]:
        rows.append([key(ents[i]), key(ents[j]), ents[i]["q"], ans[key(ents[i])], ents[j]["q"],
                     ans[key(ents[j])], round(float(s_q), 3), round(float(s_a), 3), ""])
    xlsx_write.write_xlsx(a.out, rows, widths=[12, 12, 38, 60, 38, 60, 11, 11, 14], input_cols={8},
                          sheet="Shodné odpovědi")
    print(f"{n} záznamů, {len(pairs)} dvojic; nejlepších {a.top} → {a.out}", file=sys.stderr)
    print("podobnost nejlepší / 10. / 20. / 40.: " +
          " / ".join(f"{pairs[k][0]:.3f}" for k in (0, 9, 19, min(39, len(pairs) - 1))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
