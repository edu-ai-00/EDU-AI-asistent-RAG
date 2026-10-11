"""label_fallback must mean "the asked-for translation is missing" and nothing
else (CONTRACT.md §4.1). Covers the three cases the portal actually issues:
`lang` omitted, `lang=cs` sent explicitly, and a language that really has no
translation. Applies to domains and categories alike — both go through this
one function.
"""
from __future__ import annotations

import pytest

from app.services.labels import resolve_label

CS_ONLY = {"cs": "Docházka"}
CS_AND_EN = {"cs": "Docházka", "en": "Attendance"}


def test_omitted_lang_uses_the_ui_default_and_is_not_a_fallback():
    label, fallback = resolve_label(CS_ONLY, None, "cs", "ATTENDANCE")
    assert label == "Docházka"
    assert fallback is False, "cs exists and cs is the UI default — nothing was substituted"


def test_explicit_cs_behaves_exactly_like_omitting_it():
    omitted = resolve_label(CS_ONLY, None, "cs", "ATTENDANCE")
    explicit = resolve_label(CS_ONLY, "cs", "cs", "ATTENDANCE")
    assert omitted == explicit == ("Docházka", False)


def test_a_genuinely_missing_translation_is_a_fallback():
    label, fallback = resolve_label(CS_ONLY, "en", "cs", "ATTENDANCE")
    assert label == "Docházka", "falls back to the UI default rather than hiding the item"
    assert fallback is True


def test_requested_language_wins_when_it_exists():
    assert resolve_label(CS_AND_EN, "en", "cs", "ATTENDANCE") == ("Attendance", False)
    assert resolve_label(CS_AND_EN, "cs", "cs", "ATTENDANCE") == ("Docházka", False)


def test_no_translation_at_all_falls_back_to_the_code():
    assert resolve_label({}, None, "cs", "BULLYING") == ("BULLYING", True)
    assert resolve_label(None, "cs", "cs", "BULLYING") == ("BULLYING", True)


def test_an_unrelated_translation_is_used_but_flagged():
    label, fallback = resolve_label({"de": "Anwesenheit"}, None, "cs", "ATTENDANCE")
    assert label == "Anwesenheit"
    assert fallback is True


@pytest.mark.parametrize("empty", ["", None])
def test_an_empty_string_is_not_a_translation(empty):
    label, fallback = resolve_label({"cs": empty, "en": "Attendance"}, None, "cs", "ATTENDANCE")
    assert label == "Attendance"
    assert fallback is True


def test_a_non_default_ui_language_is_respected():
    # ui_default_lang is configurable; the rule must follow it, not hardcode cs.
    assert resolve_label(CS_AND_EN, None, "en", "ATTENDANCE") == ("Attendance", False)
    assert resolve_label(CS_ONLY, None, "en", "ATTENDANCE") == ("Docházka", True)
