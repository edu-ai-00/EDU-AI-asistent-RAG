# last change in code by Jan Drozd (2026-09-18).
"""Unit tests for the route guard's school-identity answer (migration 216).

Pure: no stack, no DB, no LLM — the phrase set, the entity row and the message
are injected, so this covers the MATCHING and the fall-through rules, not the
rows in qa_blocklist. Run it inside the search image:

    docker compose run --rm --entrypoint python edu_ai_search tests/test_route_identity.py

The FAILING case is production log 3926 (2026-09-11, ZS_JEDOVNICE): "jak se
jmenuje naše škola?" ended no_answer with "zkus to přeformulovat", which no
rephrasing could fix — the name is in legal_entity, not in any index.

NOT covered here (needs the DB): that qa_blocklist actually holds the phrases
and system_message the 'school_identity' row.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.nodes import route  # noqa: E402

PHRASES = {"jak se jmenuje nase skola", "nazev skoly", "jak se jmenujete",
           "na jakou skolu chodim"}
MESSAGE = "Tahle škola se jmenuje {{SCHOOL}}."


class _Cur:
    """Minimal cursor stand-in: every SELECT here asks for one display_name."""

    def __init__(self, name):
        self._name = name

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a):
        pass

    def fetchone(self):
        return (self._name,) if self._name is not None else None


def _answer(question, name="Základní škola Jedovnice", message=MESSAGE,
            phrases=PHRASES):
    route._identity_phrases = lambda: phrases
    route._meta_fillers = lambda lang: {"prosim", "jeste", "vlastne"}
    route.get_cursor = lambda: _Cur(name)
    route.system_message = lambda key, lang=None: message
    return route._identity_check({"user_question": question,
                                  "query_language": "cs",
                                  "legal_entity": "ZS_JEDOVNICE"})


# ── the production finding ──
def test_the_logged_question_is_answered():
    out = _answer("jak se jmenuje naše škola?")
    assert out and out["outcome"] == "answer"
    assert out["answer"] == "Tahle škola se jmenuje Základní škola Jedovnice."
    assert out["trace_detail"] == {"guard": "identity"}
    # the enum in contracts/schemas/search-response.schema.json stays as it is
    assert out["answer_source"] == "meta"


def test_diacritics_case_and_punctuation_do_not_matter():
    assert _answer("Název školy")
    assert _answer("JAK SE JMENUJE NAŠE ŠKOLA")
    assert _answer("nazev skoly?")


def test_fillers_are_stripped_before_the_equality():
    assert _answer("jak se prosím jmenuje naše škola?")


# ── what must stay on the cascade ──
def test_a_question_about_a_person_is_not_the_school():
    assert _answer("jak se jmenuje školník?") is None
    assert _answer("jak se jmenuje učitel IT?") is None


def test_a_school_question_that_merely_contains_the_words():
    assert _answer("kdy začíná školní rok?") is None
    assert _answer("jak se jmenuje naše škola v přírodě a kdy se platí?") is None


def test_no_name_falls_through():
    assert _answer("název školy", name=None) is None
    assert _answer("název školy", name="  ") is None


def test_missing_message_row_falls_through():
    # new code, migration 216 not applied yet: system_message returns ""
    assert _answer("název školy", message="") is None


def test_a_db_failure_never_breaks_the_guard():
    def _boom():
        raise RuntimeError("db down")
    route._meta_fillers = lambda lang: set()
    route._identity_phrases = _boom
    assert route._identity_check({"user_question": "název školy",
                                  "query_language": "cs",
                                  "legal_entity": "ZS_JEDOVNICE"}) is None


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as e:
                failed += 1
                print(f"  FAIL {name}: {e}")
    print("FAILED" if failed else "all identity tests passed")
    sys.exit(1 if failed else 0)
