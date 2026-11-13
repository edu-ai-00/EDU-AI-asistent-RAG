"""Unit tests: Czech typed WITHOUT diacritics is lemmatized and detected.

Pure: no stack, no DB. Run inside the search image (simplemma lives there):

    docker exec -i edu_ai_search python - < tests/test_diacritics_restore.py

Why: Czech typed without diacritics was often not detected as Czech and
"zacina" stayed `zacina`; eduai_normalize 3.3.0 looks up its spellings with
diacritics.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eduai_normalize import detect_lang, diacritic_candidates, fold, lemma_word  # noqa: E402

LANGS = ("cs", "en", "de", "es")


def _lem(w):
    return fold(lemma_word(w, "cs"))


def test_restored_when_every_form_agrees():
    for typed, lemma in (("zacina", "zacinat"), ("pise", "psat"), ("konaji", "konat"),
                         ("prihlasit", "prihlasit"), ("novy", "novy")):
        assert _lem(typed) == lemma, (typed, _lem(typed))


def test_negation_survives_restoration():
    assert _lem("nesmim") == "nesmet"


def test_left_alone():
    assert diacritic_candidates("muze", "cs") == ("muže", "může")
    assert _lem("muze") == "muze"                  # muž / moci disagree
    assert diacritic_candidates("rada", "cs") == ()  # a word of its own
    assert diacritic_candidates("jpz", "cs") == ()   # unknown
    assert diacritic_candidates("škola", "cs") == ()  # has diacritics already
    assert diacritic_candidates("zacina", "en") == ()  # Czech only


def test_text_with_diacritics_unchanged():
    assert _lem("začíná") == "zacinat" and _lem("nesmím") == "nesmet"


def test_long_word_capped():
    assert diacritic_candidates("nejpravdepodobnejsi", "cs") == ()


def test_detected_as_czech():
    for q in ("co se deje pred zahajenim testu jpz?", "jak se hlasit na stredni skolu?",
              "kdy prijde pozvanka k jpz?", "musim ucast na jpz potvrdit?"):
        assert detect_lang(q, allowed=LANGS) == "cs", q


def test_english_still_english():
    assert detect_lang("When does the school year start?", allowed=LANGS) == "en"


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
