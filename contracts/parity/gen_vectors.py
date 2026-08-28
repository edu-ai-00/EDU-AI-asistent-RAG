# last change in code by Martin Safranek (2026-08-02).
"""Regenerate <lang>-normalization-vectors.json from the live Python normalizer.

This is the canonical source of the parity vectors: it runs the SAME
`search/app/text_norm.build_forms` the retrieval pipeline uses, so the committed
vectors always reflect real server behavior. The Dart offline engine must
reproduce the same outputs (see README.md in this directory).

Run it inside the search container (the app package + simplemma dicts live
there), piping this file in via stdin, with the language as the argument:

    docker exec -i edu_ai_search python - cs \
      < contracts/parity/gen_vectors.py \
      > contracts/parity/cs-normalization-vectors.json

cfg is None → the settings v1 defaults (lowercase + strip_diacritics +
drop_stopwords) and the library's BUILT-IN stoplist rather than the qa_stopword
číselník. That is deliberate: these vectors pin the LIBRARY's behaviour, so they
remain a valid guard for a code refactor even when the číselník changes.

Inputs are synthetic edge cases only — no learner content, no real school names.
"""

import json
import sys
from app.text_norm import build_forms

LANGS = {
    "cs": {
        "label": "CZ",
        "cases": [
            "Kdy začíná školní rok?",
            "Přijímačky na střední školu",
            "PŘIHLÁŠKA",
            "matematika a český jazyk",
            "termín 1. září 2025",
            "česko-slovenský slovník",
            "Jak se přihlásit?",
            "vysvědčení",
            "Co je to JPZ?",
            "škola",
            "do školy",
            "2. kolo přijímaček",
            "němčina",
        ],
    },
    "en": {
        "label": "EN",
        # Mirrors the Czech coverage where it transfers (case folding,
        # numerals/dates, hyphenation, stopword dropping, one word family) and
        # adds what only English has: plural/possessive 's', -ing/-ed/-ly, and
        # the Snowball cases whose stem is NOT a word ("necessari", "studi").
        # Those are exactly the outputs a refactor could quietly move.
        "cases": [
            "When does the school year start?",
            "Admission requirements for secondary schools",
            "APPLICATION",
            "mathematics and the English language",
            "deadline 1 September 2025",
            "state-wide entrance examination",
            "How do I apply?",
            "report card",
            "What is a standards-based assessment?",
            "school",
            "to the school",
            "the second round of admissions",
            "German",
            "students' adaptations were necessary",
            "studying, studied, studies",
            "running quickly toward graduation",
        ],
    },
}


def main() -> None:
    lang = (sys.argv[1] if len(sys.argv) > 1 else "cs").strip().lower()
    if lang not in LANGS:
        raise SystemExit(f"unknown language {lang!r}; known: {sorted(LANGS)}")
    spec = LANGS[lang]
    out = {
        "schema_version": 1,
        "language": lang,
        "generator": "search/app/text_norm.build_forms",
        "config": "null (settings v1 defaults: lowercase + strip_diacritics + drop_stopwords)",
        "note": (
            f"Canonical {spec['label']} normalization/lemma/stem parity vectors. "
            "The Python server and the Dart offline app MUST both reproduce these "
            "outputs (query<->index parity). Regenerate with "
            "contracts/parity/gen_vectors.py."
        ),
        "vectors": [],
    }
    for c in spec["cases"]:
        f = build_forms(c, None, lang)
        out["vectors"].append(
            {"input": c, "norm": f["norm"], "lemma": f["lemma"], "stem": f["stem"]}
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
