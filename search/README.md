# edu_ai — RAG search module (`edu_ai_search`)

Server-side **routing + retrieval + answer orchestration** for the EDU-AI
assistant. One FastAPI service that answers a question by walking a deterministic
**QA→RAG cascade** over Postgres (+ OpenAI for embeddings and a single generation
call).

- **Consumers:** the web chat BFF and the Flutter app (via the nginx `/search/`
  edge).
- **Not in scope:** content ingestion (that is the Langflow ingestion module) and
  any UI (separate repos). This module has no DB writes except append-only query
  logging.

> Using an AI coding assistant? Direct it to [`AGENTS.md`](../AGENTS.md) first.

## Architecture

```mermaid
flowchart LR
  q[POST /v1/ask] --> ex[executor: config-driven pipeline]
  ex --> route[route: guard + scope]
  route --> qa[curated QA: exact/lexical/vector]
  qa --> rag[document RAG: lexical/keywords/vector]
  rag --> ans[answer: LLM or no_answer]
  ans --> r[SearchResponse]
```

**Textual equivalent:** a request enters `server.py`, which calls the
config-driven `executor`. The executor loads the active `search_setting`, builds
Czech norm/lemma/stem query tokens, then runs the guard/scope router, the curated
QA steps (which return author-written answers with no LLM), and — only if those
miss — the document RAG gatherers, ending at one terminal LLM answer or a polite
`no_answer`. Full detail: [`docs/ai/`](../docs/ai/).

## Quick start

The service runs as the `edu_ai_search` container in the repo's docker-compose
stack (see the repo-root [`ONBOARDING.md`](../ONBOARDING.md) for the full stack).

```bash
# from the repo root, with the stack built and .env in place:
docker compose up -d edu_ai_search
curl -s http://localhost:8090/health
curl -s -X POST http://localhost:8090/v1/ask \
  -H 'content-type: application/json' \
  -d '{"question": "Kdy začíná školní rok?", "legal_entity": "<ENTITY_CODE>"}'
```

Replace `<ENTITY_CODE>` with an active entity code from your installation; `legal_entity` is required.

Run the evaluation sets and resilience tests:

```bash
bash tests/run_all_evals.sh          # outcome distribution + CSV export
python3 tests/test_resilience.py     # fault-injection (dev stack only)
```

## Repository map

| Path | Responsibility |
|---|---|
| `app/server.py` | FastAPI: `/search`, `/health`, `/explain` + background logging |
| `app/executor.py` · `settings.py` | Config-driven pipeline walk; active `search_setting` |
| `app/nodes/` | Cascade steps: `route`, `qa_*`, `rag_*`, `answer` |
| `app/{normalize,stemmer,lemmatizer,text_norm}.py` | Czech norm/lemma/stem (parity contract) |
| `../contracts/` | OpenAPI, error codes, JSON schemas, parity vectors |
| `../docs/` | Architecture + per-stage AI behavior pages |
| `../specs/` | Feature specs (active / completed) |
| `../tests/` | `eval_search.py`, `eval_*_questions.txt`, `test_resilience.py` |

## Navigation

- [`AGENTS.md`](../AGENTS.md) — agent execution contract
- [`docs/architecture/system.md`](../docs/architecture/system.md) — component map + data flow
- [`contracts/openapi.yaml`](../contracts/openapi.yaml) — HTTP API
- [`docs/ai/`](../docs/ai/) — routing, retrieval, QA fast-path, caching, evaluations
- Validate docs: `make docs-check`
