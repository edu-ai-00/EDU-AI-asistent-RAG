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

from ..settings import (terms_any_language, terms_for_language,
                        organization_entities, coverage_stopwords)

from ..db import get_cursor
from ..languages import languages_for_entity
from ..llm import get_chat
from ..messages import system_message
from .. import conversation
from ..state import SearchState
from .. import config
from .. import scope
from .prepare import _union_forms
from eduai_normalize import detect_lang

logger = logging.getLogger(__name__)

# Scoped by entity, not by conversation_id alone. The id arrives from the client,
# so keying on it by itself would let a caller replay somebody else's turns by
# naming their UUID — the same shape of hole as an unverified legal_entity.
# `domains IS NOT DISTINCT FROM` for the same reason conversation.load_seen uses
# it: the turns fed to the rewrite must be the turns asked in the SAME area, and
# a NULL row belongs to the unrestricted scope alone. Rewriting "A co potom?"
# against a previous turn about admissions, while the user has since switched to
# catering, produces a standalone question about the area they left — and the
# retrieval that follows is correctly filtered, so the result is a confident
# no_answer to a question nobody asked.
_TURNS_SQL = f"""
SELECT raw_query, answer_md
  FROM {config.QUESTION_ANSWER_LOG_TABLE}
 WHERE conversation_id = %s
   AND legal_entity_code IS NOT DISTINCT FROM %s
   AND user_id IS NOT DISTINCT FROM %s
   AND domains IS NOT DISTINCT FROM %s::text[]
   AND created_at > now() - %s::interval
 ORDER BY created_at DESC
 LIMIT %s
"""

# NOT_A_QUESTION is the second exception, and it is the reason this prompt has a
# marker at all. Reported 2026-09-04: "Aha" and "Tak to není sranda" were rewritten
# into the PREVIOUS question — the instruction says to fill the subject in from
# context, and with no question in the input the model fills in all of it — so the
# assistant answered the same thing three times in a row. Returning the input
# unchanged would stop the repeat but leave the cascade searching for "sranda";
# saying so outright lets the caller answer like a person instead.
_NOT_A_QUESTION = "NENI_OTAZKA"
_PROMPT = (
    "Přepiš poslední otázku tak, aby dávala smysl sama o sobě, bez znalosti "
    "předchozí konverzace. Doplň podmět a téma z kontextu. Zachovej jazyk a "
    "význam, nic nepřidávej ani nevysvětluj. Pokud otázka samostatná už je, vrať "
    f"ji beze změny. Pokud poslední vstup NENÍ otázka — je to potvrzení, "
    f"poděkování, citoslovce nebo komentář (\"aha\", \"díky\", \"no jo\", "
    f"\"tak to není sranda\") — vrať přesně {_NOT_A_QUESTION} a nic jiného. "
    "Jinak vrať POUZE tu otázku."
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
            _dom = state.get("domain_codes")
            cur.execute(_TURNS_SQL, (conv, state.get("legal_entity"),
                                     state.get("user_id"),
                                     list(_dom) if _dom else None,
                                     window, limit))
            rows = cur.fetchall()
        return [(r[0], r[1]) for r in reversed(rows) if r[0]]
    except Exception as e:
        logger.warning("resolve_context: turn lookup failed (%s)", e)
        return []


# {dom} is scope.domain_where("v", "domain_ids", …) — EMPTY for an unrestricted
# request, so the probe SQL is unchanged there. Both probes take it for one
# reason, and it is the opposite of the usual one: they decide whether to SPEND a
# rewrite, and a match means "no rewrite needed". A curated answer in an area the
# caller excluded can never answer this turn, so letting it match would BLOCK the
# rewrite that the turn does need (spec §7, and the plan's "odpověď mimo zvolenou
# oblast neblokuje potřebný přepis otázky"). Here a missing filter costs recall,
# not privacy.
_EXACT_SQL = """
SELECT 1
  FROM {table} v
 WHERE v.is_active
   AND v.legal_entity_code = ANY(%s)
   AND (v.norm_text = %s OR v.lemma_text = %s){dom}
 LIMIT 1
"""


# Candidates ranked the way qa_lexical ranks them, so the probe below asks the
# same corpus the same way — it just stops at "is one of these a full cover?".
_COVER_SQL = """
SELECT concat_ws(' ', v.norm_text, v.lemma_text) AS txt
  FROM {table} v
 WHERE v.is_active
   AND v.legal_entity_code = ANY(%s)
   AND (v.ts_norm || v.ts_lemma) @@ to_tsquery('simple', %s){dom}
 ORDER BY ts_rank_cd((v.ts_norm || v.ts_lemma), to_tsquery('simple', %s)) DESC
 LIMIT %s
"""


def _probe_domain(state):
    """(fragment, params) for the two probes above — the request's area filter."""
    return scope.domain_where("v", "domain_ids", state.get("domain_ids"))


def _answers_as_typed(question: str, cfg, state: SearchState) -> bool:
    """Does a curated question already contain EVERY content word of this one?

    Then it stands on its own and a rewrite can only damage it. Measured
    2026-09-04: "A co když zmeškám náhradní termín?" matched its entry with
    coverage 1.00, the leading "A" made the heuristic call it a follow-up, and the
    rewrite — correct in meaning — added "mám dělat" and "JPZ" (df 21 and 78 of
    210). Those flooded the OR-query, the winning entry flipped, coverage fell
    below the bar, and the answer came from the vector step: right topic, wrong
    question.

    Sibling of _has_exact_variant, one step looser: that one needs the whole text
    to match a variant, this one needs the content words to be contained in one.
    Full containment on purpose — a partial cover is exactly the elliptical
    follow-up that SHOULD be rewritten. Two content words minimum for the same
    reason: "odvolání" alone is an ellipsis, not a question.

    Scope is the organisation, like the exact probe: this only decides whether to
    spend a rewrite, and every later step still enforces the rest.
    """
    try:
        langs = languages_for_entity(state.get("legal_entity"))
        lang = (state.get("language_filter")
                or detect_lang(question, allowed=tuple(langs)))
        forms = _union_forms(question, cfg, (lang,) if lang else tuple(langs))
        stop = set(coverage_stopwords().get(lang or "") or ())
        content = {t for t in forms["lemma"] if t and t not in stop}
        terms = sorted({t for t in (list(forms["norm"]) + list(forms["lemma"])) if t})
        entities = organization_entities(state.get("legal_entity"))
        if not entities or not terms or len(content) < 2:
            return False
        tsq = " | ".join(terms)
        with get_cursor() as cur:
            _dc, _dp = _probe_domain(state)
            cur.execute(
                _COVER_SQL.format(table=config.QA_VARIANTS_TABLE, dom=_dc),
                (entities, tsq, *_dp, tsq, 20))
            for (txt,) in cur.fetchall():
                if content <= {t for t in (txt or "").split() if t}:
                    return True
        return False
    except Exception as e:
        logger.warning("resolve_context: cover probe failed (%s) — rewriting", e)
        return False


def _has_exact_variant(question: str, cfg, state: SearchState) -> bool:
    """Would qa_exact match this text as typed? Then it needs no rewrite.

    The keys are built exactly as prepare() will build them — same language
    resolution, same normalization config — so a hit here means a hit there.
    Scope is the organisation, not the full role/validity filter: this decides
    only whether to spend a rewrite, and qa_exact still enforces the rest.
    """
    try:
        langs = languages_for_entity(state.get("legal_entity"))
        lang = (state.get("language_filter")
                or detect_lang(question, allowed=tuple(langs)))
        forms = _union_forms(question, cfg, (lang,) if lang else tuple(langs))
        norm, lemma = " ".join(forms["norm"]), " ".join(forms["lemma"])
        entities = organization_entities(state.get("legal_entity"))
        if not entities or not (norm or lemma):
            return False
        with get_cursor() as cur:
            _dc, _dp = _probe_domain(state)
            cur.execute(
                _EXACT_SQL.format(table=config.QA_VARIANTS_TABLE, dom=_dc),
                (entities, norm, lemma, *_dp))
            return cur.fetchone() is not None
    except Exception as e:
        logger.warning("resolve_context: exact probe failed (%s) — rewriting", e)
        return False


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
    if out.strip(".!? ").upper() == _NOT_A_QUESTION:
        return _NOT_A_QUESTION
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
    # A short question can still be a COMPLETE one: a suggestion button is
    # literally a canonical_q, and a bare variant written for qa_exact is ≤4
    # words, which _needs_context() reads as a follow-up. Measured 2026-09-03:
    # the rewrite of "V kolik začíná JPZ?" inherited "čtyřleté obory / roce
    # 2027" from the previous answer, lost the exact match and answered from an
    # unrelated entry — 2.5 s for a wrong answer where 30 ms had the right one.
    if _has_exact_variant(question, cfg, state):
        logger.info("resolve_context: %r matches a curated variant as typed — "
                    "no rewrite", question)
        return {"context_skip_reason": "raw text is an exact QA match"}

    # One ranking query (tens of ms) before an LLM call (~700 ms) — cheaper on
    # average, and it keeps a confident deterministic match from being spent.
    if params.get("skip_when_covered", True) and _answers_as_typed(question, cfg, state):
        logger.info("resolve_context: %r is already covered by a curated question "
                    "— no rewrite", question)
        return {"context_skip_reason": "raw text covers a curated question"}

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

    if resolved == _NOT_A_QUESTION:
        # A CHIP WE OFFERED IS NEVER AN INTERJECTION. `related` chips are section
        # headings — "LEGISLATIVNÍ PŘEDPISY" — and whether the model calls a bare
        # noun phrase a question is a coin toss: the same click was dismissed for
        # one user and answered from the documents in a replay minutes later
        # (2026-09-04). The contract says every chip's text can be sent straight
        # back, so the verdict is overruled and the text goes on as typed. The
        # lookup is deferred to here — it costs a query, and only this branch
        # needs it.
        try:
            _, _offered = conversation.load_seen(
                state.get("conversation_id"), state.get("legal_entity"),
                state.get("user_id"), state.get("domain_codes"))
        except Exception:
            _offered = set()
        if question.strip().casefold() in _offered:
            logger.info("resolve_context: %r was offered as a chip — taken as typed",
                        question)
            return {"context_skip_reason": "text was offered as a suggestion"}
        logger.info("resolve_context: %r is not a question — answering directly",
                    question)
        # outcome `answer`, not `no_answer`: nothing failed. The turn is complete
        # and the reply is deliberate, while no_answer makes the chat print
        # "Odpověď nebyla nalezena" over it (OUTCOME_LABELS in the web client).
        # answer_source stays `none` — that is what says no corpus was involved.
        return {"resolved": True, "outcome": "answer", "answer_source": "none",
                "answer": system_message("not_a_question",
                                         state.get("query_language")),
                "suggestions": [], "sources_json": "[]",
                "user_language": state.get("query_language"),
                "context_turns": len(turns)}
    if resolved == question:
        return {}
    # BACKSTOP for the instruction above. A prompt can drift away from a model
    # silently, and the only symptom is the bug it was written for coming back. A
    # rewrite that reproduces the question asked one turn ago is that bug: nobody
    # asks again for an answer still on their screen. Only the LAST turn —
    # further back, repeating a question is a normal thing to do.
    if turns and (turns[-1][0] or "").strip().casefold() == resolved.strip().casefold():
        logger.info("resolve_context: rewrite of %r echoes the previous question "
                    "— discarded", question)
        return {"context_skip_reason": "rewrite echoed the previous question"}

    logger.info("resolve_context: %r → %r (%d turns)", question, resolved, len(turns))
    # user_question is overwritten on purpose: every step downstream should search
    # for the resolved text. The log writes raw_query from the REQUEST object, so
    # what the user actually typed is recorded regardless of what happens here.
    return {"user_question": resolved,
            "raw_question": question,
            "context_turns": len(turns)}
