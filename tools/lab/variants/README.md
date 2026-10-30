# tools/lab/variants — retrieval measurement on rule-based question variants

> **Status: lab tool.** It inserts a QA workbook into the database **inside one
> transaction that is rolled back**, so nothing is persisted and no other
> connection ever sees the rows. Results are written outside the repository.

From each question of a QA workbook, deterministic rules (no LLM) build
variants and every variant is sent through the **real** curated-QA nodes:
`prepare` → `qa_exact` → `qa_lexical`, with the active setting's step params,
the live synonyms and stop-words. The measurement says, per technique, how
often the right curated answer still comes back.

| Code | Technique | Expected |
|---|---|---|
| B0 | the original question | exact match, 100 % (a check of the harness) |
| B1 | synonym substitution (uchazeč → syn/dcera/dítě/žák/student, přijímačky ↔ přijímací zkouška, matematika → matika, …) | the right answer |
| B2 | the whole question without diacritics | the right answer |
| B3 | one typo per variant: i/y, a dropped letter, two letters swapped (seeded, reproducible) | the right answer |
| B4 | negation flipped (smím ↔ nesmím, lze ↔ nelze, …) | **negative test**: the original answer must *not* come back as a confident answer |

`qa_vector` (embeddings) and the document path are not measured: they need the
OpenAI key and documents. The rules are data in [`generators.py`](generators.py).

## Run

```bash
tools/lab/variants/run.sh path/to/qa_pack.xlsx
tools/lab/variants/run.sh path/to/qa_pack.xlsx --limit 5 --debug
# the effect of a new migration, without applying it
tools/lab/variants/run.sh path/to/qa_pack.xlsx --with-sql postgres/migrations/217_synonyms_meeting_groups.sql
```

`--with-sql FILE` (repeatable) runs the SQL inside the measuring transaction
before the questions are asked, so it is rolled back with everything else. It
connects as `edu_ai_migrator` (`POSTGRES_MIGRATOR_PASSWORD`) and acts as
`edu_ai_owner` via `SET LOCAL ROLE`, the rights a migration runs with.

`run.sh` copies the tool and the workbook into the running `edu_ai_search`
container and runs [`measure.py`](measure.py) there. The search role is
read-only, so the rows go in as the QA ingest role (`POSTGRES_DB_USER` /
`POSTGRES_PASSWORD` from `$EDU_AI_CONFIG_DIR/.env`, passed by environment,
never printed). Options: `--sheet NAME`, `--limit N`, `--entity CODE`
(default `SVOBODNA_SKOLA_PRAHA`), `--debug`.

Outputs in `$EDU_AI_STATE_DIR/variants/` (default `variants/` in the state directory):

- `<workbook>.variants.csv` — one row per variant: technique, rule, the
  variant, outcome, decision, coverage, margin, the question found and the
  norm / lemma / stem tokens the query was reduced to (`;`-separated, UTF-8 BOM
  for Excel)
- `<workbook>.summary.json` — counts per technique and per rule

Tests: `python3 tests/test_lab_variants.py` (generators only, stdlib).
