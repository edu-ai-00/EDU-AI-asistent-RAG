"""Unit tests for NEGATION in retrieval: the lemma keeps it, a polarity
conflict withholds a curated answer.

Pure: no stack, no DB. Run inside the search image, where the app package and
simplemma live:

    docker exec -i edu_ai_search python - < tests/test_negation_lemma.py

The test_* functions are also pytest-compatible.

Why: simplemma 2.0.0 reduces "nesmím" and "smím" to the same lemma `smět`, so
a question asked in the negative got the curated answer to the positive one —
measured 2026-10-21 with tools/lab/variants on the JPZ set, 39 of 39 flipped
questions. eduai_normalize 3.1.0 keeps the negation in the lemma, and
qa_common.negation_conflict stops the lexical and vector QA steps from
answering across opposite polarity (the other words still cover 0.65–0.75)
when the step has `negation_conflict` (migration 224). Since 3.4.0 a word is
a negation only when the lemmatizer made it one (eduai_normalize.is_negation).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eduai_normalize import fold, lemma_word, is_negation, negated_lemmas  # noqa: E402
from app.nodes.qa_common import negation_conflict, polarity_tokens  # noqa: E402


def _lem(word: str) -> str:
    return fold(lemma_word(word, "cs"))


def test_negation_kept_in_the_lemma():
    for word, lemma in (("nesmím", "nesmet"), ("nesmí", "nesmet"), ("nejde", "nejit"),
                        ("nechci", "nechtit"), ("nemá", "nemit"), ("nevím", "nevedet"),
                        ("nepřijde", "neprijit"), ("nedostatečný", "nedostatecny")):
        assert _lem(word) == lemma, (word, _lem(word))


def test_positive_unchanged():
    for word, lemma in (("smím", "smet"), ("jde", "jit"), ("má", "mit"), ("přijde", "prijit")):
        assert _lem(word) == lemma, (word, _lem(word))


def test_exception_neni():
    """"ní" is a pronoun, so the prefix test cannot see "není"."""
    assert _lem("není") == "nebyt"


def test_words_that_only_start_with_ne():
    """Superlative nej-, and words the library keeps whole, are not negations."""
    for word, lemma in (("nejlepší", "nejlepsi"), ("nejdřív", "nejdrive"),
                        ("neděle", "nedele"), ("nemocný", "nemocny"), ("nebo", "nebo")):
        assert _lem(word) == lemma, (word, _lem(word))


def test_other_languages_untouched():
    """English has no negation prefix: "needs" keeps its plain lemma."""
    assert lemma_word("needs", "en") == "need"


def test_conflict_both_directions():
    entry_pos = {"co", "delat", "pozvanka", "jpz", "prijde", "prijit", "prijd"}
    entry_neg = {"co", "delat", "pozvanka", "jpz", "neprijde", "neprijit", "neprijd"}
    assert negation_conflict(["co", "delat", "pozvanka", "jpz", "neprijit"], entry_pos, "cs") == ["neprijit"]
    assert negation_conflict(["co", "delat", "pozvanka", "jpz", "prijit"], entry_neg, "cs") == ["prijit"]


def test_same_polarity_no_conflict():
    entry_neg = {"nesmim", "nesmet", "nes", "mit"}
    assert negation_conflict(["nesmet", "mit"], entry_neg, "cs") == []
    assert negation_conflict(["smet", "mit"], {"smim", "smet", "mit"}, "cs") == []


def test_entry_with_both_polarities():
    """The entry's negation is the marked side: "Co mám vzít, když nemám
    občanku?" must not answer "Co když uchazeč MÁ občanský průkaz?"."""
    entry = {"mam", "mit", "nemam", "nemit", "obcanka"}
    assert negation_conflict(["uchazec", "mit", "obcansky"], entry, "cs") == ["mit"]
    assert negation_conflict(["nemit", "obcanka"], entry, "cs") == []


def test_no_prefix_language_no_conflict():
    assert negation_conflict(["nesmet"], {"smet"}, "en") == []
    assert negation_conflict(["nesmet"], {"smet"}, None) == ["nesmet"]   # undecided: every prefix


def test_words_that_only_look_negated_are_no_conflict():
    # folded, "někdy" reads `nekdy` and "nemoc" `nemoc`: neither is a negation
    # the lemmatizer made, so neither may withhold the answer (code review 2026-11-10)
    assert negation_conflict(["kdy", "moci", "odejit", "brzy", "skola"],
                             {"moci", "nekdy", "odejit", "brzy", "skola"}, "cs") == []
    assert negation_conflict(["kde", "najit", "rozvrh"], {"najit", "nekde", "rozvrh"}, "cs") == []
    assert negation_conflict(["moc", "ukol"], {"co", "delat", "nemoc"}, "cs") == []
    for w in ("nekdo", "nekolik", "nejak", "nedele", "nemocny", "nejlepsi"):
        assert negation_conflict([w[2:]], {w}, "cs") == [], w


def test_negations_the_lemmatizer_makes_still_conflict():
    for neg, pos in (("nesmet", "smet"), ("neprijit", "prijit"), ("nemit", "mit"),
                     ("nemoci", "moci"), ("nebyt", "byt"), ("nedostat", "dostat")):
        assert negation_conflict([neg], {pos}, "cs") == [neg], neg
        assert negation_conflict([pos], {neg}, "cs") == [pos], pos
    # the set is the lemmatizer's own: every lemma it negates is in it
    for w in ("nesmím", "nepřijde", "nemám", "nemůžu", "není", "nedostane"):
        assert is_negation(fold(lemma_word(w, "cs")), "cs"), w
    for w in ("nemoc", "někdy", "několik", "neděle", "nemocný"):
        assert not is_negation(fold(lemma_word(w, "cs")), "cs"), w


def test_no_negation_set_for_a_language_without_a_prefix():
    for lang in ("en", "de", "es"):
        assert negated_lemmas(lang) == frozenset(), lang


def test_a_repaired_word_carries_no_polarity():
    st = {"query_lemma_tokens": ["moci", "zkouset", "nemoci"], "query_repair": ["nemoxi→nemoci"]}
    assert polarity_tokens(st) == ["moci", "zkouset"]
    assert negation_conflict(polarity_tokens(st), {"moci", "zkouset"}, "cs") == []
    assert polarity_tokens({"query_lemma_tokens": ["nesmet"]}) == ["nesmet"]


def test_a_typed_word_keeps_its_polarity_beside_the_same_repaired_word():
    # "nemit" typed, and the typo "nemti" repaired to "nemit": only one goes
    st = {"query_lemma_tokens": ["nemit", "obcanka", "nemit"], "query_repair": ["nemti→nemit"]}
    assert polarity_tokens(st) == ["obcanka", "nemit"]
    assert negation_conflict(polarity_tokens(st), {"mit", "obcanka"}, "cs") == ["nemit"]


def test_tokens_with_diacritics_are_judged_too():
    # normalization.strip_diacritics false keeps diacritics in the tokens
    assert is_negation("nesmět", "cs") and is_negation("Nemít", "cs")
    assert not is_negation("někdy", "cs") and not is_negation("nemoc", "cs")
    assert negation_conflict(["nesmět", "mít"], {"smět", "mít"}, "cs") == ["nesmět"]
    assert negation_conflict(["kdy"], {"někdy"}, "cs") == []


def test_short_base_ignored():
    """"ne"+two letters is too short to be a negated word ("nebe" vs "be")."""
    assert negation_conflict(["nebe"], {"be"}, "cs") == []


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
