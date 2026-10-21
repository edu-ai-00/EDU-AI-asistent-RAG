"""Machine view: the norm / lemma / stem forms the search index would store.

Runs the SAME `eduai_normalize` code as the search service and the ingestion
flow (it is pip-installed in the edu_ai_search image this tool runs in), so
what the curator sees here is what retrieval matches on. The stoplist and the
engines come from contracts/parity/stoplist.json — the snapshot the parity
vectors use — because this lab tool has no database connection. It is
therefore the LIBRARY behaviour, which today equals the qa_stopword číselník
for Czech; a curated stoplist change in the database would not show here.

Outside that image the module degrades: `available()` is False and the UI
hides the machine view instead of failing.
"""

from __future__ import annotations

import json
import re

try:
    from eduai_normalize import build_forms, fold, tokenize
    from eduai_normalize.lemmatizer import lemma_word
    _OK = True
except ImportError:          # host run without the search image
    _OK = False

_CFG: dict | None = None
_URL = re.compile(r"https?://\S+")
_DIACRITIC = re.compile(r"[áčďéěíňóřšťúůýž]", re.I)


def available() -> bool:
    return _OK and _CFG is not None


def configure(stoplist_path: str) -> None:
    global _CFG
    with open(stoplist_path, encoding="utf-8") as fh:
        d = json.load(fh)
    _CFG = {"normalization": d["normalization"], "engines": d["engines"]}


def _known(word: str) -> bool:
    try:
        from simplemma import is_known
        return is_known(word.lower(), lang="cs")
    except Exception:
        return True


def analyse(text: str, lang: str = "cs") -> dict | None:
    """{norm, lemma, stem, dropped, warnings[]} for one question or answer."""
    if not available():
        return None
    forms = build_forms(text, _CFG, lang)
    words = tokenize(text)
    kept = set(forms["norm"])
    dropped = [w for w in words if fold(w) and fold(w) not in kept]
    warnings = []

    # 1. negation the lemma drops ("nesmím" -> smět) — retrieval then matches
    #    the opposite meaning
    lost = []
    for w in words:
        surface = fold(w)
        # "nej-" is the superlative (nejdříve -> brzy), not a negation
        if len(surface) > 3 and surface.startswith("ne") and not surface.startswith("nej"):
            lem = fold(lemma_word(w, lang))
            if lem and not lem.startswith("ne") and lem != surface:
                lost.append(f"{w} → {lem}")
    if lost:
        warnings.append({"code": "zapor", "note": "Zápor se v lemmatu ztratí: " + ", ".join(lost)})

    # 2. the stoplist ate (almost) everything ("jak rozlišit mě a mně")
    content = [t for t in forms["lemma"] if not t.isdigit()]
    if len(words) >= 3 and len(content) <= 1:
        warnings.append({"code": "stop_slova",
                         "note": "Po odstranění stop-slov zbude jen: " + (", ".join(content) or "nic")})

    # 3. text typed without diacritics: the lemma dictionary needs them
    # web addresses are not words; drop them before judging the vocabulary
    letters = [w for w in tokenize(_URL.sub(" ", text)) if w.isalpha() and len(w) > 2]
    no_diacritics = len(letters) >= 3 and not _DIACRITIC.search(text)
    if no_diacritics:
        warnings.append({"code": "diakritika",
                         "note": "Text je bez diakritiky — lemmatizace slova nepozná."})

    # 4. words the lemmatizer left as they are AND the dictionary does not know:
    #    a typo, jargon, or a form simplemma lacks ("přihlášce" stays "přihlášce"
    #    instead of "přihláška"). Skipped on diacritics-less text — warning 3
    #    already explains every word there.
    if not no_diacritics:
        unknown = sorted({w for w in letters
                          if w.lower() == w          # names and acronyms (DiPSy, Cermat) skipped
                          and lemma_word(w, lang).lower() == w.lower()
                          and not _known(w)})
        if unknown:
            warnings.append({"code": "nezname",
                             "note": "Lemmatizace slovo nepřevede (slovník ho nezná): " + ", ".join(unknown)})

    return {"norm": forms["norm"], "lemma": forms["lemma"], "stem": forms["stem"],
            "dropped": dropped, "warnings": warnings}
