"""Set N: questions the school's curated answers do NOT cover (expected: no confident answer).

  python3 make_negatives.py --xlsx "Otázky EDU.xlsx" --out negatives.json [--per-source 150] [--seed 11]

Source: publicly collected questions — mathematics (sheet "Mat", column A), Czech
language ("ČJ", A), teachers' questions ("Učitelé", C). Sheet "Brainly" is left out
(English tasks), "Přijímačkymaturita" is left out (the school's JPZ answers may
cover it). The output is a list for build_cases.py --negatives.

Nothing guarantees that a school answer really is missing for every text: the
run lists the negatives that got a CONFIDENT answer, and a person looks at those
(a legitimate answer is not an error; the case is removed from N then).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "neutral_answers"))
import xlsx_read                                                    # noqa: E402

SOURCES = (("Mat", 0, "matematika"), ("ČJ", 0, "cestina"), ("Učitelé", 2, "ucitele"))
URL = re.compile(r"https?://|www\.", re.I)


def clean(text: str) -> str | None:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if not (8 <= len(t) <= 200) or URL.search(t) or re.fullmatch(r"[\d\s.,+\-*/=()^]+", t):
        return None
    if "?" not in t and len(t.split()) < 3:
        return None
    return t


def collect(rows_by_sheet: dict[str, list[list[str]]], per_source: int, seed: int) -> list[dict]:
    rnd = random.Random(seed)
    out, seen = [], set()
    for sheet, col, tag in SOURCES:
        cand = []
        for r in rows_by_sheet.get(sheet, []):
            t = clean(r[col]) if len(r) > col else None
            if t and t.lower() not in seen:
                seen.add(t.lower())
                cand.append(t)
        rnd.shuffle(cand)
        out += [{"set": "N", "text": t, "category": tag} for t in cand[:per_source]]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-source", type=int, default=150)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    rows = {s: xlsx_read.read_rows(a.xlsx, s) for s, _, _ in SOURCES}
    neg = collect(rows, a.per_source, a.seed)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(neg, fh, ensure_ascii=False, indent=1)
    per = {}
    for n in neg:
        per[n["category"]] = per.get(n["category"], 0) + 1
    print(f"{len(neg)} záporných otázek: {per} → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
