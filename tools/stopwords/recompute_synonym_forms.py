#!/usr/bin/env python3
# last change in code by Martin Safranek (2026-09-05).
"""Recompute term_norm/term_lemma/term_stem in edu_ai.qa_synonym.

    docker exec -i -e PG_PASSWORD=… edu_ai_search python - < recompute_synonym_forms.py [--apply]

THE CONVENTION IS THE OPPOSITE OF qa_stopword. Here the stoplist IS APPLIED, because
document_lexical.py:310 compares `ts <= tokens`: the token set of the term must be a
SUBSET of the query's tokens — and those are filtered by the stopwords. If the stored
form kept a preposition ("skola v priroda"), it would not be a subset of the query
("skola priroda") and the synonym would never match. So build_forms is called
directly with the same cfg as the query side: the stored value is then exactly what
a query would make of that term.

For qa_stopword it is the other way round — an applied stoplist would erase the stopword.

WHY THIS EXISTS. Of 100 active rows some do not match what the code computes today,
for three reasons:
  * de and es were NEVER stemmed — they were seeded with cfg=None and _DEFAULT_ENGINE
    had no entry for them, so engine_name returned "" and the word passed unchanged
    (welcher->welcher instead of welch, donde->donde instead of dond). The same defect
    gave 66 rows of migration 211 a wrong term_stem.
  * a different stemmer version (fotografovat -> fotografova instead of fotograf)
  * a different simplemma version (kam -> kama)
A stored stem 'donde' against the query's 'dond' is a mismatch, so the Spanish and
German synonyms do not work on the stemmer branch today.
"""
import argparse, os, sys
import psycopg2
sys.path.insert(0, "/app")
from app.settings import get_active_setting
from eduai_normalize import build_forms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    cfg = get_active_setting(None)["config"]          # the stoplist IS applied, see the docstring
    conn = psycopg2.connect(host=os.getenv("PG_HOST", "postgres"), dbname=os.getenv("PG_DB", "edu_ai"),
                            user=os.getenv("PG_USER", "edu_ai_admin"), password=os.getenv("PG_PASSWORD", "edu_ai"))
    cur = conn.cursor()
    cur.execute("SELECT id, term, lang, term_norm, term_lemma, term_stem "
                "FROM edu_ai.qa_synonym WHERE is_active ORDER BY lang, term")
    rows = cur.fetchall()

    changes = []
    for _id, term, lang, n0, l0, s0 in rows:
        f = build_forms(term, cfg, lang)
        n, l, s = (" ".join(f["norm"]), " ".join(f["lemma"]), " ".join(f["stem"]))
        if (n, l, s) != (n0, l0, s0):
            changes.append((_id, term, lang, (n0, l0, s0), (n, l, s)))

    print(f"{len(rows)} active rows, {len(changes)} to fix\n")
    for _id, term, lang, old, new in changes:
        print(f"  {lang}  {term}")
        for k, o, x in zip(("norm ", "lemma", "stem "), old, new):
            if o != x:
                print(f"        {k}  {o!r} -> {x!r}")

    if not a.apply:
        print("\n(nothing was written without --apply)")
        return
    for _id, term, lang, old, new in changes:
        cur.execute("UPDATE edu_ai.qa_synonym SET term_norm=%s, term_lemma=%s, term_stem=%s "
                    "WHERE id=%s", (*new, _id))
    conn.commit()
    print(f"\nfixed {len(changes)} rows")


if __name__ == "__main__":
    main()
