"""Phase 2: sort the failures of a run into causes, with counts and examples.

Runs INSIDE the search container (read-only: the settings, the synonyms, the local model):

  python analyze_errors.py --questions q.json --cases cases.json --results accepted.json --out table.json
        [--sets A B D:train] [--examples 3]

Cases the asker does not see the right answer for ("nic (C)", "nabídka bez správné")
and confident wrong answers are put into ONE bucket each, in this order:

  překlep/neznámé slovo       a query word the lemmatizer and the question vocabulary do not know
  žádné tematické společné slovo   no THEMATIC word (also through synonyms) shared with the right entry
  jedno tematické společné slovo   exactly one
  2+ tematická společná slova, přesto nic   rules, weights or thresholds hold the answer back (suspicious)

A thematic word is not a generic one: a lemma in more than 5 % of the entries ("škola",
"žák", "dítě") says nothing about the topic and is not counted as a shared word.
  záměna: téměř shodná otázka     confident wrong, found entry nearly the same as the right one
  záměna: jiné téma               confident wrong, other topic (the shared word is often a generic one)

and for each bucket: how often the local model (multilingual-e5-small) has the right
answer in its top 5 = the share a semantic offer could still show.
D:holdout is never analysed (frozen, see README): pass --sets without it.
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

BUCKETS = ["překlep/neznámé slovo", "žádné tematické společné slovo", "jedno tematické společné slovo",
           "2+ tematická společná slova, přesto nic", "záměna: téměř shodná otázka", "záměna: jiné téma"]
NOT_SEEN = ("nic (C)", "nabídka bez správné")
GENERIC_DF_SHARE = 0.05     # a lemma in more than 5 % of the entries is a generic word


def in_sets(c: dict, sets: list[str]) -> bool:
    return c["set"] in sets or f"{c['set']}:{c.get('split')}" in sets


def classify(verdict, shared, unknown, near_duplicate):
    if verdict == "ŠPATNÁ odpověď":
        return "záměna: téměř shodná otázka" if near_duplicate else "záměna: jiné téma"
    if unknown and shared == 0:
        return "překlep/neznámé slovo"
    if shared == 0:
        return "žádné tematické společné slovo"
    if shared == 1:
        return "jedno tematické společné slovo"
    return "2+ tematická společná slova, přesto nic"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--cases", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sets", nargs="+", default=["A", "B", "D:train"])
    ap.add_argument("--examples", type=int, default=3)
    ap.add_argument("--entity", default="SVOBODNA_SKOLA_PRAHA")
    a = ap.parse_args()
    if any(s in ("D", "D:holdout") for s in a.sets):
        sys.exit("D and D:holdout are frozen — analyse D:train only")
    from app.db import get_cursor
    from app.settings import get_active_setting, coverage_stopwords
    from app.nodes.qa_common import _COVERAGE_STOP, load_synonym_groups
    from app.nodes import query_repair as qr
    from eduai_normalize import build_forms
    from sentence_transformers import SentenceTransformer

    questions = json.load(open(a.questions, encoding="utf-8"))
    cases = json.load(open(a.cases, encoding="utf-8"))
    res = json.load(open(a.results, encoding="utf-8"))["results"]
    cfg = get_active_setting(a.entity)["config"]
    stop = set(coverage_stopwords().get("cs") or _COVERAGE_STOP) | set(_COVERAGE_STOP)
    stop |= {"kdy", "jak", "kde", "co", "kolik", "kdo", "jaky", "ktery", "proc", "kam", "odkud", "cim",
             "moci", "mit", "byt", "muset", "chtit", "ten"}
    with get_cursor() as cur:
        groups, _kind, _phr, _terms = load_synonym_groups(cur, "cs")
    group_of = defaultdict(set)
    for gid, toks in groups.items():
        for t in toks:
            group_of[t].add(gid)

    def lem(text):
        f = build_forms(text, cfg, "cs")
        return [t for t in f["lemma"] if t not in stop and not t.isdigit() and len(t) > 2], f["norm"], f["lemma"]

    key = lambda q: (q["pack"], q.get("entry_key") or f"k{q['idx']}")
    ent = {}
    for q in questions:
        words = set()
        for t in [q["q"], *q["alts"]]:
            words |= set(lem(t)[0])
        ent[key(q)] = {"q": q["q"], "words": words, "alts": [q["q"], *q["alts"]]}
    byk = {k[1]: k for k in ent}
    df = Counter(w for e in ent.values() for w in e["words"])
    vocab = set(df) | {w for e in ent.values() for t in e["alts"] for w in build_forms(t, cfg, "cs")["norm"]}
    generic = {w for w, n in df.items() if n / len(ent) >= GENERIC_DF_SHARE}

    def expand(ws):
        out = set(ws)
        for w in ws:
            for g in group_of.get(w, ()):
                out |= groups[g]
        return out

    todo = []
    for c in cases:
        if not in_sets(c, a.sets) or c["id"] not in res or not c["entry_key"]:
            continue
        v = res[c["id"]]["verdict"]
        if v in NOT_SEEN or v == "ŠPATNÁ odpověď":
            todo.append(c)
    print(f"{len(todo)} případů k rozboru v sadách {a.sets}", file=sys.stderr)

    m = SentenceTransformer("intfloat/multilingual-e5-small", device="cpu")
    keys = list(ent)
    wording, owner = [], []
    for i, k in enumerate(keys):
        for t in ent[k]["alts"]:
            wording.append("query: " + t); owner.append(i)
    owner = np.array(owner)
    W = m.encode(wording, batch_size=64, normalize_embeddings=True, show_progress_bar=False)
    Qv = m.encode(["query: " + c["query"] for c in todo], batch_size=64, normalize_embeddings=True,
                  show_progress_bar=False)
    sim = Qv @ W.T
    ent_sim = np.stack([sim[:, owner == j].max(axis=1) for j in range(len(keys))], axis=1)
    idx_of = {k: i for i, k in enumerate(keys)}
    wsim = W @ W.T

    rows, table = [], defaultdict(lambda: defaultdict(Counter))
    examples = defaultdict(list)
    for n, c in enumerate(todo):
        v = res[c["id"]]["verdict"]
        right = (c["pack"], c["entry_key"])
        qlem, qnorm, qlemfull = lem(c["query"])
        shared = len({w for w in qlem if (expand([w]) & ent[right]["words"] - generic)})
        unknown = any(nw not in vocab and lw == nw and not qr._known(nw) and len(nw) >= 4 and not nw.isdigit()
                      for nw, lw in zip(qnorm, qlemfull))
        near = False
        found = res[c["id"]].get("found_key")
        if v == "ŠPATNÁ odpověď" and found in byk:
            fi, ri = idx_of[byk[found]], idx_of[right]
            near = float(wsim[np.ix_(owner == fi, owner == ri)].max()) >= 0.92
        bucket = classify(v, shared, unknown, near)
        top5 = set(np.argsort(-ent_sim[n])[:5].tolist())
        model5 = idx_of[right] in top5
        rows.append({"set": f"{c['set']}:{c.get('split')}" if c["set"] == "D" else c["set"],
                     "bucket": bucket, "verdict": v, "model_top5": model5, "query": c["query"]})
        sname = c["set"] if c["set"] != "D" else "D:train"
        table[bucket][sname]["n"] += 1
        table[bucket][sname]["model5"] += int(model5)
        if len(examples[bucket]) < a.examples * 3 and (len(examples[bucket]) % 3 == 0 or c["set"] != "A"):
            examples[bucket].append({"query": c["query"], "right": ent[right]["q"], "verdict": v})

    sets = sorted({r["set"] for r in rows})
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump({"rows": rows, "examples": examples}, fh, ensure_ascii=False, indent=1)
    print(f"{'skupina':<34}" + "".join(f"{s:>16}" for s in sets) + f"{'celkem':>9}{'model v top 5':>15}")
    for b in BUCKETS:
        cnt = {s: sum(1 for r in rows if r["bucket"] == b and r["set"] == s) for s in sets}
        tot = sum(cnt.values())
        if not tot:
            continue
        hit = sum(1 for r in rows if r["bucket"] == b and r["model_top5"])
        print(f"{b:<34}" + "".join(f"{cnt[s]:>16}" for s in sets) + f"{tot:>9}{100 * hit / tot:>13.0f} %")
    print()
    for b in BUCKETS:
        for e in examples.get(b, [])[:a.examples]:
            print(f"[{b}] {e['query']!r}  → správně: {e['right']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
