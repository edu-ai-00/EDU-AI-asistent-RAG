#!/usr/bin/env python3
# last change in code by Martin Safranek (2026-09-05).
"""Přepočítá term_norm/term_lemma/term_stem v edu_ai.qa_synonym.

    docker exec -i -e PG_PASSWORD=… edu_ai_search python - < recompute_synonym_forms.py [--apply]

KONVENCE JE OPAČNÁ NEŽ U qa_stopword. Tady se stoplist APLIKUJE, protože
document_lexical.py:310 porovnává `ts <= tokens`, tedy množina tokenů termu musí
být PODMNOŽINOU tokenů dotazu — a ty jsou stopslovy profiltrované. Kdyby si
uložený tvar nechal předložku („skola v priroda"), nebyl by podmnožinou dotazu
(„skola priroda") a synonymum by se nikdy netrefilo. Proto se volá rovnou
build_forms se stejným cfg, jaké má dotazová strana: uložená hodnota je pak
přesně to, co by z toho termu vyrobil dotaz.

U qa_stopword je to naopak — tam by aplikovaný stoplist stopslovo vymazal.

PROČ TO VZNIKLO. Ze 100 aktivních řádků jich část nesedí s tím, co kód počítá
dnes, a příčiny jsou tři:
  * de a es se NIKDY nezastemovaly — seedovalo se s cfg=None a _DEFAULT_ENGINE
    pro ně neměl záznam, takže engine_name vrátil "" a slovo prošlo beze změny
    (welcher->welcher místo welch, donde->donde místo dond). Tatáž vada dala
    špatný term_stem 66 řádkům migrace 211.
  * jiná verze stemmeru (fotografovat -> fotografova místo fotograf)
  * jiná verze simplemma (kam -> kama)
Uložený stem 'donde' proti dotazovému 'dond' je neshoda, takže španělská a
německá synonyma na stemmerové větvi dnes nefungují.
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

    cfg = get_active_setting(None)["config"]          # stoplist SE aplikuje, viz docstring
    conn = psycopg2.connect(host=os.getenv("PG_HOST", "postgres"), dbname=os.getenv("PG_DB", "edu_ai"),
                            user=os.getenv("PG_USER", "edu_ai_admin"), password=os.getenv("PG_PASSWORD", "edu_ai"))
    cur = conn.cursor()
    cur.execute("SELECT id, term, lang, term_norm, term_lemma, term_stem "
                "FROM edu_ai.qa_synonym WHERE is_active ORDER BY lang, term")
    rows = cur.fetchall()

    zmeny = []
    for _id, term, lang, n0, l0, s0 in rows:
        f = build_forms(term, cfg, lang)
        n, l, s = (" ".join(f["norm"]), " ".join(f["lemma"]), " ".join(f["stem"]))
        if (n, l, s) != (n0, l0, s0):
            zmeny.append((_id, term, lang, (n0, l0, s0), (n, l, s)))

    print(f"{len(rows)} aktivních řádků, {len(zmeny)} k opravě\n")
    for _id, term, lang, old, new in zmeny:
        print(f"  {lang}  {term}")
        for k, o, x in zip(("norm ", "lemma", "stem "), old, new):
            if o != x:
                print(f"        {k}  {o!r} -> {x!r}")

    if not a.apply:
        print("\n(bez --apply se nic nezapsalo)")
        return
    for _id, term, lang, old, new in zmeny:
        cur.execute("UPDATE edu_ai.qa_synonym SET term_norm=%s, term_lemma=%s, term_stem=%s "
                    "WHERE id=%s", (*new, _id))
    conn.commit()
    print(f"\nopraveno {len(zmeny)} řádků")


if __name__ == "__main__":
    main()
