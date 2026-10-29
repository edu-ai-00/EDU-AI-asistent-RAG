"""Gap report for the curator: which topics do real users ask about that the school's QA does not cover?

  python3 make_gap_report.py --queue fronta_ZS_KAVCI_HORY_ZS.json --topics temata_ZS_KAVCI_HORY_ZS.json \
        --replay pkg.jsonl --lookup log_lookup.json --total-asks 2739 --out mezery_v_obsahu.xlsx

Inputs: the queue of logged questions without a confident curated answer, grouped by wording
(`groups`, with the log ids), their assignment to topics, and a replay of the log on the CURRENT
state (replay.py). Per topic the report counts the asks the school has NO curated answer for
(the production answered them from documents or not at all) and those where an answer exists but
is not found (the production solved them through a curated entry, the replay does not). It lists the
keywords of the topic and the curated question the system offers today.

No query text is written: counts, topic names, lemma keywords and the school's own curated questions
only. The curator fills in two yellow columns (decision, note).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xlsx_write                                                    # noqa: E402

QA = {"qa_exact", "qa_lexical", "qa_vector"}
SKIP_WORDS = {"co", "jak", "kdy", "kde", "kdo", "proc", "jaky", "ktery", "mit", "moci", "byt", "muset",
              "skola", "zak", "dite", "chtit", "prosim", "dobry", "den", "ahoj"}


def answered_now(r: dict) -> bool:
    return r.get("stage") == "answered" and r.get("answer_source") in QA and r.get("outcome") != "suggest"


def build_rows(groups, assign, replay, total_asks, entries=None):
    """`entries`: {entry_id: {pack, active, entity}} — a curated entry of a switched-off pack (the
    production's test packs) is NOT an answer the school has today."""
    entries = entries or {}
    topic_asks = collections.Counter()
    t_nodata, t_notfound, t_ok = collections.Counter(), collections.Counter(), collections.Counter()
    t_words, t_kw, t_offer = collections.Counter(), collections.defaultdict(collections.Counter), \
        collections.defaultdict(collections.Counter)
    for g in groups:
        topic = assign.get(g["key"])
        if not topic:
            continue
        t_words[topic] += 1
        for w in g.get("lemmas") or []:
            if w not in SKIP_WORDS:
                t_kw[topic][w] += g["count"]
        off = g.get("offered") or []
        if off:
            t_offer[topic][off[0].get("q") or ""] += g["count"]
        for lid in g.get("log_ids") or []:
            r = replay.get(lid)
            if r is None:
                continue
            topic_asks[topic] += 1
            if answered_now(r):
                t_ok[topic] += 1
            elif r.get("prod_source") in QA and r.get("prod_entry") and \
                    (not entries or (entries.get(r["prod_entry"]) or {}).get("active")):
                t_notfound[topic] += 1          # the production found an ACTIVE curated entry, the replay does not
            else:
                t_nodata[topic] += 1            # no curated entry answered it in the production either
    is_out = lambda t: t.partition("|")[0].strip().lower().startswith("mimo qa")
    order = sorted(topic_asks, key=lambda t: (is_out(t), -t_nodata[t], -t_notfound[t], -topic_asks[t]))
    rows, cum = [], 0
    for i, t in enumerate(order, 1):
        area, _, title = t.partition("|")
        typ = "mimo QA (pozdrav, krátká navazující otázka, nesmysl)" if is_out(t) else "obsah"
        if typ == "obsah":
            cum += t_nodata[t]
        offer = t_offer[t].most_common(1)[0][0] if t_offer[t] else ""
        rows.append([i, typ, area.strip(), title.strip() or t, topic_asks[t], t_nodata[t], t_notfound[t], t_ok[t],
                     t_words[t], round(100 * topic_asks[t] / total_asks, 1),
                     round(100 * cum / total_asks, 1) if typ == "obsah" else "",
                     ", ".join(w for w, _ in t_kw[t].most_common(6)), offer, "", ""])
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", required=True)
    ap.add_argument("--topics", required=True)
    ap.add_argument("--replay", required=True)
    ap.add_argument("--total-asks", type=int, required=True, help="all logged asks of the school (the denominator)")
    ap.add_argument("--lookup", help="log_lookup.json (entries with `active`): the entries of switched-off packs do not count")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    groups = json.load(open(a.queue, encoding="utf-8"))["groups"]
    assign = json.load(open(a.topics, encoding="utf-8"))["assign"]
    replay = {r["id"]: r for r in map(json.loads, open(a.replay, encoding="utf-8"))}
    entries = {}
    if a.lookup:
        entries = json.load(open(a.lookup, encoding="utf-8")).get("entries", {})
    rows = build_rows(groups, assign, replay, a.total_asks, entries)
    header = ["Pořadí", "Typ", "Oblast", "Téma", "Dotazů celkem", "z toho škola odpověď NEMÁ",
              "z toho odpověď existuje, ale nenalezena", "z toho dnes odpovězeno", "Různých znění",
              "Podíl všech dotazů školy (%)", "Kumulativně: dotazy bez odpovědi (%)", "Klíčová slova (lemmata)",
              "Dnes nabízená nejbližší odpověď", "Rozhodnutí kurátora (napsat / už existuje / mimo rozsah)", "Poznámka"]
    xlsx_write.write_xlsx(a.out, [header, *rows], widths=[8, 22, 18, 46, 10, 14, 16, 12, 10, 12, 14, 36, 46, 30, 30],
                          input_cols={13, 14}, sheet="Mezery v obsahu")
    content = [r for r in rows if r[1] == "obsah"]
    print(f"{len(rows)} témat ({len(content)} obsahových) → {a.out}")
    print(f"dotazů bez odpovědi v datech (obsah): {sum(r[5] for r in content)}, "
          f"odpověď existuje, ale nenalezena: {sum(r[6] for r in content)}, dnes odpovězeno: {sum(r[7] for r in content)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
