"""Lint of QA entries and their alternatives BEFORE an upload (lab tool).

  tools/lab/regression/alt_lint.sh PACKS_DIR [--out report.csv] [--only KEY ...] [--t-collision 0.9]

Why. `qa_lexical` covers a query against the UNION of the lemmas of those variants of an
entry that share a token with the query (the canonical question is a variant like the
others). An alternative therefore only helps with lemmas the entry does not have yet, and
a rare lemma helps more than a common one. Per alternative this reports:

  new_lemmas  the lemmas it adds to the union of the canonical question and the alternatives
              BEFORE it (in the order they stand in the pack)
  delta       the IDF-weighted sum of those lemmas, ln((N+1)/(df+1))+1 with df = the entries
              of the pack whose union holds the lemma (the production weight, without the
              synonym groups)
  flags       no_new_lemma   adds nothing (a re-wording with the same lemmas)
              duplicate      the same lemma set as the canonical question or an earlier alternative
              overlaps       as a QUERY it would be covered at >= t_collision (0.9) by ANOTHER entry
                             (the entry it could take a confident answer from); the coverage is
                             the IDF-weighted share, a heuristic: look at the flagged ones, do not trust it blindly

  union_cross    the entry's union, WITH this alternative, covers at >= t_cross (0.6, `t_answer`) a
                 variant of ANOTHER entry (its question, alternatives or extra probes) that the union
                 did not cover before: the alternative has not stolen anything yet, but a query worded
                 like that variant now reaches the answer bar here. The coverage of a query is against
                 the UNION, so this is the failure of adding alternatives (see LEDGER, 3 Nov): it does
                 not show on one alternative, only on the growing union.

Per answer: `ano_ne` (starts with yes/no) and `hard:<kind>` (a phone number, e-mail, date,
time or amount: a WARNING, because a fact that differs from school to school does not belong
in a text that is written for all of them).

The logic is plain functions (stdlib only, unit-tested); the lemmatization is passed in, so
the CLI runs inside the search image where `eduai_normalize` is installed (alt_lint.sh).
The report contains the question texts: keep it private.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ANO_NE = re.compile(r"^\s*(ano|ne|jo|nikoli|určitě|samozřejmě)\b\s*[,.!:–-]", re.I)
HARD = (
    ("phone", re.compile(r"(?<!\d)(?:\+?420[ ]?)?\d{3}[ ]?\d{3}[ ]?\d{3}(?!\d)")),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("date", re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.(\s?\d{2,4})?")),
    ("time", re.compile(r"\b\d{1,2}:\d{2}\b|\b\d{1,2}[.]\d{2}\s?(hod|h)\b", re.I)),
    ("amount", re.compile(r"\b\d[\d  ]*\s?(Kč|korun|eur|€)\b", re.I)),
)


def idf_weights(unions: list[set]) -> dict[str, float]:
    """{lemma: ln((N+1)/(df+1)) + 1} over the entry unions (N = entries, df = entries holding it)."""
    n = len(unions)
    df: dict[str, int] = {}
    for u in unions:
        for lem in u:
            df[lem] = df.get(lem, 0) + 1
    return {lem: math.log((n + 1) / (d + 1)) + 1.0 for lem, d in df.items()}


def weighted_cover(lemmas: set, union: set, w: dict[str, float]) -> float:
    """Share (IDF-weighted) of `lemmas` that `union` holds: the coverage a query made of
    these lemmas would get against an entry."""
    tot = sum(w.get(x, 1.0) for x in lemmas)
    return sum(w.get(x, 1.0) for x in lemmas if x in union) / tot if tot else 0.0


def crossings(union_before: set, union_after: set, probes: list, w: dict, t: float, own: int) -> list[int]:
    """Owners (indexes) of the probes that `union_after` covers at >= t and `union_before` did not.
    `probes`: [(owner_index, lemma_set)]; the entry's own probes (owner == own) are skipped."""
    out = []
    for owner, lem in probes:
        if owner == own or owner in out:
            continue
        if weighted_cover(lem, union_after, w) >= t and weighted_cover(lem, union_before, w) < t:
            out.append(owner)
    return out


def build_probes(entries: list[dict], lemmatize, extra: dict | None = None) -> list:
    """Lemma sets of every question and alternative of every entry, plus `extra[(pack, key)]`
    texts (for example paraphrases that are not alternatives yet): [(entry_index, lemma_set)]."""
    probes = []
    for i, e in enumerate(entries):
        for text in [e["q"], *e["alts"], *((extra or {}).get((e["pack"], e["entry_key"])) or [])]:
            lem = set(lemmatize(text))
            if lem:
                probes.append((i, lem))
    return probes


def answer_flags(answer: str) -> list[str]:
    out = []
    if ANO_NE.search(answer or ""):
        out.append("ano_ne")
    for kind, rx in HARD:
        if rx.search(answer or ""):
            out.append(f"hard:{kind}")
    return out


def lint_entries(entries: list[dict], lemmatize, t_collision: float = 0.9, t_cross: float | None = 0.6,
                 extra_probes: dict | None = None) -> list[dict]:
    """One row per alternative. `entries`: {pack, entry_key, q, alts, answer};
    `lemmatize(text) -> set[str]`. Every alternative is judged against the canonical question
    and the alternatives BEFORE it, and (as a query) against the union of every other entry."""
    for e in entries:
        e["_canon"] = lemmatize(e["q"])
        e["_alts"] = [lemmatize(a) for a in e["alts"]]
        e["_union"] = set(e["_canon"]).union(*e["_alts"]) if e["_alts"] else set(e["_canon"])
    w = idf_weights([e["_union"] for e in entries])
    probes = build_probes(entries, lemmatize, extra_probes) if t_cross is not None else []
    rows = []
    for ei, e in enumerate(entries):
        union = set(e["_canon"])
        seen_sets = [frozenset(e["_canon"])]
        for alt_text, lem in zip(e["alts"], e["_alts"]):
            new = lem - union
            flags = []
            if not new:
                flags.append("no_new_lemma")
            if frozenset(lem) in seen_sets:
                flags.append("duplicate")
            best = ("", 0.0)
            if lem:
                for o in entries:
                    if o is e:
                        continue
                    cov = weighted_cover(lem, o["_union"], w)
                    if cov > best[1]:
                        best = (o["entry_key"], cov)
                if best[1] >= t_collision:
                    flags.append(f"overlaps:{best[0]}@{best[1]:.2f}")
            cross = crossings(union, union | lem, probes, w, t_cross, ei) if (probes and new) else []
            if cross:
                flags.append(f"union_cross:{len(cross)}")
            rows.append({"pack": e["pack"], "entry_key": e["entry_key"], "alternative": alt_text,
                         "new_lemmas": sorted(new), "delta": round(sum(w.get(x, 1.0) for x in new), 3),
                         "best_other": best[0], "best_other_cov": round(best[1], 3), "flags": flags})
            union |= lem
            seen_sets.append(frozenset(lem))
    return rows


def load_entries(packs_dir: str) -> list[dict]:
    """The active entries of the QA pack tables in a directory (.csv / .xlsx): the columns
    `entry_key`, `canonical_q`, `variants` (one per line), `answer_md` or `answer`, `is_active`."""
    import build_cases
    out = []
    for f in sorted(os.listdir(packs_dir)):
        if not f.lower().endswith((".csv", ".xlsx")):
            continue
        pack = os.path.splitext(f)[0]
        try:
            table = build_cases.read_table(os.path.join(packs_dir, f))
        except Exception as e:                                   # a file that is not a QA pack
            print(f"přeskočeno {f}: {e}", file=sys.stderr)
            continue
        for r in table:
            key, q = (r.get("entry_key") or "").strip(), (r.get("canonical_q") or "").strip()
            if not key or not q:
                continue
            if (r.get("is_active") or "TRUE").strip().upper() in ("FALSE", "0", "NO", "N"):
                continue
            alts = [a.strip() for a in (r.get("variants") or "").replace("\r", "").split("\n") if a.strip()]
            out.append({"pack": pack, "entry_key": key, "q": q, "alts": alts,
                        "answer": (r.get("answer_md") or r.get("answer") or "").strip()})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("packs_dir")
    ap.add_argument("--stoplist", default=os.path.join(HERE, "..", "..", "..", "contracts", "parity", "stoplist.json"))
    ap.add_argument("--t-collision", type=float, default=0.9,
                    help="coverage (IDF-weighted) at which ANOTHER entry holds the alternative: default 0.9. "
                         "Calibrated on the existing curated packs of one school: at 0.60 a third of their "
                         "alternatives would be flagged (39 % / 16 %), at 0.90 about 7 % / 2 % — a review list, "
                         "not a verdict")
    ap.add_argument("--t-cross", type=float, default=0.6,
                    help="coverage of another entry's variant that the growing union may not newly reach (default t_answer 0.60); 1.1 turns the check off")
    ap.add_argument("--only", nargs="*", help="report only these entry keys (the rest still count for IDF and collisions)")
    ap.add_argument("--out", help="CSV of every alternative (contains the question texts: keep it private)")
    a = ap.parse_args()
    sys.path.insert(0, os.path.join(HERE, "..", "neutral_answers"))
    import machine
    machine.configure(a.stoplist)
    if not machine.available():
        print("eduai_normalize není dostupné: pouštěj přes alt_lint.sh (v obrazu vyhledávání)", file=sys.stderr)
        return 2

    def lemmatize(text: str) -> set:
        return set((machine.analyse(text) or {}).get("lemma") or [])

    entries = load_entries(a.packs_dir)
    rows = lint_entries(entries, lemmatize, a.t_collision, a.t_cross if a.t_cross <= 1.0 else None)
    if a.only:
        keep = set(a.only)
        rows = [r for r in rows if r["entry_key"] in keep]
        entries_rep = [e for e in entries if e["entry_key"] in keep]
    else:
        entries_rep = entries
    ans = {(e["pack"], e["entry_key"]): answer_flags(e["answer"]) for e in entries_rep}
    for pack in sorted({r["pack"] for r in rows} | {e["pack"] for e in entries_rep}):
        rp = [r for r in rows if r["pack"] == pack]
        ep = [e for e in entries_rep if e["pack"] == pack]
        n_alt = len(rp)
        cnt = lambda f: sum(1 for r in rp if any(x == f or x.startswith(f + ":") for x in r["flags"]))   # noqa: E731
        af = sum(1 for e in ep if ans[(pack, e["entry_key"])])
        print(f"{pack}: záznamů {len(ep)}, alternativ {n_alt} | bez nového lemmatu {cnt('no_new_lemma')}, "
              f"duplicit {cnt('duplicate')}, překryv s cizím záznamem {cnt('overlaps')}, "
              f"unie nově pokrývá cizí variantu {cnt('union_cross')} | odpovědí k prohlídce {af}")
    if a.out:
        with open(a.out, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["balicek", "entry_key", "alternativa", "nova_lemmata", "delta_IDF", "nejblizsi_cizi", "pokryti_cizim", "priznaky"])
            for r in rows:
                w.writerow([r["pack"], r["entry_key"], r["alternative"], " ".join(r["new_lemmas"]), r["delta"],
                            r["best_other"], r["best_other_cov"], " ".join(r["flags"])])
            for (pack, key), fl in ans.items():
                if fl:
                    w.writerow([pack, key, "(odpověď)", "", "", "", "", " ".join(fl)])
        print(f"uloženo: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
