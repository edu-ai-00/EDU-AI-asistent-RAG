#!/usr/bin/env python3
# last change in code by Martin Safranek (2026-09-05).
"""Export edu_ai.qa_stopword to the CSV that load_stopwords_csv.py reads.

    docker exec -i edu_ai_search python - < tools/stopwords/export_stopwords_csv.py > stopwords.csv

`term` is returned to its BASE FORM WITH DIACRITICS on the way. Today it is stored
folded (`behem`, `kdyz`), which happens to be right for the query side — it is
compared with tokens that are already reduced — but a lemma or a stem cannot be
derived from it: simplemma knows `během`, not `behem`, so 28 % of the records get a
lemma that is only an echo of the input. Diacritics are restored only where they are
UNAMBIGUOUS: the folded form is not itself a valid word of that language and the
dictionary has exactly one form with diacritics for it. Ambiguous ones (`me` → mé/mě)
stay unchanged and the `diacritics_note` column marks them — guessing between "mě"
and "mé" means a stopword that stops matching.

The format is deliberately the same as /v1/qa-export: ';', QUOTE_MINIMAL, utf-8-sig,
so the file opens in Excel the same way as a downloaded QA pack.
"""
import csv, io, os, sys
import psycopg2, simplemma
from eduai_normalize import strip_diacritics

DIA = "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽäöüßÄÖÜñáéíóúÑÁÉÍÓÚ"

def reverse_map(lang):
    """folded form -> {forms with diacritics}, from the dictionary of that language."""
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
        restored, remark = term, ""
        if any(c in term for c in DIA):
            remark = "already has diacritics"
        elif simplemma.is_known(term, lang):
            remark = "the folded form is a valid word itself — keep"
        else:
            candidates = sorted(revs[lang].get(term, ()))
            if len(candidates) == 1:
                restored, remark = candidates[0], f"restored automatically from {term!r}"
            elif len(candidates) > 1:
                remark = "AMBIGUOUS, a person decides: " + ", ".join(candidates)
            else:
                remark = "the dictionary knows no form — check by hand"
        out.append([restored, lang, scope, "TRUE" if active else "FALSE", note, remark])

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow(["term", "lang", "scope", "is_active", "note", "diacritics_note"])
    w.writerows(out)
    sys.stdout.buffer.write(buf.getvalue().encode("utf-8-sig"))

if __name__ == "__main__":
    main()
