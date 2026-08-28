---
id: spec.v3-cascade
status: completed
components: [api, retrieval, qa, cache]
sources:
  - search/app/executor.py
  - search/app/settings.py
  - search/app/config.py
  - search/app/nodes/route.py
  - search/app/nodes/qa_lexical.py
  - search/app/nodes/document_lexical.py
last_verified: 2026-07-23
---

# v3 config-driven QA→RAG cascade

## Problem

The original retrieval fused several strategies in parallel (RRF) inside a
Langflow agent. It was hard to explain, hard to tune, and mixed curated answers
with generated ones. The team needed an explainable order, curated-first
behavior, and the ability to retune without code changes.

## Desired outcome

A single deterministic cascade where curated QA beats generated RAG and lexical
beats vector, with exactly one LLM call at the end, whose order and parameters
live in data (`edu_ai.search_setting`) so a version can be A/B tested by
switching the active row.

## Scope

**Included:** the router guard/scope, curated QA (exact/lexical/vector), document
RAG gather (lexical/keywords/vector), the terminal answer, and the config-driven
executor. **Excluded:** ingestion, intent classification (QA vs. document), a
response cache, private/public visibility enforcement, multi-turn context.

## Constraints

- The pipeline order, enabled steps, and params MUST be read from the active
  `search_setting.config`; changing them MUST NOT require a code change.
- Curated QA (`qa_*`) MUST be able to terminate before any document RAG runs.
- A single pipeline step MUST NOT be able to crash the whole cascade.
- Query and index tokens MUST be reduced by the same normalization contract
  (parity).
- The service MUST NOT return HTTP 500 for an LLM or content-availability problem;
  it MUST degrade to `no_answer`.

## Acceptance criteria

- [x] `server.py` runs `executor.run_search` (config-driven), not `graph.py`.
- [x] Reordering / disabling a step by editing `search_setting.config.pipeline`
      changes behavior with no code change (`executor.py`).
- [x] `qa_exact`/`qa_lexical`/`qa_vector` can each end the cascade (`on_hit: stop`).
- [x] `rag_keywords` and `rag_vector` run only when the chunk pool is empty
      (`only_if_empty`).
- [x] A raised step is caught and the cascade continues (`executor.py`).
- [x] An exhausted unresolved pipeline returns `outcome=no_answer`.
- [x] LLM failure in `answer` returns `no_answer`, not 500 (`nodes/answer.py`).
- [x] Lexical steps gate on coverage, not raw `ts_rank` (`nodes/document_lexical.py`,
      `qa_lexical.py`).

## Implementation map

| Area | Path | Status |
|---|---|---|
| Executor + registry | `search/app/executor.py` | done |
| Active config + cache | `search/app/settings.py`, `config.py` | done |
| Router | `search/app/nodes/route.py` | done |
| QA steps | `search/app/nodes/qa_*.py`, `qa_common.py` | done |
| RAG steps | `search/app/nodes/rag_*.py` | done |
| Answer | `search/app/nodes/answer.py`, `answer_generator.py` | done |

## Failure behavior

Invalid input (empty question) → HTTP 400. Step exception → logged, skipped,
cascade continues. Empty retrieval → `no_answer`. Provider (OpenAI) failure in a
vector step → that step falls through; in `answer` → `no_answer`. Missing active
setting → in-code `_FALLBACK` config.

## Verification

```bash
bash tests/run_all_evals.sh
python3 tests/test_resilience.py r4      # LLM down → deterministic QA still answers
make docs-check
```

## Open decisions

- Calibrate A/B/C thresholds on `qa_query_log` (currently placeholders — see
  [../../docs/ai/qa-fastpath.md](../../docs/ai/qa-fastpath.md)).
- Whether to add a normalized-query response cache
  ([../../docs/ai/caching.md](../../docs/ai/caching.md)).
- Answer-node refusal detection for the borderline unanswerable zone.
