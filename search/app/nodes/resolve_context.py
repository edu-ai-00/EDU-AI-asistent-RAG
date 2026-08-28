# last change in code by Jan Drozd and Martin Safranek (2026-08-20).
"""Node — resolve_context: turn a follow-up into a question that stands alone.

"A kdy je termín?" retrieves nothing: the subject lives in the previous turn and
the index has never seen the pronoun. This step rewrites such a query using the
conversation's earlier turns, so every retrieval step downstream matches on text
that carries its own subject.

Runs BEFORE prepare, because the norm/lemma/stem forms the whole cascade matches
on are derived from the question — rewriting after prepare would search for one
question while having tokenized another.

The turns come from question_answer_log, i.e. from what this server itself wrote. A
history supplied by the client would have to be validated and still could not be
trusted; reading our own log removes the question entirely.
"""

from __future__ import annotations

import logging

from langchain_core.messages import SystemMessage, HumanMessage

from ..settings import terms_any_language, terms_for_language

from ..db import get_cursor
from ..llm import get_chat
from ..state import SearchState
from .. import config

logger = logging.getLogger(__name__)

# Scoped by entity, not by conversation_id alone. The id arrives from the client,
# so keying on it by itself would let a caller replay somebody else's turns by
# naming their UUID — the same shape of hole as an unverified legal_entity.
_TURNS_SQL = f"""
SELECT raw_query, answer_md
  FROM {config.QUESTION_ANSWER_LOG_TABLE}
 WHERE conversation_id = %s
   AND legal_entity_code IS NOT DISTINCT FROM %s
   AND user_id IS NOT DISTINCT FROM %s
   AND created_at > now() - %s::interval
 ORDER BY created_at DESC
 LIMIT %s
"""

_PROMPT = (
    "Přepiš poslední otázku tak, aby dávala smysl sama o sobě, bez znalosti "
    "předchozí konverzace. Doplň podmět a téma z kontextu. Zachovej jazyk a "
    "význam, nic nepřidávej ani nevysvětluj. Pokud otázka samostatná už je, vrať "
    "ji beze změny. Vrať POUZE tu otázku."
)

# Openers and pro-forms that point at something said earlier. A query starting
# with one of these, or leaning on one, is almost never self-contained.
#
# FALLBACK ONLY — the live lists are the qa_stopword číselník, scopes
# 'context_leading' and 'context_anaphora' (migration 155). These words grow every
# time someone notices a missing one, and they differ per language, so they are
# data; the sets below only carry the search through a DB outage. Both were
# Czech+English only while the platform serves four languages, which is why they
# had to move: a German "Und dann?" was never recognised as a follow-up.
_LEADING = {"a", "ale", "no", "tak", "takze", "takže", "and", "but", "so"}
_ANAPHORA = {"to", "ta", "ten", "ty", "toho", "tomu", "tim", "tím", "tam",
             "tehdy", "on", "ona", "oni", "jeho", "její", "jejich",
             "it", "that", "this", "there", "they"}


def _needs_context(question: str, lang: str | None = None) -> bool:
    """Does this query lean on something the index cannot see?

    `lang` is the caller-declared language (`language_filter`), which is in the
    state before this step runs. It matters: the Czech pro-forms `to`, `on`, `ta`,
    `ty` and `tam` are also ordinary English words, so without the language every
    English question containing "to" or "on" was read as a follow-up and paid an
    LLM rewrite it did not need. When the caller declares no language we still do
    not know it here — this step runs BEFORE prepare, which is what derives it —
    so the merged list stands, erring towards one extra rewrite rather than a
    missed follow-up.
    """
    words = (question or "").lower().replace("?", " ").split()
    if not words:
        return False
    if len(words) <= 4:
        return True
    if lang:
        leading = terms_for_language("context_leading", lang, _LEADING)
        anaphora = terms_for_language("context_anaphora", lang, _ANAPHORA)
    else:
        leading = terms_any_language("context_leading", _LEADING)
        anaphora = terms_any_language("context_anaphora", _ANAPHORA)
    return words[0] in leading or any(w in anaphora for w in words)


def _load_turns(state: SearchState, window: str, limit: int) -> list[tuple[str, str]]:
    """Earlier turns of this conversation, oldest first. Never raises."""
    conv = state.get("conversation_id")
    if not conv:
        return []
    try:
        with get_cursor() as cur:
            cur.execute(_TURNS_SQL, (conv, state.get("legal_entity"),
                                     state.get("user_id"), window, limit))
            rows = cur.fetchall()
        return [(r[0], r[1]) for r in reversed(rows) if r[0]]
    except Exception as e:
        logger.warning("resolve_context: turn lookup failed (%s)", e)
        return []


def _rewrite(turns: list[tuple[str, str]], question: str, params: dict) -> str:
    """One small LLM call. Returns the original question on anything unexpected."""
    history = "\n".join(
        f"Otázka: {q}\nOdpověď: {(a or '')[:400]}" for q, a in turns
    )
    llm = get_chat(params.get("model") or config.CONTEXT_MODEL,
                   params.get("max_tokens", 120))
    resp = llm.invoke([
        SystemMessage(content=_PROMPT),
        HumanMessage(content=f"Konverzace:\n{history}\n\nPoslední otázka: {question}"),
    ])
    out = (resp.content or "").strip().strip('"')
    # A rewrite that came back empty, or ballooned into an explanation, is worse
    # than the original — the retrieval steps would match on the model's prose.
    if not out or len(out) > max(len(question) * 6, 200):
        return question
    return out


def resolve_context(state: SearchState, cfg: dict | None = None) -> dict:
    """Rewrite a follow-up into a standalone query, or leave the state alone.

    Returns {} when nothing applies — no conversation, a self-contained question,
    no prior turns, or any failure. Context is an improvement, never a condition:
    a broken lookup or an unreachable model must not cost the user their answer.
    """
    params = (cfg or {}).get("context") or {}
    if not params.get("enabled", True):
        return {}

    question = state.get("user_question", "") or ""
    if not _needs_context(question, state.get("language_filter")):
        return {}

    turns = _load_turns(state,
                        params.get("window", "2 hours"),
                        int(params.get("max_turns", 4)))
    if not turns:
        return {}

    try:
        resolved = _rewrite(turns, question, params)
    except Exception as e:
        logger.warning("resolve_context: rewrite failed (%s) — keeping raw question", e)
        return {}

    if resolved == question:
        return {}

    logger.info("resolve_context: %r → %r (%d turns)", question, resolved, len(turns))
    # user_question is overwritten on purpose: every step downstream should search
    # for the resolved text. The log writes raw_query from the REQUEST object, so
    # what the user actually typed is recorded regardless of what happens here.
    return {"user_question": resolved,
            "raw_question": question,
            "context_turns": len(turns)}
