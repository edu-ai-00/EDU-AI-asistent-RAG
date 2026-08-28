# last change in code by Martin Safranek (2026-08-04).
"""The language registry — `code_list('language')` read at runtime.

`edu_ai.code_list` with list_code='language' is the SINGLE registry of
languages. Adding a language starts there; every consumer (this service, the
web chat, the ingestion API) reads the available languages from it instead of
carrying its own list. That is what makes adding a language a data change.

The safe ordering falls out of the existing `is_active` column: INSERT the row
inactive, fill the layers (stopwords, synonyms, blocklist, system_message,
engines, labels, library support), activate it LAST. Everything here filters on
is_active, so a half-prepared language is invisible.

Two flags that look alike and must never be confused (migration 42):

    is_ui_default          which option a FORM preselects. Presentation only —
                           deliberately NOT exposed by this module, so no
                           processing path can reach for it by accident.
    is_processing_fallback which language the pipeline uses when detection
                           cannot tell. This is the one the pipeline reads.

Cached like settings.py: read once, refreshed explicitly. The registry changes
about as often as the search config, and every query would otherwise pay a
round-trip.
"""

import logging
import threading

from . import config
from .db import get_cursor

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict | None = None


def _load() -> dict:
    """{'active': (codes…), 'fallback': code|None, 'ui_default': code|None,
        'labels': {code: {lang: label}}}"""
    try:
        with get_cursor() as cur:
            cur.execute(
                f"SELECT code, is_processing_fallback, is_ui_default, labels "
                f"FROM {config.PG_SCHEMA}.code_list "
                "WHERE list_code = 'language' AND is_active "
                "ORDER BY sort_order, code"
            )
            rows = cur.fetchall()
        active = tuple(r[0] for r in rows)
        fallback = next((r[0] for r in rows if r[1]), None)
        ui_default = next((r[0] for r in rows if r[2]), None)
        labels = {r[0]: (r[3] or {}) for r in rows}
        if not active:
            raise ValueError("no active language in code_list")
        if fallback is None:
            # Not fatal for the query side (it unions every active language when
            # detection fails), but ingest has nothing to fall back to.
            logger.warning("languages: no is_processing_fallback row — ingest "
                           "has no fallback for undecidable content")
        logger.info("languages: active=%s fallback=%s ui_default=%s",
                    active, fallback, ui_default)
        return {"active": active, "fallback": fallback,
                "ui_default": ui_default, "labels": labels}
    except Exception as e:
        # NOT swallowed into a cs-only fallback. Such a fallback would only
        # pretend to be resilient: the registry lives in the same database every
        # retrieval step needs, so a query that cannot read it was going to fail
        # at qa_exact anyway — the "fallback" would just replace a clear error
        # with a Czech-only answer built on nothing.
        logger.error("languages: registry unreadable (%s)", e)
        raise


def registry(force: bool = False) -> dict:
    global _cache
    with _lock:
        if _cache is None or force:
            _cache = _load()
        return _cache


def active_languages() -> tuple:
    """Language codes usable right now — the `allowed` set for detect_lang."""
    return registry()["active"]


def languages_for_entity(legal_entity: str | None) -> tuple:
    """The SCHOOL's language selection (init 109): its own 'language' rows when
    it selected some, the whole platform set otherwise. Falls back to the
    platform set on any failure — a broken selection must narrow nothing."""
    if not legal_entity:
        return active_languages()
    try:
        from .db import get_cursor
        from . import config as _cfg
        with get_cursor() as cur:
            cur.execute(f"SELECT code FROM {_cfg.PG_SCHEMA}."
                        f"resolved_code_list(%s, 'language')", (legal_entity,))
            rows = [r[0] for r in cur.fetchall()]
        return tuple(rows) if rows else active_languages()
    except Exception as e:
        logger.warning("languages_for_entity(%s) failed (%s) — platform set",
                       legal_entity, e)
        return active_languages()


def fallback_language() -> str | None:
    """The pipeline's fallback when content gives no language signal.

    INGEST ONLY. The query side must not call this: when it cannot tell, it
    builds the query forms in every active language rather than picking one.
    """
    return registry()["fallback"]


def ui_default_language() -> str | None:
    """PRESENTATION fallback — which language to SHOW when none was resolved.

    The only legitimate use of is_ui_default inside the service: the response's
    `language` field and the canned guard / no_answer texts have to be in some
    language even for a query whose language nothing could determine. That is a
    display decision, not a retrieval one — it never selects a stemmer, a
    lemmatizer or a stoplist. For those, see fallback_language().
    """
    return registry()["ui_default"]


def language_name(code: str, in_lang: str = "en") -> str:
    """Human name of a language from the registry's `labels` jsonb.

    Used for the deterministic "Respond in <language>" instruction, so adding a
    language does not mean editing a name map in code.
    """
    return (registry()["labels"].get(code) or {}).get(in_lang) or code


def refresh() -> dict:
    return registry(force=True)
