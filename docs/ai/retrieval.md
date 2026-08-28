---
id: docs.ai.retrieval
status: current
components: [retrieval, ranking, normalization, api]
sources:
  - search/app/nodes/document_lexical.py
  - search/app/nodes/rag_keywords.py
  - search/app/nodes/rag_vector.py
  - search/app/nodes/document_siblings.py
  - search/app/nodes/answer.py
  - search/app/nodes/qa_common.py
  - search/app/rerank.py
  - search/app/scope.py
  - search/app/topical.py
  - search/app/text_norm.py
  - search/app/config.py
last_verified: 2026-08-28
---

# Retrieval

**Track:** human (AI-relevant). The document (RAG) retrieval pipeline that feeds
the terminal `answer` LLM. Control types as defined in [routing.md](routing.md).
Curated-QA matching is documented separately in [qa-fastpath.md](qa-fastpath.md).

"Normalized" throughout means the token forms defined by the parity vectors
([../../contracts/parity/](../../contracts/parity/)); the query and the index are
reduced by the same `search/app/text_norm.build_forms` contract.

Parameter **values** are not restated here. Every step's parameters, their
measured values and the reasoning behind them live in
[../SETTINGS_REFERENCE.md](../SETTINGS_REFERENCE.md), which is their canonical
owner; this page describes what each stage *does*.

## Pipeline stages (in order)

All `document_*` and `rag_*` steps are **gather-only**: they append candidate
chunks to the pool and never answer. The single terminal `answer` node consumes
it.

1. **`document_lexical`** (`enforced`) — full text over the chunk table.
2. **`rag_keywords`** (`enforced`) — the keyword array stored on each chunk.
3. **`rag_vector`** (`enforced`) — pgvector cosine.
4. **`document_siblings`** (`enforced`) — neighbouring chunks for context.
5. **`answer`** (fusion and ordering `enforced`, generation `prompt`).

Each step carries its own run condition in the settings row, evaluated by
`search/app/executor.py`:

| Condition | Meaning |
|---|---|
| `enabled` | `false` skips the step entirely. |
| `only_if_empty` | run only when the pool is still empty. |
| `only_if_max_coverage_below` | run only when the best candidate's term coverage is below the given ratio — "the lexical pool is weak". |
| `run_on_timespan` | run even for date/period questions, where lexical matching is typically poor. |

A step may therefore be enabled and still not run on a given query. Whether a
step is on, and under which condition, is configuration — see
[../SETTINGS_REFERENCE.md](../SETTINGS_REFERENCE.md) §3.

## Scope and visibility (`enforced`)

Every retrieval step builds its `WHERE` clause through `search/app/scope.py`,
which is the single enforcement point:

- A **legal person never sees another legal person's content**, under any
  configuration. This is not a setting.
- Within one organisation, `entity`-visible documents reach the ancestors of the
  owning unit **and downward to the units below an ancestor**, so a unit sees
  what the organisation above it published. A sibling unit is neither an ancestor
  nor a descendant and is not reached by this rule alone.
- `organization`-visible documents reach the parent and its units.
- `private` documents reach their owner alone.
- Excluded documents (`not_excluded`) and inactive metadata rows are filtered
  out, so a superseded document version can never be retrieved.

Role-based narrowing (`audience_roles`) is applied in the same clause when the
caller carries a role.

## Full text (document_lexical)

The primary document retriever. It matches against the chunk's generated
`bm25_text` column — the concatenation of the normalized text, lemmas, stems and
the heading lemmas — so a heading term and a body term are searchable in one
index.

- Query tokens (chosen `form`) are expanded through `qa_synonym` (entity groups
  only) and joined into an explicit **OR** tsquery over the `simple`
  configuration. `simple` means no built-in stemming: our own lemma/stem forms
  are the linguistic layer, and they are identical on the index and query sides
  (parity contract).
- **Why an OR query needs a gate.** An OR query hits a chunk on one incidental
  shared token, and `ts_rank_cd` scales with chunk size, so an absolute rank
  floor cannot separate off-topic matches across documents. Two size-independent
  gates are applied instead:
  - **IDF coverage** — the fraction of the question's *informative* mass present
    in the chunk, not the number of matching terms.
  - **Topical admission** — a chunk matching only a generic concept is not
    admitted. No round is entitled to return something (see
    [../architecture/decisions.md](../architecture/decisions.md) D-12).
- A separate **heading pass** matches headings alone, weighted above body
  matches. Terms appearing in more than a configured share of all chunks are
  treated as too common to carry meaning and are ignored in both passes.
- **Typo tolerance** on the document side is a trigram fallback: when full text
  finds nothing, candidates are matched by trigram similarity, subject to a
  similarity floor *and* a coverage floor, so one fuzzy token cannot carry a hit
  on its own.

## Keywords (rag_keywords)

Keywords are assigned at ingest and stored as an array column on the chunk —
there is no separate keyword table. Each stored keyword is reduced through the
same `build_forms` contract and the step requires a minimum overlap with the
query tokens. The same topical admission gate applies.

The step is **disabled in the reference configuration**: over a fixed question
set it moved the first passage in two cases while adding roughly a third more
candidates to the pool. The keyword *rank* is still used — it is annotated onto
candidates found by other steps and weighted in fusion, where it is the truest
content signal available.

## Vector search (rag_vector, qa_vector)

- The embedding model and target table come from `edu_ai.embedding_registry`
  (`status = 'active'`). `document_chunks.embedding` is the only vector store in
  the system.
- Search is `ORDER BY embedding <=> query::vector LIMIT fetch_k`, with cosine
  similarity `1 - (embedding <=> query)`. The ANN index type and its parameters
  are defined in the chunk DDL, which is their canonical owner.
- A relevance floor (`cosine_min`) drops neighbours below it, so an off-topic
  query ends with an empty pool and therefore `no_answer` rather than a weak hit.
  The floor differs between the curated-QA and document paths.
- **Grade enrichment:** when the asker's year is known, the embedded query is
  prefixed with it, so the vector space sees the year as part of the question.

## Siblings (document_siblings)

Pulls chunks neighbouring a hit so that a rule split across two chunks is not
half-answered. Sibling chunks are marked and ordered **after** evidence chunks,
so context can never outrank the passage that actually matched. Disabled in the
reference configuration.

## Fusion and ordering (answer)

The pool is fused once and then **ordered by tiers, not by a score**. This is the
single most important thing to understand about ranking; the full tuple, the
meaning of each tier and the measured consequence are documented in
[../SETTINGS_REFERENCE.md](../SETTINGS_REFERENCE.md) §3.10 and recorded as
decision D-8.

In outline:

- The **fused score** combines the lexical rank, the keyword rank, the vector
  rank and a heading-match bonus, each with its own weight.
- The **tiers** decide the order before that score is ever consulted: evidence
  before siblings, then the grade tier, then the priority (authority) tier, then
  — under the grade-hard branch — a heading match, then the document type's
  authority ladder. The fused score breaks ties inside a tier.
- Chunks are capped per document, so one long document cannot crowd out the
  rest, and a citation-ratio bar decides which candidates are good enough to be
  named as sources.

## Reranking

Reranking runs on the fused pool before the context is assembled. Four modes are
available — `off`, an in-process cross-encoder, an LLM judge, and a hosted
cross-encoder — and their trade-offs and the measurements behind the recommended
arrangement are in [../SETTINGS_REFERENCE.md](../SETTINGS_REFERENCE.md) §3.11.

Two properties are architectural rather than tuning, and hold in every mode:

- **The judge is a veto, not a ranker** (`enforced`). It returns a per-passage
  grade; the tier order, which is computed by SQL and fusion and is identical on
  every run, decides the sequence. An LLM permutation is not reproducible — this
  is decision D-10.
- **A hosted reranker may select, never order** (`enforced`). Used as a
  prefilter it decides which candidates reach the judge and the tier order is
  restored immediately afterwards. It runs only when the pool is larger than the
  judge's window, the leading candidate is protected, and a timeout skips the
  prefilter instead of failing the query. This is decision D-11.

**Thin-chunk guards** (`enforced`): a chunk shorter than a configured length is
technically a hit but cannot answer anything, so it may neither seed the focus
nor anchor the judge.

## Generation (`answer`, control type = `prompt`)

The terminal LLM is prompted to answer only from the supplied context, not to
invent facts, to reply in the question's language, and to state the year a
passage applies to when it differs from the asker's. These are **`prompt`
behaviours, not guarantees** — do not describe them as safeguards.

The prompt text lives versioned in `edu_ai.prompt_registry` and is fetched by a
technical key per request; it is never copied into documentation. The model,
temperature, reasoning effort and token budget are configuration.

An empty pool produces `no_answer` with no LLM call. An LLM failure degrades to
`no_answer` rather than a 500 — which means a provider outage is indistinguishable
from a genuine gap in the corpus, a known observability gap.

## Known gaps

- **Follow-up resolution is disabled** in the reference configuration
  (`intended`), so a question like "and when is that?" is resolved by retrieval
  alone rather than against the previous turn.
- **Lesson-scoped retrieval does not exist** (`intended`). Scope resolution is by
  legal entity, visibility, role and year — not by lesson.
- **Refusal detection** for the borderline zone (domain-adjacent but
  unanswerable) is unenforced (`intended`).
