#!/usr/bin/env python3
# last change in code by Martin Safranek (2026-09-05).
"""Naplní edu_ai.qa_stopword z CSV a navrhne, která slova jsou bezpečná pro stemmer.

    docker exec -i -e PG_PASSWORD=… edu_ai_search python - < load_stopwords_csv.py \
        --csv /tmp/stopwords.csv [--apply]

Bez --apply jen počítá a vypíše, co by udělal.

REDUKCE. Tři tvary se počítají TÝMIŽ funkcemi, které používá vyhledávání
(eduai_normalize), s cfg složeným ze dvou zdrojů — stoplistu a settings.config.engines
— přesně jako to dělá ChunkLemmatizer při ingestu. Dvě odchylky, obě nutné:

  * drop_stopwords=False. Kdyby se stoplist aplikoval, stopslovo by samo sebe
    odfiltrovalo a všechny tři sloupce by vyšly prázdné.
  * redukce PO SLOVECH, spojená mezerou. fold() je normalizér jednoho slova a
    strhne mezery, takže „lyžařský výcvik" by se slepilo na „lyzarskyvycvik".

PŘÍZNAKY. use_norm a use_lemma se plní TRUE — na těchto dvou tvarech stoplist
funguje spolehlivě, protože redukce zachovává tvar funkčních slov. use_stem je
FALSE a skript k němu jen navrhne kandidáty; zapnutí je vědomé rozhodnutí.

ALGORITMUS PRO STEMMER. Otázka nezní „je to stopslovo", ale „ukradne jeho stem
nějaké obsahové slovo". Postup:

  1. S = stem termu T.
  2. Ze slovníku daného jazyka se vezmou všechna slova W, jejichž stem je také S.
  3. W se rozdělí podle lemmatu:
       lemma(w) == lemma(T)  -> vlastní tvary téhož slova, neškodí
       lemma(w) != lemma(T)  -> CIZÍ slovo se stejným stemem = kolize
  4. Žádné cizí slovo            -> BEZPEČNÉ, navrhne se use_stem = TRUE.
  5. Cizí slovo existuje         -> ověří se, jestli se v korpusu (chunk_stem_text,
     qa_variants.stem_text) ten stem vůbec vyskytuje jinde než u tohoto termu:
       vyskytuje se  -> NEBEZPEČNÉ, kolize je živá
       nevyskytuje   -> HRANIČNÍ, teoretická kolize; rozhodne člověk
  6. Pojistka: stem kratší než 3 znaky je nebezpečný vždy — je příliš obecný.

Krok 5 je tam proto, že kolize se slovem, které nikdo nikdy nenapíše, je jen
teoretická. „během"→beh koliduje s „běh" a to je živé; kolize s knižním výrazem,
který v korpusu není, by zbytečně zablokovala užitečné stopslovo.
"""
import argparse, csv, os, sys, collections
import psycopg2, simplemma
from eduai_normalize import fold, tokenize, stem_min_len, engine_name
from eduai_normalize.lemmatizer import lemma_word
from eduai_normalize.stemmer import stem_word


def load_cfg(cur):
    """Tentýž cfg, jaký si staví ChunkLemmatizer — stoplist + enginy, jen bez drop."""
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
    """stem -> {slovo: lemma} nad celým slovníkem jazyka. Postaví se jednou."""
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
    """Které stemy se v korpusu vůbec vyskytují (chunky + QA varianty)."""
    seen = set()
    for q in ("SELECT chunk_stem_text FROM edu_ai.document_chunks WHERE chunk_stem_text <> ''",
              "SELECT stem_text FROM edu_ai.qa_variants WHERE stem_text <> ''"):
        try:
            cur.execute(q)
            for (txt,) in cur.fetchall():
                seen.update((txt or "").split())
        except Exception as e:
            print(f"[VAROVÁNÍ] korpus nešel načíst ({e}) — krok 5 se přeskočí", file=sys.stderr)
    return seen


def advise(term, lang, f_lemma, f_stem, idx, corpus):
    """(navrh_use_stem, verdikt, důvod) podle algoritmu v docstringu."""
    if not f_stem:
        return False, "NEBEZPEČNÉ", "prázdný stem"
    if len(f_stem) < 3:
        return False, "NEBEZPEČNÉ", f"stem {f_stem!r} je kratší než 3 znaky — příliš obecný"
    cizi = {w: lem for w, lem in idx.get(f_stem, {}).items() if lem != f_lemma}
    if not cizi:
        return True, "BEZPEČNÉ", f"stem {f_stem!r} nesdílí žádné jiné slovo"
    ukazka = ", ".join(sorted(cizi)[:5])
    if f_stem in corpus:
        return False, "NEBEZPEČNÉ", f"stem {f_stem!r} je v korpusu a sdílí ho: {ukazka}"
    return False, "HRANIČNÍ", f"stem {f_stem!r} sdílí {ukazka}, ale v korpusu není"


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
    print(f"korpus: {len(corpus)} různých stemů", file=sys.stderr)

    rows = list(csv.DictReader(open(a.csv, encoding="utf-8-sig"), delimiter=";"))
    idxs, out = {}, []
    for r in rows:
        term, lang = (r["term"] or "").strip(), (r["lang"] or "cs").strip()
        if not term:
            continue
        if lang not in idxs:
            print(f"stavím index stemů pro {lang}…", file=sys.stderr)
            idxs[lang] = stem_index(lang, cfg)
        n, l, s = forms(term, lang, cfg)
        use_stem, verdikt, duvod = advise(term, lang, l, s, idxs[lang], corpus)
        out.append({"term": term, "lang": lang, "scope": (r.get("scope") or "index").strip(),
                    "is_active": (r.get("is_active", "TRUE") or "TRUE").upper() != "FALSE",
                    "note": r.get("note") or None, "norm": n, "lemma": l, "stem": s,
                    "navrh_use_stem": use_stem, "verdikt": verdikt, "duvod": duvod})

    c = collections.Counter(x["verdikt"] for x in out)
    print(f"\n{len(out)} termů — návrh pro stemmer: {dict(c)}")
    print("\nBEZPEČNÉ (navrhuje se use_stem = TRUE):")
    for x in out:
        if x["verdikt"] == "BEZPEČNÉ":
            print(f"   {x['term']:<16} {x['lang']}  stem={x['stem']}")

    if not a.apply:
        print("\n(bez --apply se nic nezapsalo)")
        return

    cur.execute("UPDATE edu_ai.qa_stopword SET is_active = false")
    print(f"\nvšech {cur.rowcount} stávajících řádků nastaveno na is_active = false")
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
    print(f"zapsáno {ins} řádků (use_norm=TRUE, use_lemma=TRUE, use_stem=FALSE)")


if __name__ == "__main__":
    main()
