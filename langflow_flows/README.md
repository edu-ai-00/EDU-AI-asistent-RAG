# Langflow Flow Exports

JSON exports of the edu_ai production Langflow flows (as of **2026-07-07** (Langflow 1.10.1, Postgres-backed)).
Import via Langflow UI → Projects → Import, or `POST /api/v1/flows/`.

> **No secrets inside.** Components resolve credentials at runtime from
> **Langflow Global Variables** (`POSTGRES_PASSWORD`, `OPENAI_API_KEY` —
> referenced by name via `load_from_db`) or from the container environment
> (`LANGFLOW_API_KEY` for the agent's APIRequest headers). After import,
> create the two global variables (Settings → Global Variables, type
> Credential) — see `../ONBOARDING.md`, step 7. Flow/component **IDs** are
> retained (identifiers, not secrets) so cross-flow calls resolve.

## Pipeline overview

```
FastAPI /v1/ingest
   ├─ pdf    → EDU_AI_LOAD_PDF       (markitdown + pdfplumber → LLM judge → heading
   │                                   normalizer → save .md → POST to MARKDOWN flow)
   ├─ office → EDU_AI_LOAD_OFFICE    (→ markdown → normalizer → save .md → POST)
   └─ web    → EDU_AI_LOAD_WEB_PAGE  (→ markdown → normalizer → save .md → POST)
                        │
                        ▼
          EDU_AI_LOAD_MARKDOWN  (shared ingestion: read .md → split into
                                 page/table elements → table metadata →
                                 chunk → embed → Postgres/pgvector)

Q&A:  AI_AGENT_2026_04_01_API_version
        └─ calls child flows: vector_search, keyword_search,
           table_search, document_metadata_search (RRF ranking)
```

## ingestion/
| file | flow id | purpose |
|---|---|---|
| `EDU_AI_LOAD_PDF.json` | `04b6ce5b-dd70-436a-ba4a-395bb0a5d4a2` | PDF extraction (dual-path + LLM judge + `MarkdownHeadingNormalizer`) |
| `EDU_AI_LOAD_OFFICE.json` | `323ed9c4-6a6a-4c57-b6f8-8579d315ce29` | Office (docx/pptx/xlsx) extraction |
| `EDU_AI_LOAD_WEB_PAGE.json` | `d0b07325-05c3-4d9b-ab44-c925f8546f19` | Web page extraction |
| `EDU_AI_LOAD_MARKDOWN.json` | `c3dddc81-d595-400b-8b39-6b03ec2b36b7` | Shared markdown ingestion (`MdFileRead` → `MarkdownSectionSplitter` → table metadata → chunk → embed → Postgres) |

Component code lives embedded inside the flow JSONs (each node's
`template.code.value`) — that is the single source of truth. Edit it in the
Langflow UI or via the API, then re-export the flow JSON.

## search/
Child flows called by the agent (4 nodes each):
`vector_search.json` (`6e85db5a`), `keyword_search.json` (`41ac481b`),
`table_search.json` (`967cd23a`), `document_metadata_search.json` (`b6674a00`).

## agent/
`AI_AGENT_2026_04_01_API_version.json` (`f8e886c2`) — Q&A retrieval orchestrator;
loops over the search child flows and merges results (RRF).

## After import — checklist
1. Create the Langflow Global Variables `POSTGRES_PASSWORD` and
   `OPENAI_API_KEY` (Settings → Global Variables, type Credential) — the
   flows reference them by name.
2. Extraction flows read `LANGFLOW_FLOW_ID` (target ingestion flow) and
   `LANGFLOW_API_KEY` from the container environment — see `.env.example`.
3. FastAPI needs `LANGFLOW_FLOW_ID_PDF/_OFFICE/_WEB` pointing at the imported
   flow ids (unchanged if you import with IDs preserved).
