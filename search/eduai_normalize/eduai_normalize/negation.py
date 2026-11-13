"""Which folded lemmas are a negation the lemmatizer made, for the polarity check
of the curated-QA match (qa_common.negation_conflict).

A folded token alone cannot tell `nekdy` ("někdy") from a negation; the set,
generated over the dictionary by negation_build.py (command in AGENTS.md), can.
Languages without a negation prefix have an empty set.
"""

from __future__ import annotations

from ._negated_lemmas import NEGATED_LEMMAS
from .normalize import fold

_EMPTY: frozenset = frozenset()


def negated_lemmas(lang: str | None) -> frozenset:
    """The folded negated lemmas of `lang`; None = of every language."""
    if lang:
        return NEGATED_LEMMAS.get(lang, _EMPTY)
    return frozenset().union(*NEGATED_LEMMAS.values()) if NEGATED_LEMMAS else _EMPTY


def is_negation(token: str, lang: str | None) -> bool:
    """True when `token` (a lemma) is a negation the lemmatizer makes. The token is
    folded first: the set is folded, and a school that keeps diacritics in its
    tokens (normalization.strip_diacritics false) must not lose the check."""
    t = fold(token or "")
    if lang:
        return t in NEGATED_LEMMAS.get(lang, _EMPTY)
    return any(t in s for s in NEGATED_LEMMAS.values())
