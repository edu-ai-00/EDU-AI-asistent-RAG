"""A gold set from the curator's decisions about the logged questions ("queue" of unanswered questions).

  python3 build_gold.py --queue fronta.json --topics temata.json --decisions decisions.json \
      --packs-dir DIR --out gold_cases.json [--sheet verify.xlsx --per-topic 5]

The curator sorted the groups of logged questions into topics and decided, per topic, what is
missing (`missing`), what an existing entry already answers (`alternative`, with that entry) or
what is not a question at all (`skip`). That is a label for real wordings, which the written sets
(A, B, D) do not have.

How far to trust it: a topic decision says "this entry is the closest existing answer", not that
EVERY wording of the topic belongs to it (`all_wordings` is false). The cases are therefore marked
`label: "topic"` (a weak label, like set L). A person can confirm a sample with `--sheet`; the
confirmed cases are the strong gold set (`label: "verified"`, see `apply_verification`).

Cases are [{id, set: "G", pack, entry_key, query, category, split, label, n, topic}] and are
split by GROUP (the same group is always on the same side): `train` may be tuned on, `holdout`
only measures. `skip` topics become cases with `entry_key: null` (no confident answer is right).
Private data (the school's real questions): keep the output outside the repository.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_cases                                                   # noqa: E402


def case_id(group_key: str, query: str) -> str:
    return "g" + hashlib.sha1(("gold|" + group_key + "|" + query).encode("utf-8")).hexdigest()[:11]


def build_gold(groups: list[dict], assign: dict, topic_decisions: dict) -> list[dict]:
    """The weak (topic-level) gold cases. `assign`: {group key: topic}, `topic_decisions`:
    {topic: {status, entry_key, pack, …}}; groups of topics without a usable decision are left out."""
    cases = []
    for g in groups:
        topic = assign.get(g["key"])
        dec = topic_decisions.get(topic or "") or {}
        status = dec.get("status")
        if status == "alternative" and dec.get("entry_key"):
            pack, key, cat = dec.get("pack") or "faq-provoz", dec["entry_key"], "alternativa"
        elif status == "skip":
            pack, key, cat = "faq-provoz", None, "mimo"
        else:
            continue
        split = build_cases.split_of(g["key"])
        for w in g.get("wordings") or []:
            cases.append({"id": case_id(g["key"], w), "set": "G", "pack": pack, "entry_key": key,
                          "query": w, "category": cat, "split": split, "label": "topic",
                          "n": g.get("count", 1), "topic": topic})
    return cases


def sample_for_sheet(cases: list[dict], per_topic: int) -> list[dict]:
    """The `per_topic` most frequent wordings of every topic that has an entry."""
    by_topic: dict[str, list[dict]] = {}
    for c in cases:
        if c["entry_key"]:
            by_topic.setdefault(c["topic"], []).append(c)
    out = []
    for topic, cs in sorted(by_topic.items()):
        out += sorted(cs, key=lambda c: (-c["n"], c["id"]))[:per_topic]
    return out


def apply_verification(cases: list[dict], verdicts: dict[str, str]) -> list[dict]:
    """`verdicts`: {case id: "ano" | "ne" (a note may follow: "ne, patří k absenci") | <entry_key of the right answer>}. A confirmed case
    becomes `label: "verified"`; a rejected one is dropped; a case with another entry is relabelled."""
    out = []
    for c in cases:
        v = (verdicts.get(c["id"]) or "").strip()
        if not v:
            out.append(c)
        elif re.match(r"ano\b", v, re.I):                    # "ano" or "ano, ale…"
            out.append({**c, "label": "verified"})
        elif re.match(r"ne\b", v, re.I):                     # "ne" or "ne, patří k absenci"; an entry key like "neomluvene-…" is not "ne"
            continue
        else:
            out.append({**c, "entry_key": v, "label": "verified"})
    return out


def read_verdicts(rows: list[list], blank_means: str | None = None) -> dict[str, str]:
    """{case id: "ano" | "ne"} from the worksheet rows (header first; id in column A, the answer in
    column G). Free text is read by its first word ("ne, patří k absenci" is "ne") or, failing
    that, by an "ano" in it ("divná otázka, ale ano"); text that is neither is left unjudged.
    `blank_means="ano"` reads an empty cell as "ano" (the person said so); by default it is unjudged."""
    out = {}
    for r in rows[1:]:
        if not r or not r[0]:
            continue
        v = (r[6] if len(r) > 6 else "").strip()
        if not v:
            if blank_means:
                out[r[0]] = blank_means
        elif re.match(r"ne\b", v, re.I):
            out[r[0]] = "ne"
        elif re.search(r"\bano\b", v, re.I):
            out[r[0]] = "ano"
    return out


def _answers(packs_dir: str) -> dict[str, tuple[str, str]]:
    out = {}
    for f in sorted(os.listdir(packs_dir)):
        if f.lower().endswith(".csv"):
            for r in csv.DictReader(open(os.path.join(packs_dir, f), encoding="utf-8-sig", newline="")):
                if r.get("entry_key"):
                    out[r["entry_key"]] = (r.get("canonical_q", ""), r.get("answer_md", ""))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", required=True)
    ap.add_argument("--topics", required=True)
    ap.add_argument("--decisions", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--packs-dir", help="the school's pack CSVs (for the verification sheet)")
    ap.add_argument("--sheet", help="write a worksheet in which a person confirms a sample")
    ap.add_argument("--per-topic", type=int, default=5)
    ap.add_argument("--verified", help="the filled worksheet (from --sheet): writes only the confirmed cases to --out-verified")
    ap.add_argument("--blank-is-ano", action="store_true", help="an empty answer in the worksheet means ano")
    ap.add_argument("--out-verified")
    a = ap.parse_args()
    groups = json.load(open(a.queue, encoding="utf-8"))["groups"]
    assign = json.load(open(a.topics, encoding="utf-8"))["assign"]
    td = json.load(open(a.decisions, encoding="utf-8")).get("topic_decisions", {})
    cases = build_gold(groups, assign, td)
    json.dump(cases, open(a.out, "w", encoding="utf-8"), ensure_ascii=False)
    n_ent = sum(1 for c in cases if c["entry_key"])
    print(f"{len(cases)} případů ({n_ent} s odpovědí, {len(cases) - n_ent} bez), "
          f"train {sum(c['split'] == 'train' for c in cases)}, holdout {sum(c['split'] == 'holdout' for c in cases)}")
    if a.verified:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "neutral_answers"))
        import xlsx_read
        names = xlsx_read.sheet_names(a.verified) if hasattr(xlsx_read, "sheet_names") else ["Ověření"]
        rows = xlsx_read.read_rows(a.verified, names[0])
        verdicts = read_verdicts(rows, "ano" if a.blank_is_ano else None)
        sampled = {r[0] for r in rows[1:] if r and r[0]}
        ver = [c for c in apply_verification([c for c in cases if c["id"] in sampled], verdicts)
               if c["label"] == "verified"]
        json.dump(ver, open(a.out_verified or a.out.replace(".json", "_verified.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
        print(f"ověřeno {len(ver)} z {len(sampled)} řádků tabulky "
              f"(ano {sum(v == 'ano' for v in verdicts.values())}, ne {sum(v == 'ne' for v in verdicts.values())})")
    if a.sheet:
        import xlsx_write
        ans = _answers(a.packs_dir) if a.packs_dir else {}
        rows = [["id", "Téma", "Dotaz (skutečné znění)", "Kolikrát", "Přiřazená odpověď (otázka)",
                 "Přiřazená odpověď (text)", "Sedí? (ano / ne / klíč jiné odpovědi)"]]
        for c in sample_for_sheet(cases, a.per_topic):
            q, text = ans.get(c["entry_key"], (c["entry_key"], ""))
            rows.append([c["id"], c["topic"].split("|", 1)[-1], c["query"], c["n"], q, text, ""])
        xlsx_write.write_xlsx(a.sheet, rows, widths=[14, 30, 50, 9, 40, 60, 24], input_cols={6}, sheet="Ověření")
        print(f"tabulka k ověření: {len(rows) - 1} řádků → {a.sheet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
