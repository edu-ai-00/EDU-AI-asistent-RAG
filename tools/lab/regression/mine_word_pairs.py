"""Phase 4/5: mine single-word synonym candidates (colloquial / unknown word -> school word).

Runs INSIDE the search container (read-only):

  python mine_word_pairs.py --questions q.json --cases cases.json --results accepted.json \
        --out-csv pairs.csv --out-sql whatif.sql [--min-support 3] [--min-share 0.5]

Mined ONLY from cases of sets A and B whose curated entry is in the `train` split.
A query word that no curated wording of the school contains (and that is not a stop
word) is paired with the thematic word of the right entry that the query does not
cover; a pair is kept when the same word recurs with the same partner often enough
(`--min-support` cases, `--min-share` of the cases of that word). The SQL is a what-if
(rows of one synonym group per pair, source 'lab-mined'), measured with
run.sh --with-sql on the hold-out cases and D:train: a pair that only fits the
training entries shows up as no gain there.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict

GENERIC_DF_SHARE = 0.05


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--cases", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--out-sql", required=True)
    ap.add_argument("--min-support", type=int, default=3)
    ap.add_argument("--min-share", type=float, default=0.5)
    ap.add_argument("--entity", default="SVOBODNA_SKOLA_PRAHA")
    a = ap.parse_args()
    from app.db import get_cursor
    from app.settings import get_active_setting, coverage_stopwords
    from app.nodes.qa_common import _COVERAGE_STOP, load_synonym_groups
    from eduai_normalize import build_forms

    questions = json.load(open(a.questions, encoding="utf-8"))
    cases = json.load(open(a.cases, encoding="utf-8"))
    res = json.load(open(a.results, encoding="utf-8"))["results"]
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
        f = build_forms(text, cfg, "cs")
        return [t for t in f["lemma"] if t not in stop and not t.isdigit() and len(t) >= 4]

    key = lambda q: (q["pack"], q.get("entry_key") or f"k{q['idx']}")
    ent, df = {}, Counter()
    for q in questions:
        words = set()
        for t in [q["q"], *q["alts"]]:
            words |= set(lem(t))
        ent[key(q)] = words
        df.update(words)
    vocab = set(df)
    generic = {w for w, n in df.items() if n / len(ent) >= GENERIC_DF_SHARE}

    def expand(ws):
        out = set(ws)
        for w in ws:
            for g in group_of.get(w, ()):
                out |= groups[g]
        return out

    pair_cases, word_cases = Counter(), Counter()
    for c in cases:
        if c["set"] not in ("A", "B") or c.get("split") != "train" or not c["entry_key"]:
            continue
        if res.get(c["id"], {}).get("verdict") not in ("nic (C)", "nabídka bez správné"):
            continue
        q_words = set(lem(c["query"]))
        e_words = ent[(c["pack"], c["entry_key"])]
        covered = expand(q_words)
        unknown = {w for w in q_words if w not in vocab and not (expand([w]) & e_words)}
        uncovered = {e for e in e_words if e not in covered and e not in generic}
        for w in unknown:
            word_cases[w] += 1
            for e in uncovered:
                pair_cases[(w, e)] += 1
    kept = []
    for (w, e), n in pair_cases.items():
        share = n / word_cases[w]
        if n >= a.min_support and share >= a.min_share:
            kept.append((w, e, n, word_cases[w], round(share, 2)))
    # one partner per word: the best supported
    best = {}
    for w, e, n, tot, share in sorted(kept, key=lambda r: (-r[2], -r[4])):
        best.setdefault(w, (w, e, n, tot, share))
    rows = sorted(best.values(), key=lambda r: -r[2])
    with open(a.out_csv, "w", encoding="utf-8-sig", newline="") as fh:
        wr = csv.writer(fh, delimiter=";")
        wr.writerow(["slovo z dotazu (lemma)", "slovo ve školní otázce", "případů", "případů se slovem", "podíl"])
        wr.writerows(rows)

    def forms(word):
        f = build_forms(word, cfg, "cs")
        return f["norm"][0], f["lemma"][0], f["stem"][0]
    sql = ["-- lab what-if: mined single-word pairs (source 'lab-mined'), rolled back with the run",
           "DELETE FROM edu_ai.qa_synonym WHERE source = 'lab-mined';",
           "INSERT INTO edu_ai.qa_synonym (group_id, kind, term, term_norm, term_lemma, term_stem, is_key, lang, source, is_active) VALUES"]
    vals = []
    for i, (w, e, *_r) in enumerate(rows):
        gid = f"syn_mined_{i:03d}"
        for t, is_key in ((e, True), (w, False)):
            n, l, s = forms(t)
            vals.append(f"('{gid}','entity','{t}','{n}','{l}','{s}',{'true' if is_key else 'false'},'cs','lab-mined',true)")
    if vals:
        sql.append(",\n".join(vals) + ";")
    open(a.out_sql, "w", encoding="utf-8").write("\n".join(sql) + "\n")
    print(f"{len(word_cases)} neznámých slov v zmeškaných případech (train, A+B); "
          f"{len(rows)} dvojic splňuje podporu ≥ {a.min_support} a podíl ≥ {a.min_share}")
    for r in rows[:25]:
        print(f"  {r[0]:<16} → {r[1]:<16} {r[2]:>3}× z {r[3]:>3} ({int(r[4] * 100)} %)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
