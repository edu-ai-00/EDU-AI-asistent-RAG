# last change in code by Jan Drozd (2026-09-03).
"""What this conversation has already asked and already been offered.

One suggestion repeated across turns is the difference between a chat that leads
somewhere and one that goes in circles. Measured on production 2026-09-03: a
second turn offered back the very question the first turn had asked, plus a chip
the first turn had already shown and the user had ignored.

The two halves are NOT the same rule:

  asked    the question was already put to the system in this conversation, so
           offering it again is always wrong — the user has the answer. DROPPED.
  offered  a chip was shown and not taken. That is not a refusal ("not yet" reads
           the same as "no thanks"), and dropping it can empty a list that a
           stricter relevance bar has already cut to one item. DEMOTED to the end,
           so a fresh chip always wins the slot and a stale one still beats
           nothing.

Scoped by entity AND caller, not by conversation_id alone: the id arrives from
the client, so keying on it by itself would let anyone read back another
conversation's turns by naming its UUID — the same hole resolve_context._load_turns
is careful about, and the same fix.
"""

from __future__ import annotations

import logging

from .db import get_cursor
from . import config

logger = logging.getLogger(__name__)

# How far back a conversation counts. Long enough for a chat picked up after
# lunch, short enough that yesterday's questions do not silently strip today's
# chips — a suppression nobody can see is worse than a repeat everybody can.
_WINDOW = "12 hours"
# Turns read back. The longest real conversation in production is 13 turns
# (2026-09-03), so this is the whole thing with room over, not a sample.
_LIMIT = 40

# `domains IS NOT DISTINCT FROM %s::text[]` — NULL-SAFE EQUALITY, and it has to
# be exactly that. The rule (spec §7): a turn counts as part of this
# conversation only if it was asked in the SAME thematic scope, and a NULL row
# (asked before migration 213, or asked without a filter) belongs to the
# unrestricted scope ALONE. Plain `=` would drop every historical row even for
# an unrestricted request, because NULL = NULL is unknown; `&&` or a containment
# test would let a turn asked in one area suppress a chip in another. The
# canonical array is what makes the comparison meaningful — the codes arrive
# trimmed, upper-cased, deduplicated and SORTED, so two spellings of one
# selection are one value here.
_SEEN_SQL = f"""
SELECT raw_query, suggestions_offered
  FROM {config.QUESTION_ANSWER_LOG_TABLE}
 WHERE conversation_id = %s
   AND legal_entity_code IS NOT DISTINCT FROM %s
   AND user_id IS NOT DISTINCT FROM %s
   AND domains IS NOT DISTINCT FROM %s::text[]
   AND created_at > now() - %s::interval
 ORDER BY created_at DESC
 LIMIT %s
"""


def _key(text) -> str:
    """The comparison form of a question — what the client would send back.

    A chip's `question` is resent verbatim, so it reappears as the next turn's
    raw_query character for character; casefold and strip are for the typed
    variant of the same question, not for near-matching.
    """
    return (text or "").strip().casefold()


def unique_questions(items) -> list[dict]:
    """One button per question text, first occurrence winning.

    The outgoing list is assembled from several builders (curated chips, the
    other-ročník net, document sections, LLM variants), each deduping only within
    itself — so the same sentence can arrive twice from two of them. Applied at the
    response boundary because that is the only place all of them have met.
    """
    seen, out = set(), []
    for it in items or []:
        k = _key(it.get("question"))
        if k and k in seen:
            continue
        if k:
            seen.add(k)
        out.append(it)
    return out


def load_seen(conversation_id, legal_entity, user_id,
              domains: list[str] | None = None) -> tuple[set[str], set[str]]:
    """(asked, offered) question keys for this conversation. Never raises.

    Empty for a request without a conversation_id, which is most non-chat traffic
    (evals, scripts) — those have no turns to repeat.

    `domains` scopes the memory the same way it scopes the search. A client is
    expected to start a new conversation when the area changes, but the server
    cannot rely on that: conversation_id arrives from the client, and a direct
    API caller may reuse it (spec §10.8). Without this, changing the area kept
    the previous area's questions suppressed — the user picks "Stravování",
    asks the catering question their "Docházka" turn had already offered, and
    the chip they need is dropped as a repeat.

    Argument, not read off a global: every call site has the request's scope in
    hand, and a default of None would silently mean "unrestricted" for a caller
    that forgot to pass it. It is keyword-defaulted only so the eval harness and
    scripts keep working unchanged — for them the value IS None.
    """
    if not conversation_id:
        return set(), set()
    try:
        with get_cursor() as cur:
            cur.execute(_SEEN_SQL, (str(conversation_id), legal_entity, user_id,
                                    list(domains) if domains else None,
                                    _WINDOW, _LIMIT))
            rows = cur.fetchall()
    except Exception as e:
        # a dedupe is a refinement of an offer — it must never cost the response
        logger.warning("conversation: seen lookup failed (%s) — no dedupe", e)
        return set(), set()
    asked, offered = set(), set()
    for raw_query, suggestions in rows:
        if raw_query:
            asked.add(_key(raw_query))
        for s in (suggestions or []):
            if isinstance(s, dict) and s.get("question"):
                offered.add(_key(s["question"]))
    return asked, offered


def drop_repeats(items, asked: set[str], offered: set[str]) -> list[dict]:
    """Suggestions minus what was asked, with what was already shown demoted.

    `clarify` passes through untouched: its `question` is the asking sentence, not
    a query, so it is not the kind of thing that CAN repeat — the sendable texts
    live in its options.
    """
    if not items or not (asked or offered):
        return list(items or [])
    fresh, stale = [], []
    for it in items:
        if it.get("kind") == "clarify":
            fresh.append(it)
            continue
        k = _key(it.get("question"))
        if k and k in asked:
            continue
        (stale if k and k in offered else fresh).append(it)
    if not fresh and stale:
        logger.info("conversation: every chip was already offered — kept, demoted")
    return fresh + stale
