# AGENTS.md — EDU-AI asistent RAG module

Execution contract for AI coding agents. Human rationale lives in
[`docs/`](docs/); do not duplicate it here.

## Commands

Run from the **repository root**, with the dev stack built and `.env` present.

```bash
# run the retrieval service (baked image — a code change needs a rebuild)
docker compose up -d edu_ai_search
docker compose build edu_ai_search && docker compose up -d edu_ai_search

# health, and one live query (legal_entity is required; without it you get 422)
curl -s http://localhost:8090/health
curl -s -X POST http://localhost:8090/v1/ask -H 'content-type: application/json' \
  -d '{"question":"Kdy zacina skolni rok?","legal_entity":"<ENTITY_CODE>","user_role":"STUDENT"}'

# regression eval — the primary harness (metadata per case, snapshot + diff)
python3 tests/eval_rag.py --repeat 3
python3 tests/eval_rag.py --baseline tests/eval_out/<snapshot>.json

# flat question sets (outcome distribution + CSV export)
bash tests/run_all_evals.sh
python3 tests/eval_search.py tests/eval_prijimacky_questions.txt

# behavioural and fault-injection tests (dev stack only)
python3 tests/test_route_guard.py
python3 tests/test_resilience.py
python3 tests/test_resilience.py r4        # one targeted case

# docs validation
make docs-check

# parity: assert the committed vectors still describe live behaviour
docker cp contracts/parity/. edu_ai_search:/tmp/parity/
docker exec -i edu_ai_search python - /tmp/parity < contracts/parity/check_vectors.py

# regenerate the CZ vectors after a deliberate normalization/stemming change
docker exec -i edu_ai_search python - \
  < contracts/parity/gen_vectors.py \
  > contracts/parity/cs-normalization-vectors.json
```

## Repository map

| Responsibility | Path |
|---|---|
| HTTP API, models, logging | `search/app/server.py`, `search/app/log.py` |
| Pipeline runner and step registry | `search/app/executor.py` |
| Configuration resolution and cache | `search/app/settings.py`, `search/app/config.py` |
| Entity, visibility and role enforcement | `search/app/scope.py` |
| Idempotency claim and replay | `search/app/idempotency.py` |
| Router (guard, pack and entity scope) | `search/app/nodes/route.py` |
| Follow-up rewriting | `search/app/nodes/resolve_context.py` |
| Curated QA path | `search/app/nodes/qa_exact.py`, `qa_lexical.py`, `qa_vector.py`, `qa_common.py` |
| Document retrieval | `search/app/nodes/document_lexical.py`, `rag_keywords.py`, `rag_vector.py`, `document_siblings.py` |
| Fusion, ordering, generation | `search/app/nodes/answer.py`, `answer_generator.py` |
| Reranking backends | `search/app/rerank.py` |
| Topical admission | `search/app/topical.py` |
| Wording per language | `search/app/messages.py`, `search/app/languages.py` |
| Normalization | `search/app/text_norm.py`, `search/eduai_normalize/` |
| Contracts | `contracts/` (openapi, errors, schemas, parity) |
| Clean database install | `postgres/init_clean/` |

## Source precedence

- **Current behaviour:** tests → contracts → docs → comments.
- **Intended behaviour:** accepted spec (`specs/`) → the `docs/ai/` page → contracts → tests.
- A conflict between sources is a **defect**: report it, do not silently pick one.
- The live cascade is `executor.py` plus the active `settings` row — **not**
  `graph.py`, which is a readable reference and may drift.
- Behaviour is data. Reading the code does not tell you what the system does;
  the active configuration does — see [docs/SETTINGS_REFERENCE.md](docs/SETTINGS_REFERENCE.md).

## Boundaries

**Always**

- Validate input at the API boundary; keep the public error codes in
  `contracts/errors.yaml`.
- Run the parity check **before and after** any normalization or stemming change;
  update `contracts/parity/` vectors first, then both implementations.
- Keep each pipeline step self-contained and non-crashing (catch, fall through);
  preserve the `no_answer` safety net.
- Route every query's `WHERE` through `search/app/scope.py`. It is the single
  enforcement point for entity, visibility and role.
- Re-measure the regression set with `--repeat` before claiming an improvement:
  the set has a noise floor and a single-case diff proves nothing.
- Add or extend tests for behaviour changes; run `make docs-check` before
  completion.

**Ask first**

- A new production dependency.
- Any schema or index change: full-text configuration, ANN index parameters, the
  embedding model or its dimension, any column on the QA or chunk tables.
- Changing cascade precedence, or a step's `on_hit` / run-condition semantics.
- Changing idempotency or response-cache semantics.
- Adding a configuration flag: settings are **per organisation**, so it must be
  written to every active row, not only the platform-wide one.

**Never**

- Commit secrets, or log raw questions and retrieved content in standard logs.
  Structured identifiers only; the query log is the one sanctioned sink.
- Copy executable prompt text into documentation. Prompts live versioned in
  `prompt_registry` and are fetched by key.
- Bypass the scope or active-document filters to make a test pass.
- Edit generated files (the parity vectors, the clean-install dumps) by hand —
  regenerate them.
- Hard-code a legal entity, a language or a threshold. All three are data.

## Expected patterns

```python
# GOOD — a step catches its own error and falls through (never 500s the cascade)
try:
    state.update(fn(state, params) or {})
except Exception as e:
    logger.warning("executor: step '%s' raised (%s) — continuing", name, e)

# BAD — an unguarded step aborts the whole request
state.update(fn(state, params))
```

```python
# GOOD — structured log with identifiers, no user content
logger.info("Search completed in %d ms (outcome=%s via %s)", ms, outcome, source)
# BAD — logs the raw question and the retrieved content
logger.info("answered '%s' with %s", req.question, context_text)
```

## Completion definition

The implementation matches the accepted spec; tests cover success and failure;
the contracts are updated; every `sources:` path in a touched page is valid and
`last_verified` was bumped **only** after actually checking those sources;
`make docs-check`, the parity check and the regression set pass; remaining
uncertainty is reported explicitly rather than left silent.
