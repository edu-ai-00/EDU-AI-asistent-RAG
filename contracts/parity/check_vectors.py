# last change in code by Martin Safranek (2026-08-02).
"""Assert the live normalizer still reproduces the COMMITTED parity vectors.

This is what turns the vectors from a snapshot into a guard. Refactoring the
reduction code (the language branches in stemmer.py / normalize.py, the
`lang` defaults, moving reducer selection into settings.engines) rewrites
the code that produced every token now stored in document_chunks and qa_variants.
If the output moves even by one character, the stored index no longer matches
what a query will build, and the only remedy is re-ingesting everything.

So: run this BEFORE a refactor (it must pass — that proves the committed vectors
describe today's behaviour) and AFTER it (it must still pass — that proves the
refactor was behaviour-neutral and no re-ingest is needed).

    docker cp contracts/parity/. edu_ai_search:/tmp/parity/
    docker exec -i -w /tmp/parity -e PYTHONPATH=/app edu_ai_search \
      python - /tmp/parity \
      < contracts/parity/check_vectors.py

The directory argument is required when piping via stdin: `__file__` is not a
real path then, so the script cannot find the vectors next to itself. The
working directory also makes stoplist.json available to parity_cfg(), and
PYTHONPATH keeps app importable. /app is read-only for the runtime user, hence /tmp.

Exits non-zero on any drift and prints the exact input and form that moved.
Reads every <lang>-normalization-vectors.json in that directory, so a new
language is covered as soon as its vectors are generated.
"""

import glob
import json
import os
import sys
from app.text_norm import build_forms

FORMS = ("norm", "lemma", "stem")



# The stoplist and the engines are DATA, not code: eduai_normalize 3.0.0 has no
# built-in tables and stopwords_of()/engine_name() raise without them. These vectors stay deliberately pinned to
# LIBRARY behaviour, not to whatever edu_ai.qa_stopword holds today, so they read
# the snapshot in stoplist.json next to them — no database, still deterministic.
def parity_cfg() -> dict:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "stoplist.json"), encoding="utf-8") as fh:
        _d = json.load(fh)
        return {"normalization": _d["normalization"], "engines": _d["engines"]}


def vectors_dir() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:                      # piped via stdin
        return "."


def main() -> int:
    HERE = vectors_dir()
    files = sorted(glob.glob(os.path.join(HERE, "*-normalization-vectors.json")))
    if not files:
        print(f"FAIL: no vector files found in {HERE}", file=sys.stderr)
        return 2

    drift = 0
    checked = 0
    for path in files:
        doc = json.load(open(path, encoding="utf-8"))
        # older files (schema_version 1, cs) predate the explicit language key
        lang = doc.get("language") or os.path.basename(path).split("-")[0]
        for vec in doc["vectors"]:
            checked += 1
            got = build_forms(vec["input"], parity_cfg(), lang)
            for form in FORMS:
                if got[form] != vec[form]:
                    drift += 1
                    print(f"DRIFT [{lang}] {vec['input']!r} .{form}\n"
                          f"   committed: {vec[form]}\n"
                          f"   live     : {got[form]}", file=sys.stderr)
        print(f"  {os.path.basename(path)}: {len(doc['vectors'])} vectors, lang={lang}")

    if drift:
        print(f"\nFAIL: {drift} form(s) moved across {checked} vectors — the stored "
              f"index no longer matches what a query would build. Either revert the "
              f"change or re-ingest every affected language.", file=sys.stderr)
        return 1
    print(f"\nOK: {checked} vectors reproduce exactly — reduction behaviour unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
