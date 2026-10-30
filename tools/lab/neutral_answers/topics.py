"""Topics for the queue of logged questions: what a curator decides about ONCE.

Two layers, measured on 24 Oct 2026 against a hand-made split of the queue
(458 groups of one log, 73 topics):

* TOPIC — what the question is about ("omlouvání absence"). Starts from a
  hand-made split (--manual, a JSON outside Git); a group missing from it joins
  the topic it is most similar to, or "Nezařazené". The curator can move a group
  to another topic or create a new one; that is stored with the decisions.
* SUBGROUP inside a topic — wordings the SEARCH ITSELF treats as the same
  question: the same reduction the QA cascade uses (lemmas, coverage
  stop-words, synonym groups, IDF-weighted coverage), linked when EACH side
  covers at least `THRESHOLD` of the other, average linkage. Measured: 458 →
  342 groups, 93 % of the merged pairs belong together. Different words with
  the same meaning ("Jak omluvit absenci?" / "Co doložit, když dítě
  nepřišlo?") stay apart — those need an alternative each, while one
  wording per subgroup is enough for the rest.

Standard library only.

  python3 topics.py QUEUE.json --manual temata.json [--synonyms syn.txt]
                    [--stopwords stop.txt] --out topics.json

syn.txt  = `group_id|kind|term_norm|term_lemma|term_stem` (qa_synonym, lang cs)
stop.txt = `scope|term_norm|term_lemma` (qa_stopword, lang cs; scope coverage used)
"""

from __future__ import annotations

import argparse
import itertools
import json
import math

THRESHOLD = 0.6
UNSORTED = "Nezařazené"
# the coverage fallback of search/app/nodes/qa_common.py (_COVERAGE_STOP), used
# when no stop-word export is given — the same words the search ignores
_COVERAGE_STOP = {
    "byt", "je", "jsou", "byl", "byla", "bylo", "budou", "bude",
    "a", "i", "o", "u", "v", "ve", "z", "ze", "s", "se", "si", "k", "ke",
    "na", "do", "od", "po", "za", "pro", "pri", "nad", "pod", "the",
    "to", "ten", "ta", "ty", "toto", "tak", "ale", "nebo", "co",
    "muj", "tvuj", "mi", "me", "ja", "my", "vy", "on", "ona", "ono",
}


def read_synonyms(path: str | None) -> dict[str, str]:
    """token → synonym group (single-word terms; norm and lemma forms)."""
    out: dict[str, str] = {}
    if not path:
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("|")
            if len(parts) < 4:
                continue
            gid, _kind, norm, lemma = parts[:4]
            for t in (norm, lemma):
                if t and " " not in t:
                    out.setdefault(t, gid)
    return out


def read_stopwords(path: str | None) -> set[str]:
    if not path:
        return set(_COVERAGE_STOP)
    out = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("|")
            if len(parts) >= 3 and parts[0] == "coverage":
                out |= {p for p in parts[1:3] if p}
    return out or set(_COVERAGE_STOP)


def concepts(lemmas, synonyms: dict, stop: set) -> list[str]:
    """The concepts of a question as the coverage step counts them."""
    return sorted({synonyms.get(t, t) for t in lemmas or () if t and t not in stop})


def idf_weights(concept_lists) -> dict[str, float]:
    """ln((N+1)/(df+1)) + 1 over the queue — the formula of concept_idf_weights."""
    df: dict[str, int] = {}
    for cs in concept_lists:
        for c in set(cs):
            df[c] = df.get(c, 0) + 1
    n = len(concept_lists)
    return {c: math.log((n + 1) / (d + 1)) + 1.0 for c, d in df.items()}


def _cov(a, b, w) -> float:
    if not a:
        return 0.0
    total = sum(w.get(c, 1.0) for c in a)
    return sum(w.get(c, 1.0) for c in a if c in b) / total


def similarity(a, b, w) -> float:
    """Both ways: a short question fully inside a long one is not the same question."""
    a, b = set(a), set(b)
    return min(_cov(a, b, w), _cov(b, a, w))


def cluster(keys, concept_of: dict, w: dict, threshold: float = THRESHOLD) -> list[list[str]]:
    """Average-linkage clusters of `keys`; deterministic (input order breaks ties)."""
    cl = {i: [k] for i, k in enumerate(keys)}
    sim = {}
    for i, j in itertools.combinations(range(len(keys)), 2):
        s = similarity(concept_of.get(keys[i], ()), concept_of.get(keys[j], ()), w)
        if s > 0:
            sim[(i, j)] = s
    while sim:
        (a, b), best = max(sim.items(), key=lambda kv: (kv[1], -kv[0][0], -kv[0][1]))
        if best < threshold:
            break
        na, nb = len(cl[a]), len(cl[b])
        cl[a] += cl.pop(b)
        new = {p: s for p, s in sim.items() if a not in p and b not in p}
        for k in cl:
            if k == a:
                continue
            sa = sim.get((min(a, k), max(a, k)), 0.0)
            sb = sim.get((min(b, k), max(b, k)), 0.0)
            s = (sa * na + sb * nb) / (na + nb)
            if s > 0:
                new[(min(a, k), max(a, k))] = s
        sim = new
    return list(cl.values())


def topic_id(area: str, title: str) -> str:
    return f"{area}|{title}" if area else title


def build(groups, manual: dict | None = None, synonyms: dict | None = None,
          stop: set | None = None) -> dict:
    """{topics: {id: {title, area}}, assign: {key: id}, concepts: {key: [..]},
    weights: {concept: w}} for the groups of log_queue.build_queue."""
    synonyms = synonyms or {}
    stop = set(_COVERAGE_STOP) if stop is None else stop
    manual = manual or {}
    concept_of = {g["key"]: concepts(g.get("lemmas"), synonyms, stop) for g in groups}
    w = idf_weights(list(concept_of.values()))
    topics: dict[str, dict] = {}
    assign: dict[str, str] = {}
    for g in groups:
        m = manual.get(g["key"])
        if m:
            tid = topic_id(m.get("area", ""), m["title"])
            topics.setdefault(tid, {"title": m["title"], "area": m.get("area", "")})
            assign[g["key"]] = tid
    # groups the hand-made split does not know: the most similar topic, else
    # the subgroups of the search's own reduction become topics of their own
    rest = [g["key"] for g in groups if g["key"] not in assign]
    members: dict[str, list[str]] = {}
    for k, t in assign.items():
        members.setdefault(t, []).append(k)
    leftover = []
    for k in rest:
        best, best_t = 0.0, None
        for t, ks in members.items():
            s = sum(similarity(concept_of[k], concept_of[m], w) for m in ks) / len(ks)
            if s > best:
                best, best_t = s, t
        if best_t and best >= THRESHOLD:
            assign[k] = best_t
        else:
            leftover.append(k)
    if leftover:
        if manual:
            topics.setdefault(UNSORTED, {"title": UNSORTED, "area": ""})
            for k in leftover:
                assign[k] = UNSORTED
        else:
            by_key = {g["key"]: g for g in groups}
            for c in cluster(leftover, concept_of, w):
                top = max(c, key=lambda k: by_key[k]["count"])
                tid = f"auto|{top}"
                topics[tid] = {"title": by_key[top]["question"], "area": ""}
                for k in c:
                    assign[k] = tid
    return {"topics": topics, "assign": assign, "concepts": concept_of, "weights": w}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("queue")
    ap.add_argument("--manual", help="hand-made split: {topics: {group key: {area, title}}}")
    ap.add_argument("--synonyms")
    ap.add_argument("--stopwords")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    with open(a.queue, encoding="utf-8") as fh:
        data = json.load(fh)
    groups = data.get("groups", data if isinstance(data, list) else [])
    manual = {}
    if a.manual:
        with open(a.manual, encoding="utf-8") as fh:
            manual = json.load(fh).get("topics", {})
    res = build(groups, manual, read_synonyms(a.synonyms), read_stopwords(a.stopwords))
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)
    n_sub = sum(len(cluster([k for k, t in res["assign"].items() if t == tid], res["concepts"], res["weights"]))
                for tid in res["topics"])
    print(f"{len(groups)} groups -> {len(res['topics'])} topics, {n_sub} subgroups -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
