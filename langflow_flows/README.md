# Langflow ingestion and export flows

The current versioned source contains six ingestion/export flows. Retrieval runs
in `search/`, not historical Langflow `search/` or `agent/` folders.

| File | Endpoint alias |
|---|---|
| [ingestion/EDU_AI_EXPORT_QA_PACK.json](ingestion/EDU_AI_EXPORT_QA_PACK.json) | `edu-ai-export-qa-pack` |
| [ingestion/EDU_AI_LOAD_MARKDOWN.json](ingestion/EDU_AI_LOAD_MARKDOWN.json) | `edu-ai-load-markdown` |
| [ingestion/EDU_AI_LOAD_OFFICE.json](ingestion/EDU_AI_LOAD_OFFICE.json) | `edu-ai-load-office` |
| [ingestion/EDU_AI_LOAD_PDF.json](ingestion/EDU_AI_LOAD_PDF.json) | `edu-ai-load-pdf` |
| [ingestion/EDU_AI_LOAD_WEB_PAGE.json](ingestion/EDU_AI_LOAD_WEB_PAGE.json) | `edu-ai-load-web` |
| [ingestion/EDU_AI_QA.json](ingestion/EDU_AI_QA.json) | `edu-ai-load-qa` |

Component code is embedded in each JSON's template code; it is not duplicated in Python files.
Bootstrap prepares connectivity variables and imports missing aliases with
`import_flows.py --missing-only`, preserving every existing flow. Explicit
`import_flows.py` PATCHes existing aliases and overwrites manual edits. For a targeted
A-to-B update use `flow_rollout.py`; it refuses divergence from the expected source.

Use [the clean installer](../docs/CLEAN_INSTALL.md) and [script index](../scripts/README.md).
Old manual instructions about missing search/agent flows and manually recreating global
variables are historical. Provider credentials belong in runtime/Bifrost, outside Git.
