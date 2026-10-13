# tools — index

Manual helpers for development and debugging. They are not part of the application runtime or of the installation, and the runtime images do not contain them. The exact
way to run each file is in its docstring; this is only an overview. Operational scripts are in [scripts/](../scripts/README.md),
the documentation check is in [docs/tools/](../docs/tools/docs_check.py) (`python docs/tools/docs_check.py`).

| Path | Purpose | Note |
|---|---|---|
| [lab/](lab/README.md) | lab backup and restore of an isolated synthetic stack | verified only for the stated SHA-256; never on production or against someone else's stack; [OPERATIONS_LINUX.md](../docs/OPERATIONS_LINUX.md), section 5 |
| `stopwords/` | export and loading of the stoplist (`qa_stopword`) and recomputation of synonym forms (`qa_synonym`) | run via `docker exec -i edu_ai_search python - < …`; historical record of the procedure and reasons: [2026-09-05_stoplist_and_normalize_3.md](../docs/deployments/2026-09-05_stoplist_and_normalize_3.md) |
| `measure_related_chips.py` | comparison of two related-suggestion settings on real questions from the log | calls the nodes directly, writes nothing, no LLM runs; context: [qa-fastpath.md](../docs/ai/qa-fastpath.md) |
| `report_chip_repeats.py` | what the suggestions actually did, read from `question_answer_log` | read-only; `tools/` is attached to the `edu_ai_search` image via `-v` |
| `search_test.html`, `upload_test.html` | manual development pages for `/v1/ask` and document upload | opened from disk and call `127.0.0.1:8090` and `:8080`; from another origin `CORS_ORIGINS` must be set locally ([.env.example](../.env.example)) |
| `test_inputs/pipeline_input_o_studiu.json` | sample input of the ingestion pipeline (anonymized) | nothing in the tracked tree refers to it |

The paths stay fixed: documentation, code comments and deployment procedures refer to them.
