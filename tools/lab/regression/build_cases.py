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
import csv
import hashlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "neutral_answers"))

HOLDOUT_SHARE = 30      # % of the curated entries held back (never tuned on)


def case_id(set_name: str, pack: str, entry_key, query: str) -> str:
    raw = "|".join([set_name, pack, entry_key or "", query])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def key_of(q: dict) -> str:
    return q.get("entry_key") or f"k{q['idx']}"


def split_of(text: str) -> str:
    """Deterministic: the same entry (or negative text) is always on the same side.
    'train' may be tuned on; 'holdout' only measures."""
    h = int(hashlib.sha1(("split|" + text).encode("utf-8")).hexdigest(), 16) % 100
    return "holdout" if h < HOLDOUT_SHARE else "train"


def build(questions: list[dict], sets: dict[str, list[dict]], extra: list[dict] | None = None,
          negatives: list[dict] | None = None, equivalents: list[list[str]] | None = None) -> list[dict]:
    """negatives: [{set, text, category?}] — questions the school's answers do not cover
    (expected: no confident answer). equivalents: [[key, key, …]] groups of entry keys
    that are equally right for one another (confirmed by the curator)."""
    q_by_idx = {q["idx"]: q for q in questions}
    group_of = {}
    for g in equivalents or []:
        for k in g:
            group_of[k] = [x for x in g if x != k]
    cases, seen = [], set()

    def add(set_name, pack, entry_key, query, category):
        cid = case_id(set_name, pack, entry_key, query)
        if cid in seen:
            return
        seen.add(cid)
        c = {"id": cid, "set": set_name, "pack": pack, "entry_key": entry_key,
             "query": query, "category": category,
             "split": split_of(entry_key if entry_key else "neg|" + query)}
        if entry_key and group_of.get(entry_key):
            c["accept"] = group_of[entry_key]
        cases.append(c)

    for name, items in sets.items():
        for it in items:
            q = q_by_idx.get(it["idx"])
            if q is None:
                continue
            add(name, q["pack"], key_of(q), it["text"], it.get("category", ""))
    for it in extra or []:
        add(it["set"], it["pack"], it.get("entry_key"), it["query"], it.get("category", ""))
    for it in negatives or []:
        add(it["set"], it.get("pack", ""), None, it["text"], it.get("category", ""))
    return cases


def apply_fixes(cases: list[dict], fixes: dict, questions: list[dict]) -> list[dict]:
    """Corrections of WRONG LABELS found after a run (not of results): {"drop": [{set, query}],
    "relabel": [{set, query, entry_key}]}. Use it only for a label that is plainly wrong
    (a question "without an answer" that the school does answer); every fix goes into LEDGER.md."""
    pack_of = {key_of(q): q["pack"] for q in questions}
    drop = {(d["set"], d["query"].strip()) for d in fixes.get("drop", [])}
    relabel = {(d["set"], d["query"].strip()): d["entry_key"] for d in fixes.get("relabel", [])}
    out = []
    for c in cases:
        k = (c["set"], c["query"].strip())
        if k in drop:
            continue
        if k in relabel:
            key = relabel[k]
            c = {**c, "entry_key": key, "pack": pack_of.get(key, c["pack"]), "split": split_of(key),
                 "id": case_id(c["set"], pack_of.get(key, c["pack"]), key, c["query"])}
            c.pop("accept", None)
        out.append(c)
    return out


def read_table(path: str) -> list[dict]:
    """A filled worksheet as a list of {header: value}: .xlsx (preferred, no delimiter
    problems) or .csv (comma or semicolon)."""
    if path.lower().endswith(".xlsx"):
        import xlsx_read
        rows = xlsx_read.read_rows(path)
        if not rows:
            return []
        head = [h.strip() for h in rows[0]]
        return [{h: (r[i] if i < len(r) else "") for i, h in enumerate(head)} for r in rows[1:]]
    raw = open(path, "rb").read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:                  # a Czech Excel saves CSV as Windows-1250
        text = raw.decode("cp1250")
    first = text.split("\n", 1)[0]
    return list(csv.DictReader(io.StringIO(text, newline=""),
                               delimiter=";" if first.count(";") >= first.count(",") else ","))


def read_d_csv(path: str, questions: list[dict], id_map: dict | None = None) -> list[dict]:
    """The filled worksheet of set D (see make_d_template.py): rows `id;odpoved;otazka_1;otazka_2`
    answer-based (id = entry key), and rows with `id` empty or `BEZ` = a question with no answer."""
    known = {key_of(q): q for q in questions}
    out = []
    for r in read_table(path):
        r = {(k or "").split(" (")[0].strip(): v for k, v in r.items()}      # "otazka_1 (normálně)" → "otazka_1"
        rid = (r.get("id") or "").strip()
        rid = (id_map or {}).get(rid, rid)
        for col in ("otazka_1", "otazka_2"):
            text = (r.get(col) or "").strip()
            if not text:
                continue
            if rid in ("", "BEZ"):
                out.append({"set": "D", "pack": "", "entry_key": None, "query": text,
                            "category": "bez_odpovedi"})
            elif rid in known:
                out.append({"set": "D", "pack": known[rid]["pack"], "entry_key": rid, "query": text,
                            "category": col})
    return out


def read_equiv_csv(path: str) -> list[list[str]]:
    """The candidate list of make_equiv_candidates.py with `shodne (ano/ne)` filled in:
    rows marked "ano" become groups of equally right entry keys (pairs are merged)."""
    groups: list[set[str]] = []
    for r in read_table(path):
        if (r.get("shodne (ano/ne)") or "").strip().lower() not in ("ano", "a", "yes", "y", "1"):
            continue
        pair = {r["klic_A"].strip(), r["klic_B"].strip()}
        for g in [g for g in groups if g & pair]:
            groups.remove(g)
            pair |= g
        groups.append(pair)
    return [sorted(g) for g in groups]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--set", action="append", default=[], metavar="NAME=FILE")
    ap.add_argument("--extra")
    ap.add_argument("--negatives", help="JSON [{set, text}] — questions without an answer in the data")
    ap.add_argument("--equivalents", help="JSON [[key, key], …] confirmed equivalent answers")
    ap.add_argument("--equiv-csv", help="candidates of make_equiv_candidates.py (.xlsx or .csv) with the column `shodne` filled in")
    ap.add_argument("--fixes", help="JSON {drop: [{set, query}], relabel: [{set, query, entry_key}]} — wrong labels only")
    ap.add_argument("--d-csv", help="the filled worksheet of set D (.xlsx or .csv)")
    ap.add_argument("--d-map", help="sada_D_mapa.json (opaque id → entry key) from make_d_template.py")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    questions = json.load(open(a.questions, encoding="utf-8"))
    sets = {}
    for spec in a.set:
        name, _, path = spec.partition("=")
        if not name or not path:
            sys.exit(f"--set wants NAME=FILE, got {spec!r}")
        sets[name] = json.load(open(path, encoding="utf-8"))
    extra = json.load(open(a.extra, encoding="utf-8")) if a.extra else []
    if a.d_csv:
        extra = [*extra, *read_d_csv(a.d_csv, questions, json.load(open(a.d_map, encoding="utf-8")) if a.d_map else None)]
    negatives = json.load(open(a.negatives, encoding="utf-8")) if a.negatives else None
    equivalents = json.load(open(a.equivalents, encoding="utf-8")) if a.equivalents else []
    if a.equiv_csv:
        equivalents = [*equivalents, *read_equiv_csv(a.equiv_csv)]
    cases = build(questions, sets, extra, negatives, equivalents)
    if a.fixes:
        cases = apply_fixes(cases, json.load(open(a.fixes, encoding="utf-8")), questions)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(cases, fh, ensure_ascii=False, indent=1)
    per = {}
    for c in cases:
        k = f"{c['set']}:{c['split']}"
        per[k] = per.get(k, 0) + 1
    print(f"{len(cases)} případů: {dict(sorted(per.items()))} → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
