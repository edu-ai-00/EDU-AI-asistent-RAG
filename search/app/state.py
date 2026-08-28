# last change in code by Jan Drozd and Martin Safranek (2026-08-22).
"""LangGraph state schema for the deterministic search cascade.

Flow (all deterministic except the final `answer` LLM):

    prepare → route → qa_exact → qa_lexical ─┬─ A: curated answer             → END
                                             ├─ B: suggestions ("myslel jsi")  → END
                                             └─ C: document_lexical → qa_vector ─hit→ curated answer → END
                                                                          └miss→ rag_vector → answer(LLM) → END

Principle: QA (curated) beats RAG (generated); lexical beats vector; the only
LLM on the path is `answer`, and `qa_vector` gates before it so a curated hit
always wins over a generated one.
"""

from __future__ import annotations

from typing import TypedDict, Literal


class SearchState(TypedDict, total=False):
    """Flows through every node of the cascade."""

    # ── Input (from SearchRequest) ────────────────────────────
    # The question the cascade searches for. resolve_context may REPLACE this
    # with a standalone rewrite of a follow-up; raw_question then holds what the
    # user typed. question_answer_log.raw_query comes from the request, not from here.
    user_question: str
    raw_question: str | None                # set only when a rewrite happened
    context_turns: int | None               # prior turns fed to that rewrite
    user_id: str | None
    user_school_grade: int | None           # ročník — pack scoping
    # A code_list('role') code — STUDENT | TEACHER | PARENT — already validated
    # and upper-cased by server._valid_role; `str` rather than a Literal for the
    # same reason SearchRequest uses one: the codebook is the authority, and a
    # frozen list here can only drift from it. The Czech slugs zak/ucitel/rodic
    # are the WEB CHAT's own vocabulary and are translated at that boundary.
    # NOT inert (init 127): every retrieval step passes it to scope.where(),
    # where it becomes `AND audience_roles && %s::text[]` — a HARD visibility
    # filter. A missing role short-circuits to outcome='role_required' in
    # server._role_gate before any retrieval runs.
    user_role: str | None
    conversation_id: str | None
    session_id: str | None
    # query tokens in both index forms (mirror qa_variants norm_text/lemma_text).
    # Client may send them (on-device simplemma); else `prepare` derives them.
    query_norm_tokens: list[str]            # surface: lower, no diacritics, tokenized
    query_lemma_tokens: list[str]           # PURE simplemma lemmas
    query_stem_tokens: list[str]            # PURE Dolamic-Savoy light stems

    # ── route: guard + scope ──────────────────────────────────
    pack_ids: list[str]                     # QA pack(s) in scope (by grade); empty = all active
    # the caller's ročník split into the two kinds the tiers distinguish
    # (route._scope resolves both): an EXACT ročník row, and a "9999" row whose
    # entity covers that ročník — the organisation's does, a sibling SŠ's does not
    grade_exact_ids: list[int]
    grade_universal_ids: list[int]
    block_category: str | None              # profanity|hate|sexual|self_harm|violence when blocked

    # ── cascade control / outcome ─────────────────────────────
    resolved: bool                          # a terminal decision was reached → route to END
    outcome: Literal["answer", "suggest", "blocked", "self_harm",
                     "unusable", "no_answer"]
    answer_source: Literal["guard", "qa_exact", "qa_lexical", "qa_vector",
                           "rag", "none"]
    # granular list of the retrieval steps that actually contributed, in order
    # (e.g. ["document_lexical"] or ["qa_lexical"]). Populated by the executor and
    # logged to question_answer_log.strategies — finer than the coarse answer_source.
    strategies: list[str]

    # ── QA lexical scoring (A/B/C) ────────────────────────────
    decision: Literal["A", "B", "C"]
    match_score: float | None               # coverage of the top entry (0..1)
    match_margin: float | None              # relative gap top1↔top2
    matched_entry_id: str | None

    # ── outputs ───────────────────────────────────────────────
    answer: str                             # curated / generated / guard message
    answer_parts: list[dict]                # one entry per answer paragraph + its source
    legal_entity: str                       # whose search this is
    organization_entities: list[str]         # root of its organisation + all under it
    # ALWAYS returned (empty only on guards/qa_exact): every item's `question` is
    # a text the client can resend to /search. kinds: qa (curated entry, with
    # entry_id+score), clarify (domains differ; `options` = refined queries),
    # related (unread section of an answering document).
    suggestions: list[dict]
    # qa_lexical decision C parks its nearest entries here; only
    # resolve_unanswered reads it, so a real answer never carries junk.
    fallback_suggestions: list[dict]
    sources_json: str                       # JSON string
    user_language: str                      # ISO 639-1 (answer language)

    # ── RAG branch (retrieval-only; consumed by `answer`) ─────
    rag_chunks: list[dict]                   # candidate chunks gathered by document_lexical + rag_vector
    # synonym groups the query started, in their original spelling (["OZP",
    # "ozdravný pobyt", "škola v přírodě"]). Retrieval acts on the expansion; the
    # answer prompt has to be TOLD about it, or the model refuses a row that says
    # "OZP" to a question that says "škola v přírodě".
    synonym_hint: list[list[str]]
    keywords_used: list[str]                 # query concepts rag_keywords matched → response key_words_used
    context_text: str
    # embedding of user_question — computed ONCE by the first vector step
    # (qa_vector) and reused by the next (rag_vector); the question never
    # changes mid-cascade, so re-embedding it would be a wasted OpenAI call
    query_vec: list[float] | None
