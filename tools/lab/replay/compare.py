"""Compare two replay runs (replay.py) of the same questions on the same data.

  python3 compare.py OLD.jsonl NEW.jsonl --lookup lookup.json --out changes.csv

`lookup.json` maps ids to readable text, exported from the replayed database:
{"questions": {log_id: raw_query}, "entries": {entry_id: canonical_q}}. Without
it the table carries ids only. Standard library only; runs on the host.

Every question lands in exactly one category:

  same answer        both answered with the same curated entry (or the same
                     non-QA step: guard, meta, …)
  other answer       both answered from QA, with different entries
  QA only before     the old version answered from QA, the new one does not
  QA only after      the new version answers from QA, the old one did not
  documents          neither answered; compared by the chunks gathered
                     (same top chunk / different)
"""

from __future__ import annotations

import argparse
import collections
import csv
import json

_QA = {"qa_exact", "qa_lexical", "qa_vector"}


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return {r["id"]: r for r in map(json.loads, fh)}


def _kind(r):
    if r.get("stage") == "answered":
        return r.get("answer_source") or "answered"
    return r.get("stage") or "?"


def category(o, n) -> str:
    ko, kn = _kind(o), _kind(n)
    if ko in _QA and kn in _QA:
        return "same answer" if o.get("entry") == n.get("entry") else "other answer"
    if ko in _QA:
        return "QA only before"
    if kn in _QA:
        return "QA only after"
    if ko == kn == "before_answer":
        co, cn = o.get("chunks") or [], n.get("chunks") or []
        if not co and not cn:
            return "documents: none either"
        return ("documents: same top chunk" if co[:1] == cn[:1]
                else "documents: other top chunk")
    return "same answer" if ko == kn else f"{ko} -> {kn}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--lookup")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    old, new = _load(a.old), _load(a.new)
    lk = {"questions": {}, "entries": {}}
    if a.lookup:
        with open(a.lookup, encoding="utf-8") as fh:
            lk = json.load(fh)
    q = lambda i: lk["questions"].get(str(i), "")          # noqa: E731
    e = lambda i: lk["entries"].get(str(i), "") if i else ""  # noqa: E731

    cats = collections.Counter()
    chunk_overlap = []
    rows = []
    for i in sorted(old.keys() & new.keys()):
        o, n = old[i], new[i]
        c = category(o, n)
        cats[c] += 1
        if c.startswith("documents") and (o.get("chunks") or n.get("chunks")):
            so, sn = set((o.get("chunks") or [])[:5]), set((n.get("chunks") or [])[:5])
            chunk_overlap.append(len(so & sn) / max(len(so | sn), 1))
        if c not in ("same answer", "documents: same top chunk", "documents: none either"):
            rows.append({
                "id": i, "kategorie": c, "dotaz": q(i),
                "pred_zdroj": _kind(o), "pred_odpoved": e(o.get("entry")),
                "po_zdroj": _kind(n), "po_odpoved": e(n.get("entry")),
                "produkce_zdroj": o.get("prod_source") or "",
                "produkce_odpoved": e(o.get("prod_entry")),
                "pred_lemma": " ".join(o.get("lemma") or []),
                "po_lemma": " ".join(n.get("lemma") or []),
            })
    with open(a.out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["id"], delimiter=";")
        w.writeheader()
        w.writerows(rows)

    total = sum(cats.values())
    print(f"questions compared: {total}")
    for c, k in cats.most_common():
        print(f"  {c:<32} {k:>5}  {100 * k / total:5.1f} %")
    if chunk_overlap:
        print(f"documents: mean top-5 chunk overlap {100 * sum(chunk_overlap) / len(chunk_overlap):.0f} %")
    print(f"changed rows: {len(rows)} -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
