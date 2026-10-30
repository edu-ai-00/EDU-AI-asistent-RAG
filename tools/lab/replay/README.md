# tools/lab/replay — replay logged questions on two code versions

> **Status: lab tool.** It reads a COPY of a database (e.g. a production
> dump restored into an isolated container) and writes results outside the
> repository. Never point it at a live installation: `reform.py` rewrites rows.

The question log (`question_answer_log`) is the most realistic test set there
is. These scripts send every logged question through the search cascade of a
given code version — the endpoint's own preparation and
`run_search(stop_before="answer")`, so nothing is written and no answer is
generated — and compare two versions question by question.

| Script | Runs | Does |
|---|---|---|
| [`replay.py`](replay.py) | inside a search image | one JSON line per logged question: the curated entry it answered with, or the document chunks it gathered. The language model and the embedder fail at once (no key needed). |
| [`reform.py`](reform.py) | inside a search image | recomputes the stored norm/lemma/stem forms of QA variants and document chunks with that image's normalizer — what a re-ingest would do, on a copy |
| [`compare.py`](compare.py) | on the host | categories (same answer, other answer, QA only before/after, documents same/other top chunk) and a CSV of the changed questions with the texts side by side |
| [`explain_one.py`](explain_one.py) | inside a search image | one question's QA decision: concepts, weights, coverage, margin |

## A fair comparison

Compare **old code on data ingested by the old code** with **new code on data
ingested by the new code**: a normalization change moves the stored forms too,
and new queries against old forms measure a mismatch nobody will deploy.

1. Restore the dump into an isolated PostgreSQL (`--network` an internal
   Docker network, `shared_preload_libraries=pg_search`), as two databases
   (`CREATE DATABASE … TEMPLATE …`).
2. `reform.py` with the old image on the first copy, with the new image (plus
   any new migration) on the second.
3. `replay.py` with each image on its copy, then `compare.py OLD NEW`.

The database connection comes from `PG_HOST`, `PG_USER`, `PG_PASSWORD`,
`PG_DATABASE`; mount this directory read-only and set
`PYTHONPATH=/app:/replay`. Follow-up questions are replayed without their
conversation; questions the production answered by vector search are not
reproduced without a key.

Measured with it on 2026-10-22 (4 442 logged questions): eduai_normalize
3.3.0 + migration 217 never chose a DIFFERENT curated entry than before; two
tempting decision-logic changes (coverage tie-break, polarity-neutral IDF)
were measured and rejected — see
[`docs/deployments/2026-10-21_normalize_3_3.md`](../../../docs/deployments/2026-10-21_normalize_3_3.md).
