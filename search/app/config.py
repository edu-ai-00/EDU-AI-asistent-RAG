# last change in code by Jan Drozd and Martin Safranek (2026-08-21).
"""Centralised configuration — all values come from environment variables."""

import os


# ── Postgres ──────────────────────────────────────────────────
PG_HOST = os.getenv("PG_HOST", "postgres")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER", "edu_ai")
PG_PASSWORD = os.getenv("PG_PASSWORD", os.getenv("POSTGRES_PASSWORD", ""))
PG_DATABASE = os.getenv("PG_DATABASE", "edu_ai")
PG_SCHEMA = os.getenv("PG_SCHEMA", "edu_ai")

# ── Tables ────────────────────────────────────────────────────
CHUNKS_TABLE = f"{PG_SCHEMA}.document_chunks"
# keywords merged onto document_chunks (2026-08-07); no separate keyword table
METADATA_TABLE = f"{PG_SCHEMA}.document_metadata"

# ── QA pack tables (offline pack mirror + server vector fallback) ──
QA_MANIFEST_TABLE = f"{PG_SCHEMA}.qa_pack_manifest"
QA_ENTRIES_TABLE = f"{PG_SCHEMA}.qa_entries"
QA_VARIANTS_TABLE = f"{PG_SCHEMA}.qa_variants"
QA_SYNONYM_TABLE = f"{PG_SCHEMA}.qa_synonym"
QA_EMBEDDING_TABLE = f"{PG_SCHEMA}.qa_embedding"
QA_BLOCKLIST_TABLE = f"{PG_SCHEMA}.qa_blocklist"
EMBEDDING_REGISTRY_TABLE = f"{PG_SCHEMA}.embedding_registry"
QUESTION_ANSWER_LOG_TABLE = f"{PG_SCHEMA}.question_answer_log"

# ── OpenAI ────────────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
ANALYZER_MODEL = os.getenv("ANALYZER_MODEL", "gpt-4.1-mini")
ANSWER_MODEL = os.getenv("ANSWER_MODEL", "gpt-4.1-mini")
# follow-up rewriting (resolve_context) — a few tokens in, a question out, and it
# sits BEFORE retrieval, so its latency is added to every follow-up query
CONTEXT_MODEL = os.getenv("CONTEXT_MODEL", "gpt-4.1-mini")
# hard cap on generated-answer length — output tokens dominate LLM latency
# (~30–60 tok/s), so capping the answer is the single biggest latency lever
ANSWER_MAX_TOKENS = int(os.getenv("ANSWER_MAX_TOKENS", "350"))

# ── Search defaults ──────────────────────────────────────────
VECTOR_TOP_K = int(os.getenv("VECTOR_TOP_K", "5"))
VECTOR_FETCH_K = int(os.getenv("VECTOR_FETCH_K", "30"))
KEYWORD_TOP_K = int(os.getenv("KEYWORD_TOP_K", "5"))
# candidate cap for rag_keywords: overlap is scored in Python, so this must be
# large enough to see all keyword-bearing chunks of a document, not just the first
# few (was 30, which only inspected a handful of chunks and missed the match).
KEYWORD_FETCH_K = int(os.getenv("KEYWORD_FETCH_K", "1000"))

# ── Retrieval feature switches (GLOBAL, platform-wide) ───────────
# One place to turn the two heavy retrieval layers on/off for the whole platform.
# A per-request params_override (eval sweeps) or a search_setting step param still
# wins over these defaults.
#   RAG_BM25_LEXICAL  — BM25 re-rank of document_lexical candidates (pg_search).
BM25_LEXICAL = os.getenv("RAG_BM25_LEXICAL", "false").lower() in ("1", "true", "yes", "on")
#   RAG_RERANK_MODE   — cross-encoder rerank of the fused top-K:
#     off   — no reranker (fastest)
#     local — in-process bge-reranker-v2-m3 (CPU, ~12–16 s/dotaz)
#     cloud — OpenAI LLM listwise reranker (RERANK_OPENAI_MODEL, ~3–5 s/dotaz)
RERANK_MODE = os.getenv("RAG_RERANK_MODE", "off").strip().lower()
RERANK_TOP_K = int(os.getenv("RAG_RERANK_TOP_K", "20"))
#   RERANK_OPENAI_MODEL — which OpenAI-family model the cloud reranker uses.
RERANK_OPENAI_MODEL = os.getenv("RERANK_OPENAI_MODEL", "gpt-4o-mini")
#   JINA_API_KEY / RERANK_JINA_* — the cross-encoder reranker at api.jina.ai.
#   A third mode beside `local` (bge in-process) and `cloud` (LLM listwise):
#   it returns a CALIBRATED relevance score per passage, which is the one thing
#   the LLM listwise reranker cannot give — its output is a permutation, and a
#   position carries no threshold. Measured 2026-08-27 on 20 passages: 419-456 ms
#   (against 3-8 s for the LLM judge), 5 392 input tokens, no output tokens, and
#   the identical score to four decimals across three identical calls.
JINA_API_KEY = os.getenv("JINA_API_KEY", "")
RERANK_JINA_URL = os.getenv("RERANK_JINA_URL", "https://api.jina.ai/v1/rerank")
# v3, NOT the newer v3.5: measured 2026-08-27 on produktový vlastník's nine questions with the
# QA path off, v3.5 answered 6/9 where the LLM judge answered 9/9, while v3 and
# v2-base-multilingual answered 8/9 — v3.5 alone ranked a New Year's greeting
# above the events calendar. v3 also scores off-topic passages NEGATIVELY, which
# is what makes an absolute threshold (rerank_prefilter.raw_min) mean anything.
RERANK_JINA_MODEL = os.getenv("RERANK_JINA_MODEL", "jina-reranker-v3")
# fusion: keyword_rank weight. Was 3.0 ("keywords = truest content signal"), but
# that let an OFF-topic keyword hit (šikana) dominate selection; 1.0 = equal to
# lexical/vector (measured: fixes the GDPR case without breaking přijímačky).
KEYWORD_WEIGHT = float(os.getenv("RAG_KEYWORD_WEIGHT", "1.0"))
METADATA_TOP_K = int(os.getenv("METADATA_TOP_K", "10"))
RRF_K = int(os.getenv("RRF_K", "60"))
CONTENT_TOP_N = int(os.getenv("CONTENT_TOP_N", "5"))
SOURCES_TOP_N = int(os.getenv("SOURCES_TOP_N", "3"))   # user-facing links: top-N documents
TOKEN_BUDGET = int(os.getenv("TOKEN_BUDGET", "8000"))

# ── QA cascade tuning ─────────────────────────────────────────
# A/B/C thresholds default to the pack manifest values; these env vars are the
# fallback when a manifest row is missing. They are PLACEHOLDERS — calibrate on
# real queries (question_answer_log records the scores).
QA_T_ANSWER = float(os.getenv("QA_T_ANSWER", "0.60"))    # coverage ≥ this AND margin ≥ m_margin → answer (A)
QA_T_SUGGEST = float(os.getenv("QA_T_SUGGEST", "0.30"))  # coverage ≥ this (but not A) → suggestions (B)
QA_M_MARGIN = float(os.getenv("QA_M_MARGIN", "0.08"))    # relative gap top1↔top2 needed for a confident answer
QA_VECTOR_COSINE_MIN = float(os.getenv("QA_VECTOR_COSINE_MIN", "0.75"))  # min cosine to accept a curated QA-vector hit
QA_VECTOR_FETCH_K = int(os.getenv("QA_VECTOR_FETCH_K", "20"))            # neighbours pulled before proximity picks
QA_VECTOR_TIER_BAND = float(os.getenv("QA_VECTOR_TIER_BAND", "0.001"))   # cosine distance within which proximity, not distance, decides
QA_SUGGEST_TOP_N = int(os.getenv("QA_SUGGEST_TOP_N", "3"))
QA_LEXICAL_FETCH_K = int(os.getenv("QA_LEXICAL_FETCH_K", "50"))  # candidate variants pulled before entry aggregation
RAG_LEXICAL_FETCH_K = int(os.getenv("RAG_LEXICAL_FETCH_K", "30"))

# ── Guard responses (child-safe; ~10–11-year-old audience) ────
GUARD_PROFANITY_MESSAGE = os.getenv(
    "GUARD_PROFANITY_MESSAGE",
    "Zkus se prosím zeptat slušně a bez neslušných slov. "
    "Rád ti pomůžu s otázkami k přijímačkám. 🙂",
)
# self_harm is handled separately — never a "be polite" refusal.
GUARD_SELF_HARM_MESSAGE = os.getenv(
    "GUARD_SELF_HARM_MESSAGE",
    "Je mi líto, že se teď necítíš dobře. Nejsi na to sám/sama a je v pořádku "
    "poprosit o pomoc. Zavolej prosím na **Linku bezpečí 116 111** (zdarma, "
    "nonstop) nebo si promluv s někým dospělým, komu důvěřuješ. "
    "Když jde o život, volej 112.",
)
GUARD_UNUSABLE_MESSAGE = os.getenv(
    "GUARD_UNUSABLE_MESSAGE",
    "Nerozuměl jsem tvé otázce. Zkus ji prosím napsat trochu jinak.",
)
NO_ANSWER_MESSAGE = os.getenv(
    "NO_ANSWER_MESSAGE",
    "Na tuto otázku zatím nemám odpověď. Zkus ji prosím přeformulovat.",
)
# Never "rephrase": the question is fine, the answering model is not. Without
# this entry _CODE_FALLBACK returns "" and an outage during a DB blip would
# answer with a blank string.
LLM_UNAVAILABLE_MESSAGE = os.getenv(
    "LLM_UNAVAILABLE_MESSAGE",
    "Teď nemůžu odpověď připravit kvůli dočasnému technickému problému. "
    "Tvoje otázka je v pořádku — zkus to prosím za chvíli znovu.",
)
