"""Questions from the log that got no confident answer, grouped for a curator.

Input: a replay of the question log (tools/lab/replay/replay.py) and a lookup
of the texts — {"questions": {id: {q, turn, entity}}, "entries": {id: {q,
entity, pack, active}}}, exported from the replayed database. Output: one group
per distinct question (same school, same lemmas), most frequent first, with
every wording and the curated entries the search offered ("myslel jste…?").

Standard library only; the review app imports it, `python log_queue.py` writes
the CSV of step 1 (DESIGN.md, section 6).

  python3 log_queue.py REPLAY.jsonl LOOKUP.json --out queue.csv
"""

from __future__ import annotations

import argparse
import collections
import csv
import json

# decisions of qa_lexical that leave the question without a curated answer
_OUTCOME = {"B-deferred": "nabídka", "C": "nic"}


def group_key(entity: str, lemmas) -> str:
    """Same school, same lemmas (order and repetition ignored)."""
    return f"{entity}|{' '.join(sorted(set(lemmas or ())))}"


def is_follow_up(text: str, turn) -> bool:
    """A follow-up needs its conversation: "A kolik to stojí?" alone means nothing."""
    if turn not in (None, "", 0, 1, "1"):
        return True
    t = (text or "").strip().lower()
    return t.startswith("a ")


def build_queue(replay_rows, lookup) -> list[dict]:
    """Groups of not-answered questions, most frequent first."""
    questions, entries = lookup.get("questions", {}), lookup.get("entries", {})
    groups: dict[str, dict] = {}
    for r in replay_rows:
        if r.get("stage") != "before_answer":
            continue                             # answered (QA, meta, guard) or refused
        q = questions.get(str(r["id"])) or {}
        text, entity = (q.get("q") or "").strip(), r.get("entity") or q.get("entity") or ""
        if not text or not r.get("lemma") or is_follow_up(text, q.get("turn")):
            continue
        g = groups.setdefault(group_key(entity, r["lemma"]), {
            "entity": entity, "lemmas": sorted(set(r["lemma"])), "count": 0,
            "wordings": collections.Counter(), "outcomes": collections.Counter(),
            "prod": collections.Counter(), "earlier": collections.Counter(),
            "offered": {}, "log_ids": []})
        g["count"] += 1
        g["wordings"][text] += 1
        g["outcomes"][_OUTCOME.get(r.get("decision"), "nic")] += 1
        # how the production answered it: documents (rag) may well be fine
        g["prod"][r.get("prod_source") or "?"] += 1
        # the curated entry the production answered with, if it is switched off
        # since: a replaced pack that dropped a question people kept asking
        pe = entries.get(str(r.get("prod_entry"))) or {}
        if pe and pe.get("active") is False:
            g["earlier"][f'{pe.get("q", "")} [{pe.get("pack", "")}]'] += 1
        g["log_ids"].append(r["id"])
        for o in r.get("offered") or []:
            eid = str(o.get("entry"))
            e = entries.get(eid) or {}
            if not e or e.get("active") is False:
                continue
            best = g["offered"].get(eid)
            score = float(o.get("score") or 0.0)
            if best is None or score > best["score"]:
                g["offered"][eid] = {"entry": eid, "q": e.get("q", ""), "pack": e.get("pack", ""),
                                     "score": score}
    out = []
    for key, g in groups.items():
        wordings = [w for w, _ in g["wordings"].most_common()]
        out.append({
            "key": key, "entity": g["entity"], "count": g["count"],
            "question": wordings[0], "wordings": wordings,
            "outcome": g["outcomes"].most_common(1)[0][0],
            "prod": dict(g["prod"].most_common()),
            "earlier_inactive": [k for k, _ in g["earlier"].most_common()],
            "offered": sorted(g["offered"].values(), key=lambda o: -o["score"]),
            "lemmas": g["lemmas"], "log_ids": g["log_ids"],
        })
    out.sort(key=lambda g: (-g["count"], g["entity"], g["question"]))
    return out


def write_csv(queue, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["pocet", "skola", "dotaz", "dalsi_zneni", "vysledek", "produkce_odpovedela", "drive_odpoved_dnes_vypnuta",
                    "nabidnuto_1", "nabidnuto_2", "nabidnuto_3", "lemmata"])
        for g in queue:
            off = [f'{o["q"]} ({o["score"]:.2f})' for o in g["offered"][:3]]
            w.writerow([g["count"], g["entity"], g["question"],
                        "\n".join(g["wordings"][1:6]), g["outcome"],
                        ", ".join(f"{k} {v}×" for k, v in g["prod"].items()),
                        "\n".join(g["earlier_inactive"][:3]),
                        *(off + ["", "", ""])[:3], " ".join(g["lemmas"])])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("replay")
    ap.add_argument("lookup")
    ap.add_argument("--out", required=True, help="queue.csv (a table) or queue.json (for the review app)")
    ap.add_argument("--entity", help="only this school's questions")
    a = ap.parse_args()
    with open(a.replay, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    with open(a.lookup, encoding="utf-8") as fh:
        lookup = json.load(fh)
    queue = build_queue(rows, lookup)
    if a.entity:
        queue = [g for g in queue if g["entity"] == a.entity]
    if a.out.endswith(".json"):
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump({"groups": queue}, fh, ensure_ascii=False, indent=1)
    else:
        write_csv(queue, a.out)
    print(f"{len(queue)} groups from {sum(g['count'] for g in queue)} questions -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
