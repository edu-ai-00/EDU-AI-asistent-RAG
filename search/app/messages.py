# last change in code by Jan Drozd and Martin Safranek (2026-08-20).
"""System messages (guard, no_answer) — per-language, DB-first.

The deterministic paths (guard, no_answer) never call an LLM, so their texts
must exist per language as DATA: číselník edu_ai.system_message, read PER
REQUEST (hot-editable, same pattern as prompt_registry). Fallback chain:
requested language → Czech row → the config.py code constant.
"""

import logging

from . import config
from .db import get_cursor
from .languages import ui_default_language

logger = logging.getLogger(__name__)

# code fallback when the DB is unreachable (guard must never crash)
_CODE_FALLBACK = {
    "guard_profanity": config.GUARD_PROFANITY_MESSAGE,
    "guard_self_harm": config.GUARD_SELF_HARM_MESSAGE,
    "guard_unusable": config.GUARD_UNUSABLE_MESSAGE,
    "no_answer": config.NO_ANSWER_MESSAGE,
    "llm_unavailable": config.LLM_UNAVAILABLE_MESSAGE,
}


def system_message(key: str, lang: str | None = None) -> str:
    """Message text for `key` in `lang` (číselník code); missing → code fallback.

    `lang` is None when the query language could not be determined. Showing SOME
    language is unavoidable for a canned message, and which one to show is a
    presentation decision — so it comes from the registry's is_ui_default, not
    from a literal "cs" (and never from is_processing_fallback, which selects
    reducers on the ingest side).
    """
    # The registry read may raise (it no longer hides a dead DB behind a
    # cs-only fallback), but the guard must never crash — a blocked query has to
    # get SOME text back. Presentation may degrade; processing may not. That is
    # why the literal survives here and nowhere on the retrieval path.
    try:
        _ui = ui_default_language()
    except Exception:
        _ui = None
    lang = (lang or _ui or "cs").lower()
    try:
        with get_cursor() as cur:
            cur.execute(
                f"SELECT message FROM {config.PG_SCHEMA}.system_message "
                "WHERE message_key = %s AND lang = %s AND is_active LIMIT 1",
                (key, lang),
            )
            row = cur.fetchone()
            if row:
                return row[0]
            # Second chance in the registry's UI-default language, not in Czech.
            # This used to be `lang = 'cs'` hardcoded, so a deployment without
            # Czech served a CZECH message when a translation was missing.
            if _ui and _ui != lang:
                cur.execute(
                    f"SELECT message FROM {config.PG_SCHEMA}.system_message "
                    "WHERE message_key = %s AND lang = %s AND is_active LIMIT 1",
                    (key, _ui),
                )
                row = cur.fetchone()
                if row:
                    return row[0]
    except Exception as e:
        logger.warning("system_message(%s, %s) DB lookup failed (%s) — code fallback",
                       key, lang, e)
    return _CODE_FALLBACK.get(key, "")


def subject_absent(state) -> bool:
    """Did BOTH indexes look for the question's subject and both come up empty?

    Only then may the answer say "we do not have this". Either half alone is a
    claim about half the corpus: the QA packs hold ~210 curated questions while
    the school documents hold far more, so a subject missing from QA is routinely
    present in the documents. Unknown (the step did not run, or had no corpus
    statistics) is NOT absence — the older "try rephrasing" stands, because
    telling a parent the school never published something it did publish is worse
    than sending them round one more loop.
    """
    return (state.get("query_in_corpus") is False
            and state.get("query_in_documents") is False)


def resolve_unanswered(state) -> dict:
    """The terminal result for a cascade that produced NO answer.

    If qa_lexical parked a suggest earlier (deferred_suggest), emit THAT instead
    of a bare no_answer: the suggestions were withheld only to give the document
    retrievers a chance, not judged wrong. Every place that used to fabricate a
    no_answer result goes through here, so a deferred suggest cannot be lost on
    any path (executor safety net, empty chunk pool, LLM failure, both streaming
    branches).
    """
    ds = state.get("deferred_suggest")
    if ds:
        return dict(ds)
    return {
        "resolved": True, "outcome": "no_answer", "answer_source": "none",
        # WHICH no-answer this is. "Try rephrasing" is right when the words
        # missed and wrong when the subject is absent from the materials: the
        # asker then rephrases, fails again, and rephrases again (reported
        # 2026-09-04). qa_lexical says which case it is; None means it could not
        # tell, and the older, safer message stands.
        "answer": system_message(
            "no_answer_missing_topic" if subject_absent(state) else "no_answer",
            state.get("query_language")),
        # best-effort: qa_lexical parks its nearest entries even when they fell
        # below every threshold (decision C), so a dead end still ends with an
        # offer instead of an empty hand. The outcome stays no_answer — these
        # were judged too weak to claim "did you mean?".
        "suggestions": (state.get("fallback_suggestions") or [])
                       + list(state.get("other_grade_suggestions") or []),
        "sources_json": "[]", "user_language": state.get("query_language"),
    }


def resolve_llm_unavailable(state) -> dict:
    """Terminal result when the ANSWER MODEL failed — not the corpus.

    Deliberately not resolve_unanswered: its message asks the user to rephrase,
    the one thing that cannot help here, and its outcome makes an outage
    indistinguishable from a real gap in both the UI and the query log (log.py
    writes outcome into `stage`).

    A parked deferred_suggest still wins — those came from the corpus and are
    unaffected by the model being down.
    """
    ds = state.get("deferred_suggest")
    if ds:
        return dict(ds)
    return {
        "resolved": True, "outcome": "llm_unavailable", "answer_source": "none",
        "answer": system_message("llm_unavailable", state.get("query_language")),
        "suggestions": (state.get("fallback_suggestions") or [])
                       + list(state.get("other_grade_suggestions") or []),
        "sources_json": "[]", "user_language": state.get("query_language"),
    }
