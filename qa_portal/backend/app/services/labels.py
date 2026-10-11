"""Shared label-fallback rule for domains and categories (CONTRACT.md §4.1).

requested lang -> default UI lang -> stable code. A missing translation never
drops the item from the list and never changes what it is assigned to; it
only sets label_fallback=true so the UI can flag it.
"""
from __future__ import annotations


def resolve_label(labels: dict[str, str] | None, lang: str | None,
                   default_lang: str, code: str) -> tuple[str, bool]:
    # Omitting `lang` MEANS "the UI default language", so a translation for it
    # is the requested one, not a substitute: label_fallback stays false.
    # It only turns true when the asked-for language is genuinely missing.
    labels = labels or {}
    wanted = lang or default_lang
    if labels.get(wanted):
        return labels[wanted], False
    if labels.get(default_lang):
        return labels[default_lang], True
    for value in labels.values():
        if value:
            return value, True
    return code, True
