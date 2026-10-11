#!/usr/bin/env python3
# last change in code by Martin Safranek (2026-09-05).
"""Vyexportuje edu_ai.qa_stopword do CSV, které bere load_stopwords_csv.py.

    docker exec -i edu_ai_search python - < tools/stopwords/export_stopwords_csv.py > stopwords.csv

`term` se přitom vrací do ZÁKLADNÍHO TVARU S DIAKRITIKOU. Dnes je uložený
složeně (`behem`, `kdyz`), což je pro dotazovou stranu náhodou správně — porovnává
se proti už redukovaným tokenům — ale odvodit z toho lemma nebo stem nejde:
simplemma zná `během`, ne `behem`, takže 28 % záznamů dostane lemma, které je jen
echo vstupu. Diakritika se doplňuje jen tam, kde je JEDNOZNAČNÁ: složený tvar sám
není platné slovo daného jazyka a ve slovníku k němu existuje právě jeden tvar
s diakritikou. Víceznačné (`me` → mé/mě) zůstávají nezměněné a sloupec
`pozn_diakritika` je označí — hádat mezi „mě" a „mé" znamená stopslovo, které
přestane chytat.

Formát je záměrně tentýž jako u /v1/qa-export: ';', QUOTE_MINIMAL, utf-8-sig,
aby se soubor otevřel v Excelu stejně jako stažený QA pack.
"""
import csv, io, os, sys
import psycopg2, simplemma
from eduai_normalize import strip_diacritics

DIA = "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽäöüßÄÖÜñáéíóúÑÁÉÍÓÚ"

def reverse_map(lang):
    """složený tvar -> {tvary s diakritikou}, ze slovníku daného jazyka."""
    try:
        from simplemma import strategies
        d = strategies.dictionaries.DefaultDictionaryFactory().get_dictionary(lang)
    except Exception:
        return {}
    rev = {}
    for w in d.keys():
        w = (w.decode() if isinstance(w, bytes) else w).lower()
        if any(c in w for c in DIA):
            rev.setdefault(strip_diacritics(w), set()).add(w)
    return rev

def main():
    conn = psycopg2.connect(host=os.getenv("PG_HOST", "postgres"), dbname="edu_ai",
                            user=os.getenv("PG_USER", "edu_ai_admin"), password=os.getenv("PG_PASSWORD", "edu_ai"))
    cur = conn.cursor()
    cur.execute("SELECT term, lang, scope, is_active, coalesce(note,'') "
                "FROM edu_ai.qa_stopword ORDER BY lang, scope, term")
    rows = cur.fetchall()
    revs, out = {}, []
    for term, lang, scope, active, note in rows:
        if lang not in revs:
            revs[lang] = reverse_map(lang)
        novy, pozn = term, ""
        if any(c in term for c in DIA):
            pozn = "už má diakritiku"
        elif simplemma.is_known(term, lang):
            pozn = "složený tvar je sám platné slovo — nechat"
        else:
            kand = sorted(revs[lang].get(term, ()))
            if len(kand) == 1:
                novy, pozn = kand[0], f"doplněno automaticky z {term!r}"
            elif len(kand) > 1:
                pozn = "VÍCEZNAČNÉ, rozhodne člověk: " + ", ".join(kand)
            else:
                pozn = "slovník nezná ani jeden tvar — zkontrolovat ručně"
        out.append([novy, lang, scope, "TRUE" if active else "FALSE", note, pozn])

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow(["term", "lang", "scope", "is_active", "note", "pozn_diakritika"])
    w.writerows(out)
    sys.stdout.buffer.write(buf.getvalue().encode("utf-8-sig"))

if __name__ == "__main__":
    main()
