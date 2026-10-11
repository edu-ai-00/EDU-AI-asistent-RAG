# last change in code by Jan Drozd (2026-09-03).
"""Unit tests for language detection on ALL-CAPS and short text.

Pure: no stack, no DB. Needs eduai_normalize importable — run it inside either
image, since the package is installed in both:

    docker exec -i edu_ai_search python - < tests/test_langdetect_caps.py

The test_* functions are also pytest-compatible.

Why this file exists: a Czech heading in capitals was detected as GERMAN, so the
ingest reduced it with the German stemmer and kept the Czech stopwords — and a
Czech query could then never meet it on the lemma form. Found on production
2026-09-03 (5 active chunks, e.g. "HODNOCENÍ NA VYSVĚDČENÍCH Z PŘEDCHOZÍHO
VZDĚLÁVÁNÍ"). The stored langdetect_result_score cannot catch it: all of them
carried 0.8, exactly like correctly detected Czech.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eduai_normalize import detect_lang  # noqa: E402
from eduai_normalize.normalize import _LANGDETECT_LOWER_BELOW  # noqa: E402

ALLOWED = ("cs", "de", "en", "es")


def test_czech_allcaps_headings_are_czech():
    """The production cases that were reduced with the German stemmer."""
    for text in ("HODNOCENÍ NA VYSVĚDČENÍCH Z PŘEDCHOZÍHO VZDĚLÁVÁNÍ",
                 "HODNOCENÍ NA VYSVĚDČENÍ PŘEVÁDĚJÍCÍ SLOVNÍ HODNOCENÍ DO KLASIFIKACE",
                 "ELEKTRONICKÉ PODÁNÍ BEZ OHLEDU NA ZPŮSOB PODÁNÍ"):
        assert detect_lang(text, allowed=ALLOWED) == "cs", f"expected cs: {text!r}"


def test_mixed_case_czech_still_czech():
    """The fix must not disturb ordinary sentence-case text."""
    for text in ("Kdy se konají řádné termíny jednotné přijímací zkoušky?",
                 "Přineste pozvánku a modře píšící propisku."):
        assert detect_lang(text, allowed=ALLOWED) == "cs", f"expected cs: {text!r}"


def test_long_text_is_not_lowercased():
    """Lowercasing UNCONDITIONALLY moved 2 English passages to German.

    The bound is the whole point of the fix, so assert the bound itself rather
    than trusting that some sample happens to be long enough.
    """
    assert _LANGDETECT_LOWER_BELOW == 200


def test_english_is_never_dragged_elsewhere():
    """English must come back as en or as an abstention — never another language.

    None is the SAFE outcome: it triggers the documented fallback chain and is
    recorded in langdetect_result_score, whereas a confident wrong call silently
    picks the wrong stemmer. "SCHOOL ADMISSION REQUIREMENTS" abstains (too short
    to clear _LANGDETECT_MIN_PROB), so asserting en there would assert a
    behaviour the detector does not promise.
    """
    for text in ("SCHOOL ADMISSION REQUIREMENTS",
                 "SECONDARY SCHOOL ADMISSION REQUIREMENTS FOR APPLICANTS",
                 "The school publishes its criteria in advance."):
        got = detect_lang(text, allowed=ALLOWED)
        assert got in ("en", None), f"expected en or None, got {got!r}: {text!r}"


TESTS = {
    "caps": test_czech_allcaps_headings_are_czech,
    "mixed": test_mixed_case_czech_still_czech,
    "bound": test_long_text_is_not_lowercased,
    "english": test_english_is_never_dragged_elsewhere,
}


def main(argv):
    keys = argv or list(TESTS)
    results = []
    for key in keys:
        fn = TESTS.get(key)
        if not fn:
            print(f"?? unknown test '{key}' (have: {', '.join(TESTS)})")
            continue
        print(f"\n> {key}: {fn.__doc__.splitlines()[0]}")
        try:
            fn()
            print(f"PASS {key}")
            results.append((key, True))
        except AssertionError as e:
            print(f"FAIL {key}: {e}")
            results.append((key, False))
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {key}: {type(e).__name__}: {e}")
            results.append((key, False))
    ok = sum(1 for _, p in results if p)
    print(f"\n==== {ok}/{len(results)} passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
