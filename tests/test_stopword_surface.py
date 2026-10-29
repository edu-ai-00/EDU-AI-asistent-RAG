"""Unit tests: a stop-word is judged by its SURFACE form before reduction.

Pure: no stack, no DB. Run inside the search image (simplemma lives there):

    docker exec -i edu_ai_search python - < tests/test_stopword_surface.py

Why: the lemma and stem branches compared the stoplist with REDUCED tokens, so
44 of the 155 Czech index stop-words leaked (jsou -> stem `jso`, budou -> `bud`)
and some collided with content words ("během" -> `beh` as "běh", "této" ->
lemma `teta`). eduai_normalize 3.2.0 drops them by the surface form first.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eduai_normalize import build_forms  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(_ROOT, "contracts", "parity", "stoplist.json"), encoding="utf-8") as _fh:
    _d = json.load(_fh)
CFG = {"normalization": _d["normalization"], "engines": _d["engines"]}


def _f(text, lang="cs"):
    return build_forms(text, CFG, lang)


def test_leaking_stopwords_dropped_in_every_form():
    for text, gone in (("Kdy jsou přijímačky?", {"jso", "jsou", "byt"}),
                       ("Kdy budou známky?", {"bud", "budou"}),
                       ("Moje dcera", {"moj", "moje", "muj"}),
                       ("Je mezi testy přestávka?", {"meh", "mezi"})):
        f = _f(text)
        for form in ("norm", "lemma", "stem"):
            assert not (set(f[form]) & gone), (text, form, f[form])


def test_content_word_with_a_stopword_stem_kept():
    """"běh" shares the stem `beh` with the stop-word "během"; only the latter goes."""
    f = _f("Během přestávky je běh")
    assert f["lemma"].count("beh") == 1 and f["stem"].count("beh") == 1, f


def test_teta_only_from_teta():
    assert "teta" not in _f("u této školy")["lemma"]
    assert "teta" in _f("u této tety")["lemma"]


def test_negation_still_kept():
    assert _f("Co nesmím mít?")["lemma"] == ["co", "nesmet", "mit"]


def test_english_too():
    assert "doe" not in _f("When does the school year start?", "en")["stem"]


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    print("FAILED" if fails else "OK")
    sys.exit(1 if fails else 0)
