"""Worksheet for set D (the frozen test set, written by a person).

  python3 make_d_template.py --questions otazky_idx.json --packs-dir DIR_WITH_faq-*.csv \
        --out sada_D_sablona.xlsx --map-out sada_D_mapa.json [--extra-train 40] [--none-rows 25] [--seed 5]

The author sees only the ANSWER of a curated entry and writes the question a
person would ask, in two styles. The question therefore does not copy the wording
in the data and the label (which entry) comes with it. The hold-out entries (never
tuned on, see build_cases.split_of) are all included, plus a few training ones.
Rows with id BEZ are for questions the data has no answer for.

The ids in the worksheet are opaque (D001…): the entry keys are made of the
question text and would give the wording away. The map (id → entry key) is kept
separately and is read by build_cases.py --d-map.

Fill it in, then: build_cases.py --d-csv sada_D_vyplnena.xlsx --d-map sada_D_mapa.json …
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_cases                                                   # noqa: E402
import xlsx_write                                                    # noqa: E402


def answers_from(dir_path: str, packs: list[str]) -> dict[str, str]:
    out = {}
    for pack in packs:
        path = os.path.join(dir_path, pack + ".csv")
        with open(path, encoding="utf-8-sig", newline="") as fh:
            head = fh.readline()
            fh.seek(0)
            for r in csv.DictReader(fh, delimiter=";" if head.count(";") >= head.count(",") else ","):
                out[r["entry_key"]] = r["answer_md"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--packs-dir", required=True)
    ap.add_argument("--packs", nargs="+", default=["faq-jpz", "faq-provoz"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--map-out", required=True, help="id → entry key (keep it away from the authors)")
    ap.add_argument("--extra-train", type=int, default=40)
    ap.add_argument("--none-rows", type=int, default=25)
    ap.add_argument("--seed", type=int, default=5)
    a = ap.parse_args()
    questions = json.load(open(a.questions, encoding="utf-8"))
    answers = answers_from(a.packs_dir, a.packs)
    keys = [build_cases.key_of(q) for q in questions if build_cases.key_of(q) in answers]
    hold = [k for k in keys if build_cases.split_of(k) == "holdout"]
    train = [k for k in keys if build_cases.split_of(k) == "train"]
    rnd = random.Random(a.seed)
    rnd.shuffle(train)
    chosen = hold + train[:a.extra_train]
    rnd.shuffle(chosen)
    rows = [["id", "odpoved", "otazka_1 (normálně)", "otazka_2 (krátce / hovorově)"]]
    mapping = {}
    for n, k in enumerate(chosen, 1):
        mapping[f"D{n:03d}"] = k
        rows.append([f"D{n:03d}", answers[k].replace("\n", " "), "", ""])
    for _ in range(a.none_rows):
        rows.append(["BEZ", "(otázka, na kterou škola v datech odpověď nemá)", "", ""])
    xlsx_write.write_xlsx(a.out, rows, widths=[8, 90, 45, 45], input_cols={2, 3}, sheet="Sada D")
    with open(a.map_out, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, ensure_ascii=False, indent=1)
    print(f"{len(chosen)} odpovědí ({len(hold)} zadržených + {len(chosen) - len(hold)} trénovacích) "
          f"a {a.none_rows} řádků BEZ → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
