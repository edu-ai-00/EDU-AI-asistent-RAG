"""Build the regression cases (cases.json) from sets whose right answer is known.

  python3 build_cases.py --questions otazky_idx.json --out cases.json \
      --set A=prepisy.json --set B=heldout.json [--extra extra_cases.json]

--questions  the school's curated questions [{idx, pack, entry_key, q, alts}]
--set NAME=FILE  written test queries [{idx, category, text}] of one set; the
             right answer is the question `idx` they were written from
--extra      hand-approved cases [{set, query, pack, entry_key | null, category?}];
             `entry_key: null` = no confident answer is right (ambiguous, missing)

The output is private data (a school's questions): keep it outside the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys


def case_id(set_name: str, pack: str, entry_key, query: str) -> str:
    raw = "|".join([set_name, pack, entry_key or "", query])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def key_of(q: dict) -> str:
    return q.get("entry_key") or f"k{q['idx']}"


def build(questions: list[dict], sets: dict[str, list[dict]], extra: list[dict] | None = None) -> list[dict]:
    q_by_idx = {q["idx"]: q for q in questions}
    cases, seen = [], set()

    def add(set_name, pack, entry_key, query, category):
        cid = case_id(set_name, pack, entry_key, query)
        if cid in seen:
            return
        seen.add(cid)
        cases.append({"id": cid, "set": set_name, "pack": pack, "entry_key": entry_key,
                      "query": query, "category": category})

    for name, items in sets.items():
        for it in items:
            q = q_by_idx.get(it["idx"])
            if q is None:
                continue
            add(name, q["pack"], key_of(q), it["text"], it.get("category", ""))
    for it in extra or []:
        add(it["set"], it["pack"], it.get("entry_key"), it["query"], it.get("category", ""))
    return cases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--set", action="append", default=[], metavar="NAME=FILE")
    ap.add_argument("--extra")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    questions = json.load(open(a.questions, encoding="utf-8"))
    sets = {}
    for spec in a.set:
        name, _, path = spec.partition("=")
        if not name or not path:
            sys.exit(f"--set wants NAME=FILE, got {spec!r}")
        sets[name] = json.load(open(path, encoding="utf-8"))
    extra = json.load(open(a.extra, encoding="utf-8")) if a.extra else None
    cases = build(questions, sets, extra)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(cases, fh, ensure_ascii=False, indent=1)
    per = {}
    for c in cases:
        per[c["set"]] = per.get(c["set"], 0) + 1
    print(f"{len(cases)} případů: {per} → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
