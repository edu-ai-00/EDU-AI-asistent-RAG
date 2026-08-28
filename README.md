# EDU-AI asistent RAG module

AI assistant for primary/secondary-school pupils (entrance exams, school-leaving
exams, school life) — **multilingual RAG + Q&A over uploaded documents**. The
target audience is children aged ~10–15, so both answer and data safety come
first.

**Multilingual, driven by data rather than code.** The supported languages are
rows in a číselník (`code_list('language')`); the search API, the ingestion API
and the web chat read the active set at runtime. Language is detected
deterministically per document *and per part of a document*, so a file that
switches language is split and each part is indexed with that language's
lemmatizer and stemmer. Czech and English are active; German and Spanish were
verified end to end without a line of code (see
[docs/ADDING_A_LANGUAGE.md](docs/ADDING_A_LANGUAGE.md)).

Behaviour is likewise configuration, not code: the retrieval strategy, every LLM
prompt, the document-type taxonomy, stopwords, synonyms, the blocklist and the
user-facing messages all live in versioned tables read per request.

The stack is docker-compose-native: **7 containers** (nginx, FastAPI gateway,
Langflow, Postgres+pgvector, pgAdmin, search microservice, web chat UI).

---

## Setup documentation

| I want to… | Document |
|---|---|
| **run it locally** (dev, from a clean clone) | **[ONBOARDING.md](ONBOARDING.md)** |
| **understand every configuration parameter** | **[docs/SETTINGS_REFERENCE.md](docs/SETTINGS_REFERENCE.md)** |
| **understand the data model and set up a new school** | **[docs/DATA_MODEL_AND_SETUP.md](docs/DATA_MODEL_AND_SETUP.md)** |
| **see the component map and data flow** | **[docs/architecture/system.md](docs/architecture/system.md)** |
| **understand routing, retrieval, caching, evaluations** | **[docs/ai/](docs/ai/)** |
| **call the HTTP API** | **[contracts/openapi.yaml](contracts/openapi.yaml)** |
| **know why a design was chosen** | **[docs/architecture/decisions.md](docs/architecture/decisions.md)** |

> **Using an AI coding assistant?** Direct it to [AGENTS.md](AGENTS.md) first — it
> carries the commands, the repository map and the boundaries.


---

## How it works (in a nutshell)

Two planes:

- **Ingest** — `POST /v1/ingest` → the gateway detects the file type → runs the
  matching **Langflow** flow (chunking + embeddings + write to Postgres).
- **Retrieval / answer** — `POST /search/v1/ask` → the **`edu_ai_search`** microservice
  runs a configurable QA→RAG cascade over Postgres (+ OpenAI) → answer.

Data lives **only in Postgres + bind mounts** (`langflow_data/`, `uploads/`) —
**not in git**. Git = code + config + init SQL + Langflow flow exports.

---

## Prerequisites

- Docker (Engine + compose plugin, or Docker Desktop)
- `git`, `python3`
- **Your own OpenAI API key** (without credit both chunking and answer
  generation fail)

## TL;DR (details in ONBOARDING.md)

```bash
git clone https://github.com/edu-ai-00/EDU-AI-asistent-RAG.git && cd EDU-AI-asistent-RAG
python3 scripts/bootstrap.py
#   … then put a real OPENAI_API_KEY in .env and: docker compose up -d
```

Bootstrap generates `.env` with strong passwords, creates the nginx auth files,
waits for every service to report *healthy*, creates the Langflow API key,
imports the 6 flows and runs a smoke test. Idempotent — re-run it any time; it never
overwrites an existing file.

```bash
python3 scripts/smoke.py    # verify a running stack (no OpenAI cost, cron-safe)
```

---

## Repo layout

```
api/                    gateway (/v1/ingest) — type detection, calls the Langflow flow
search/app/             retrieval microservice — configurable QA→RAG cascade
langflow_flows/         exports of the active ingestion flows (import: scripts/import_flows.py)
                        — component code lives embedded in the flow JSON (single source)
postgres/init_clean/    consolidated baseline a clean install boots from (schema + config seed)
postgres/init/          numbered migration history — not executed, applied by hand to live DBs
nginx/                  edge (routing, auth, rate limits) — plain HTTP, TLS terminates in front
                        — .htpasswd/auth_keys outside git
scripts/bootstrap.py    clean clone -> running stack, one idempotent command
scripts/create_api_key.py  Langflow API key over REST (the CLI cannot, see ONBOARDING §5)
scripts/import_flows.py import the 6 flows into Langflow (idempotent, PATCH-if-exists)
scripts/smoke.py        verify a running stack — no OpenAI cost, cron-safe
docs/                   SETTINGS_REFERENCE.md, DATA_MODEL_AND_SETUP.md, ai/, architecture/
```

---

## License

Copyright 2026 **EDU-AI, o.p.s.**, IČO 01674846. Licensed under the **Apache
License 2.0** — see [LICENSE](LICENSE) and [NOTICE](NOTICE), which carries
the attribution notices required by §4(d) of the License.

The work was commissioned and funded from a state subsidy by EDU-AI, o.p.s.

Third-party components (all containers: Langflow, PostgreSQL + pgvector,
pgAdmin, nginx, FastAPI stack, …) are listed with licences and authors in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Anyone redistributing this work, or a derivative of it, must keep the copyright,
patent and attribution notices and the `NOTICE` file (License §4). Most component
logic lives inside `langflow_flows/*.json` rather than in `.py` files, so every
component authored for this project carries a one-line header naming who last
changed its code and when — Langflow's built-in and derived components carry
Langflow's MIT notice as well (see NOTICE).

---

*Repo: `github.com/edu-ai-00/EDU-AI-asistent-RAG` · Contact: info@edu-ai.eu*
