"""Which words of real queries does the school's vocabulary NOT know? (read-only, private data)

Runs INSIDE the search container:

  python analyze_log_words.py --questions q.json --log log_queries.json --out words.json [--min-queries 3]

Input: the distinct query wordings of the log with how often each was asked
({"query": text, "n": count}). A content word of a query is "unmatched" when no curated
wording of the school contains it, directly or through the synonym groups. The words are
counted per distinct wording and per ask; the nearest school words by the local model
(single-word similarity) are proposed as candidates for NEW synonym groups, which a person
confirms. Only words and counts are printed, never a whole query.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

import numpy as np

os.environ.setdefault("HF_HOME", "/models")
os.environ["HF_HUB_OFFLINE"] = "1"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-queries", type=int, default=3)
    ap.add_argument("--entity", default="SVOBODNA_SKOLA_PRAHA")
    a = ap.parse_args()
    from app.db import get_cursor
    from app.settings import get_active_setting, coverage_stopwords
    from app.nodes.qa_common import _COVERAGE_STOP, load_synonym_groups
    from eduai_normalize import build_forms
    from sentence_transformers import SentenceTransformer

    questions = json.load(open(a.questions, encoding="utf-8"))
    log = json.load(open(a.log, encoding="utf-8"))
    cfg = get_active_setting(a.entity)["config"]
    stop = set(coverage_stopwords().get("cs") or _COVERAGE_STOP) | set(_COVERAGE_STOP)
    stop |= {"kdy", "jak", "kde", "co", "kolik", "kdo", "jaky", "ktery", "proc", "kam", "odkud", "cim",
             "moci", "mit", "byt", "muset", "chtit", "ten", "ahoj", "dobry", "den"}
    with get_cursor() as cur:
        groups, _k, _p, _t = load_synonym_groups(cur, "cs")
    group_of = defaultdict(set)
    for gid, toks in groups.items():
        for t in toks:
            group_of[t].add(gid)

    def lem(text):
        f = build_forms(text, cfg, "cs")
        return [(n, l) for n, l in zip(f["norm"], f["lemma"]) if l not in stop and not l.isdigit() and len(l) > 3]

    vocab = set()
    for q in questions:
        for t in [q["q"], *q["alts"]]:
            vocab |= {l for _n, l in lem(t)}
    expanded = set(vocab)
    for w in list(vocab):
        for g in group_of.get(w, ()):
            expanded |= groups[g]
    total_q = len(log); total_asks = sum(x["n"] for x in log)
    w_queries, w_asks, surface = Counter(), Counter(), {}
    with_unmatched_q = with_unmatched_asks = 0
    for x in log:
        un = {l for _n, l in lem(x["query"]) if l not in expanded}
        for n, l in lem(x["query"]):
            if l in un:
                surface.setdefault(l, n)
        if un:
            with_unmatched_q += 1; with_unmatched_asks += x["n"]
        for l in un:
            w_queries[l] += 1; w_asks[l] += x["n"]
    print(f"dotazů (různých znění): {total_q}, zeptáno celkem {total_asks}×")
    print(f"s aspoň jedním slovem, které školní slovník nezná: {with_unmatched_q} znění ({100*with_unmatched_q/total_q:.0f} %), "
          f"{with_unmatched_asks}× ({100*with_unmatched_asks/total_asks:.0f} % všech dotazů)")
    freq = [w for w, n in w_queries.items() if n >= a.min_queries]
    covered_asks = sum(w_asks[w] for w in freq)
    print(f"různých neznámých slov: {len(w_queries)}; opakují se aspoň v {a.min_queries} zněních: {len(freq)} slov")
    dist = Counter(min(n, 6) for n in w_queries.values())
    print("neznámá slova podle počtu znění, ve kterých se vyskytují (1×, 2×, … 6+×):", [dist.get(i, 0) for i in range(1, 7)])
    # candidates: nearest school words by the model
    school_words = sorted(vocab)
    m = SentenceTransformer("intfloat/multilingual-e5-small", device="cpu")
    SW = m.encode(["query: " + w for w in school_words], batch_size=128, normalize_embeddings=True, show_progress_bar=False)
    top = sorted(freq, key=lambda w: -w_asks[w])[:60]
    QW = m.encode(["query: " + w for w in top], batch_size=128, normalize_embeddings=True, show_progress_bar=False)
    sim = QW @ SW.T
    out = []
    print(f"\n{'neznámé slovo':<16}{'znění':>6}{'zeptáno':>8}  nejbližší školní slova (podobnost)")
    for i, w in enumerate(top):
        near = np.argsort(-sim[i])[:3]
        out.append({"word": w, "queries": w_queries[w], "asks": w_asks[w],
                    "near": [(school_words[j], round(float(sim[i, j]), 3)) for j in near]})
        print(f"{w:<16}{w_queries[w]:>6}{w_asks[w]:>8}  " + ", ".join(f"{school_words[j]} ({sim[i, j]:.2f})" for j in near))
    json.dump({"summary": {"queries": total_q, "asks": total_asks, "unmatched_queries": with_unmatched_q,
                           "unmatched_asks": with_unmatched_asks, "frequent_words": len(freq)}, "words": out},
              open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
