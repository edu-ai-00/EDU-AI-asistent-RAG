# Deployment: stoplist, eduai_normalize 3.0.0, synonym fix

> **Historical record (5 Sep 2026).** The commands, image tags, SHAs and production state apply only to the state at that time; do not use them as a current procedure. The statuses "init_clean does not carry the new columns" and "prompt v15 runs outside the repo" have been superseded: migrations 211 and 212 are in the repo and part of the B216 baseline. The current nginx configuration is `nginx/conf/nginx.conf` (Compose mounts the `nginx/conf` directory); the older `nginx/nginx.conf` does not replace it.

**For:** Jan Drozd
**From:** Martin Safránek
**Branch:** `chunking-config`, commit `3cc33ec`
**Written:** 5 Sep 2026, 23:05

---

## What this is verified against

| what | when | note |
|---|---|---|
| dump of the production DB | **5 Sep 2026 22:40:41** | 58 967 653 B, `pg_dump` custom |
| last `git fetch` to the laptop | **5 Sep 2026 23:01:07** | |
| Honza's last commit on this branch | **4 Sep 2026 18:28** | `1949487` — contained in my branch |
| my `HEAD` | **5 Sep 2026 23:05** | `3cc33ec` |

Older dumps in case of a rollback: `2026_09_04` (4 Sep 08:39:33) and
`edu_ai_2026_09_03` (3 Sep 16:44:33).

I tried all the steps below **on a restored copy of that 22:40 dump**, not
only in my own environment. Where I give an expected number, it is the number measured on that
copy.

---

## What has changed on the server since the last export

Compared column by column and row by row against the dump from 5 Sep 22:40:

* **structure unchanged** — 483 columns on the server, none that I would not have
* **no new packs** — `qa_pack_manifest` 38, `qa_entries` 563,
  `qa_variants` 7094, `qa_embedding` 7094
* **no new documents** — `document_metadata` 45, `document_chunks` 1348
* `settings`, `qa_synonym`, `qa_blocklist`, `code_list`,
  `grade_term`, `subject_term`, `ui_label`, `taxonomy_rules`, `ingestion_log` are also identical

Only two things differ outside the query log, both yours:

* `system_message` — the server has 8 more rows (`no_answer_missing_topic`
  and `not_a_question`, each in 4 languages) from migrations 206–208, which I do not
  have locally
* `prompt_registry` — the server has `search_answer_generation` **v15** active
  (created_by `codex`, 4 Sep 17:25), I have v14. **It is left alone.**

---

## Before you start

Production **does not have migrations 209 or 210**. Verified: `qa_stopword` on the server has only
`term, lang, is_active, note, created_at, scope` and the PK is already
`(lang, term, scope)`.

**Do not deploy 209** — migration 211 does the same thing better (three booleans instead of one
`form` column) and cancels it. 211 accounts for that: the block that removes `form` is
conditional on its existence. Without that condition the whole migration would fail and even the populated data
would be rolled back.

**210 yes**, but only at the end — step 5.

Take a dump before you start.

---

## 1) Migration 211

It must run **before** the code: `settings.py` already reads `term_norm`.

```bash
docker cp postgres/init/211_stopword_blocklist_reduced_forms.sql edu_ai_postgres:/tmp/
docker exec -i edu_ai_postgres psql -U edu_ai -d edu_ai -v ON_ERROR_STOP=1 \
  -f /tmp/211_stopword_blocklist_reduced_forms.sql
```

Expect `COMMIT`. Check — it must give **680 / 680 / 680**:

```bash
docker exec -i edu_ai_postgres psql -U edu_ai -d edu_ai -c \
 "select count(*), count(*) filter (where term_norm<>''), count(*) filter (where term_stem<>'') from edu_ai.qa_stopword;"
```

The migration adds `term_norm/term_lemma/term_stem` and `use_norm/use_lemma/use_stem`
to `qa_stopword`, the missing `term_stem` and the flags to `qa_blocklist`, and populates
them itself (823 UPDATE statements with computed values).

## 2) Build BOTH images

`eduai_normalize` 3.0.0 is a breaking change. One side on the old library means that
the index and the query reduce differently.

```bash
docker compose build edu_ai_search edu_ai
docker compose up -d edu_ai_search edu_ai
docker exec -i edu_ai_search python -c "import eduai_normalize as e;print(e.__version__)"
docker exec -i edu_ai       python -c "import eduai_normalize as e;print(e.__version__)"
```

Both must report `3.0.0`.

## 3) Flow import

**First export those 6 flows from the server and compare them with the repo.**
`import_flows.py` overwrites **all six**, not just the two that changed, and Langflow
keeps no history — `flow_version` is empty. Whatever was edited only in the server UI
and is not in the repo disappears irreversibly.

```bash
python3 scripts/import_flows.py
```

Only a single component changed: `ChunkLemmatizer-qa003-QA` in `EDU_AI_QA`. It had two
silent escape paths — without the DB password it returned `(None, True, True)` and an exception during
loading it swallowed the same way. The library then slipped in the built-in stoplist. That no longer
exists, so it would fail a bit further on and incomprehensibly; now it refuses right away.
The markdown components had this right already before, only a single comment line changes in them.

## 4) `qa_synonym` fix

Data, not schema. First without `--apply`, so you can see what:

```bash
docker cp tools/stopwords/recompute_synonym_forms.py edu_ai_search:/tmp/rs.py
docker exec -i -e PG_PASSWORD=$POSTGRES_PASSWORD edu_ai_search python /tmp/rs.py
docker exec -i -e PG_PASSWORD=$POSTGRES_PASSWORD edu_ai_search python /tmp/rs.py --apply
```

Expect **27 rows to fix** (measured on a copy of production). The script takes `PG_DB`,
so it can be run as a dry run against a restored copy before it touches the live DB.

Why: 27 of the 100 active rows have stored forms that do not match what the
code computes today, and `qa_common.py:100` reads them on every query. Seventeen
German and Spanish ones were **never stemmed** — they were seeded with `cfg=None`
and the `_DEFAULT_ENGINE` table in the code had no entry for `de`/`es`, so
`engine_name` returned empty and the word passed through unchanged (`donde` → `donde` instead of
`dond`). The stored stem then never met the query one.

## 5) Migration 210 — separately, after the rest

```bash
docker cp postgres/init/210_qa_exact_form_stem.sql edu_ai_postgres:/tmp/
docker exec -i edu_ai_postgres psql -U edu_ai -d edu_ai -v ON_ERROR_STOP=1 \
  -f /tmp/210_qa_exact_form_stem.sql
```

Expect `UPDATE 4` (four active `settings` rows).

It does two things: it writes out into the configuration the default that has so far applied silently
(`["norm","lemma"]` from `qa_common.py:21`), and adds a third form — `stem`.

**It is the only step that truly changes the search behaviour**, hence last:
if something got worse, you know why. It can be reverted with a single UPDATE back to
`["norm","lemma"]`.

Measured on 482 unique real queries: 260 already hit `qa_exact` today via
norm|lemma, via stem **exactly one** would be added („kdo resi neomluvenu absenci"),
and that one is answered by `qa_vector` today anyway with a score of 0.9759. Among the 2 092
variants there are 7 groups of collisions and all 7 are diacritic pairs of the same question, where the
match on the root is correct.

---

## What must NOT change

The stoplist stays **cs 155, de 111, en 112, es 86**. `term_norm` is character for character
what is in `term` today — `fold("během")` gives `behem`, which is exactly the old
value. What changed is the column that is read, not the content.

Verified by a deterministic test: 39 QA-answered questions from
`question_answer_log` after loading the last pack, replayed through `/v1/ask` —
**39 of 39 hit the same entry**. Two runs of the same test give the same result (0 differences),
because a question answered from QA never reaches generation.

Beware of testing via the corpora in `tests/eval_*_questions.txt`: they go through the
`answer` step, i.e. through the model, and are **not reproducible** — two runs on an
unchanged state gave me a different result for 5 of 32 questions. A/B cannot be measured on them.

---

## Rollback

* `postgres/init/211` is transactional — if it fails, it changes nothing
* I have the table backups locally: `qa_stopword_backup_20260905`,
  `qa_synonym_backup_20260905`
* 210 is reverted with a single UPDATE
* the build is reverted by switching to the previous image tag

## What remains open

* **`postgres/init_clean/` does not carry my new columns** — the consolidated init is
  behind `postgres/init/211`
* locally I am missing your migrations **206–208** (8 messages); I have the files, I just
  have not run them
* prompt **v15** runs on the server outside the repo as a reversible experiment
