#!/usr/bin/env python3
# last change in code by Martin Safranek (2026-09-05).
"""Fill edu_ai.qa_stopword from a CSV and suggest which words are safe for the stemmer.

    docker exec -i -e PG_PASSWORD=… edu_ai_search python - < load_stopwords_csv.py \
        --csv /tmp/stopwords.csv [--apply]

Without --apply it only computes and prints what it would do.

REDUCTION. The three forms are computed with THE SAME functions search uses
(eduai_normalize), with a cfg assembled from two sources — the stoplist and
settings.config.engines — exactly as ChunkLemmatizer does at ingest. Two deviations,
both necessary:

  * drop_stopwords=False. If the stoplist were applied, a stopword would filter
    itself out and all three columns would come out empty.
  * reduction PER WORD, joined with a space. fold() normalises a single word and
    strips spaces, so "lyžařský výcvik" would be glued into "lyzarskyvycvik".

FLAGS. use_norm and use_lemma are set TRUE — on these two forms the stoplist works
reliably, because the reduction keeps the form of function words. use_stem is FALSE
and the script only suggests candidates for it; switching it on is a deliberate decision.

THE STEMMER ALGORITHM. The question is not "is it a stopword" but "does its stem
steal some content word". Steps:

  1. S = stem of the term T.
  2. Take every word W from the dictionary of that language whose stem is also S.
  3. Split W by lemma:
       lemma(w) == lemma(T)  -> forms of the same word, harmless
       lemma(w) != lemma(T)  -> a FOREIGN word with the same stem = a collision
  4. No foreign word            -> SAFE, use_stem = TRUE is suggested.
  5. A foreign word exists      -> check whether that stem occurs in the corpus at all
     (chunk_stem_text, qa_variants.stem_text) other than for this term:
       it occurs        -> UNSAFE, the collision is live
       it does not      -> BORDERLINE, a theoretical collision; a person decides
  6. Safeguard: a stem shorter than 3 characters is always unsafe — too generic.

Step 5 exists because a collision with a word nobody ever writes is only
theoretical. "během"->beh collides with "běh" and that is live; a collision with a
literary expression absent from the corpus would block a useful stopword for nothing.
"""
import argparse, csv, os, sys, collections
import psycopg2, simplemma
from eduai_normalize import fold, tokenize, stem_min_len, engine_name
from eduai_normalize.lemmatizer import lemma_word
from eduai_normalize.stemmer import stem_word


def load_cfg(cur):
    """The same cfg ChunkLemmatizer builds — stoplist + engines, only without the drop."""
    cur.execute("SELECT lang, term_norm FROM edu_ai.qa_stopword "
                "WHERE is_active AND scope = 'index' AND term_norm <> ''")
    by = collections.defaultdict(list)
    for lang, t in cur.fetchall():
        by[lang].append(t)
    cur.execute("SELECT config->'engines' FROM edu_ai.settings WHERE is_active LIMIT 1")
    row = cur.fetchone()
    return {"normalization": {"stopwords_by_lang": dict(by), "drop_stopwords": False},
            "engines": (row[0] if row else []) or []}


def forms(term, lang, cfg):
    ml = stem_min_len(cfg, lang)
    eng = engine_name(cfg, "stemmer", lang)
    ws = tokenize(term)
    return (" ".join(fold(w) for w in ws),
            " ".join(fold(lemma_word(w, lang)) for w in ws),
            " ".join(fold(stem_word(w, ml, lang, eng)) for w in ws))


def stem_index(lang, cfg):
    """stem -> {word: lemma} over the whole dictionary of the language. Built once."""
    try:
        from simplemma import strategies
        d = strategies.dictionaries.DefaultDictionaryFactory().get_dictionary(lang)
    except Exception:
        return {}
    ml = stem_min_len(cfg, lang)
    eng = engine_name(cfg, "stemmer", lang)
    idx = collections.defaultdict(dict)
    for w in d.keys():
        w = (w.decode() if isinstance(w, bytes) else w).lower()
        if not w.isalpha() or len(w) < 2:
            continue
        idx[fold(stem_word(w, ml, lang, eng))][w] = fold(lemma_word(w, lang))
    return idx


def corpus_stems(cur):
    """Which stems occur in the corpus at all (chunks + QA variants)."""
    seen = set()
    for q in ("SELECT chunk_stem_text FROM edu_ai.document_chunks WHERE chunk_stem_text <> ''",
              "SELECT stem_text FROM edu_ai.qa_variants WHERE stem_text <> ''"):
        try:
            cur.execute(q)
            for (txt,) in cur.fetchall():
                seen.update((txt or "").split())
        except Exception as e:
            print(f"[WARNING] the corpus could not be read ({e}) — step 5 is skipped", file=sys.stderr)
    return seen


def advise(term, lang, f_lemma, f_stem, idx, corpus):
    """(suggested_use_stem, verdict, reason) following the algorithm in the docstring."""
    if not f_stem:
        return False, "UNSAFE", "empty stem"
    if len(f_stem) < 3:
        return False, "UNSAFE", f"stem {f_stem!r} is shorter than 3 characters — too generic"
    foreign = {w: lem for w, lem in idx.get(f_stem, {}).items() if lem != f_lemma}
    if not foreign:
        return True, "SAFE", f"stem {f_stem!r} is shared with no other word"
    sample = ", ".join(sorted(foreign)[:5])
    if f_stem in corpus:
        return False, "UNSAFE", f"stem {f_stem!r} is in the corpus and shared by: {sample}"
    return False, "BORDERLINE", f"stem {f_stem!r} is shared by {sample}, but not in the corpus"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    conn = psycopg2.connect(host=os.getenv("PG_HOST", "postgres"), dbname=os.getenv("PG_DB", "edu_ai"),
                            user=os.getenv("PG_USER", "edu_ai_admin"), password=os.getenv("PG_PASSWORD", "edu_ai"))
    cur = conn.cursor()
    cfg = load_cfg(cur)
    corpus = corpus_stems(cur)
    print(f"corpus: {len(corpus)} distinct stems", file=sys.stderr)

    rows = list(csv.DictReader(open(a.csv, encoding="utf-8-sig"), delimiter=";"))
    idxs, out = {}, []
    for r in rows:
        term, lang = (r["term"] or "").strip(), (r["lang"] or "cs").strip()
        if not term:
            continue
        if lang not in idxs:
            print(f"building the stem index for {lang}…", file=sys.stderr)
            idxs[lang] = stem_index(lang, cfg)
        n, l, s = forms(term, lang, cfg)
        use_stem, verdict, reason = advise(term, lang, l, s, idxs[lang], corpus)
        out.append({"term": term, "lang": lang, "scope": (r.get("scope") or "index").strip(),
                    "is_active": (r.get("is_active", "TRUE") or "TRUE").upper() != "FALSE",
                    "note": r.get("note") or None, "norm": n, "lemma": l, "stem": s,
                    "suggested_use_stem": use_stem, "verdict": verdict, "reason": reason})

    c = collections.Counter(x["verdict"] for x in out)
    print(f"\n{len(out)} terms — stemmer suggestion: {dict(c)}")
    print("\nSAFE (use_stem = TRUE is suggested):")
    for x in out:
        if x["verdict"] == "SAFE":
            print(f"   {x['term']:<16} {x['lang']}  stem={x['stem']}")

    if not a.apply:
        print("\n(nothing was written without --apply)")
        return

    cur.execute("UPDATE edu_ai.qa_stopword SET is_active = false")
    print(f"\nall {cur.rowcount} existing rows set to is_active = false")
    ins = 0
    for x in out:
        cur.execute(
            "INSERT INTO edu_ai.qa_stopword "
            "(term, lang, scope, is_active, note, term_norm, term_lemma, term_stem, "
            " use_norm, use_lemma, use_stem) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,true,true,%s) "
            "ON CONFLICT (lang, term, scope) DO UPDATE SET "
            "  is_active=EXCLUDED.is_active, note=EXCLUDED.note, "
            "  term_norm=EXCLUDED.term_norm, term_lemma=EXCLUDED.term_lemma, "
            "  term_stem=EXCLUDED.term_stem, use_norm=true, use_lemma=true, "
            "  use_stem=EXCLUDED.use_stem",
            (x["term"], x["lang"], x["scope"], x["is_active"], x["note"],
             x["norm"], x["lemma"], x["stem"], False))
        ins += 1
    conn.commit()
    print(f"wrote {ins} rows (use_norm=TRUE, use_lemma=TRUE, use_stem=FALSE)")


if __name__ == "__main__":
    main()
