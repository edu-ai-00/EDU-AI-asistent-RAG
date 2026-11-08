# Answer-search quality without an OpenAI key — method

*How a change to the curated-QA search is measured and accepted. The tool: [README.md](README.md). Every attempt with its numbers, also the rejected ones: [LEDGER.md](LEDGER.md). Operation without a key and its parameters: [offline QA rules](../../../docs/integrations/offline-qa-rules.md).*

## 1. Goal and principles

**Goal:** the asker **sees the right curated answer** (confident, among the three offered questions, or among the three questions of the fallback "Nenašel jsem přesnou odpověď") as often as possible **without an OpenAI key**. Without a key `qa_vector` and `rag_vector` stay idle and no answer is generated; a curated answer is returned verbatim and found by words alone.

1. **Measure before proposing.** A change is measured on the regression set, the gold set of real wordings (G) and, where it matters, a replay of the real log.
2. **Everything new is off by default.** A code change is a parameter or a step that does nothing until a setting turns it on; a setting changes through a migration with a release manifest. This holds for the core (`executor.py`) too.
3. **A confident error is worse than no answer.** Score: confident right +1, right in the offer +0.5, confident wrong −3.
4. **Guard against overfitting.** Tune on the train halves; the held-out halves, set D and set N are for verification only.
5. **New content is checked, not trusted.** New answers and alternatives go through `alt_lint` and `content_check.py` (§6) and are measured batch by batch.
6. **Real data stays private.** The copy of the production log is used by the tools locally only; query texts are never printed outside, stored in Git or written into documentation.
7. **Document every change** (`docs/SETTINGS_REFERENCE.md`, the migration header, a ledger row), and keep the repository free of one-off scripts: an analysis script is removed once its result is in the ledger.

## 2. Current state

| Change | What it does | Status |
|---|---|---|
| 217 | synonym groups from the meeting | applied |
| 218 | politeness and filler words do not count towards coverage | applied |
| 219 | QA matches the lemma only, not the stem | applied |
| 220 | `query_repair`: a typo in the query becomes the one word of the curated questions within edit distance 1 | applied |
| 221 | `fetch_k` 200: more candidate variants before the aggregation to entries | applied |
| 222 | `offer_ngram`: the last offered question by character n-grams | prepared, not applied |
| — | `rank_ties`: ties in the ranking settled instead of left to the row order | parameter, off in operation; always on in the lab |
| 223 | `skip_without_key` (answer step): without a key no generation is tried, the honest no-answer instead of `llm_unavailable` | prepared, not applied |

Accepted baseline (state 218–221, the lab with `rank_ties`), the asker sees the right answer: A 85.6 %, B 76.5 %, D 50.0 % (174 questions with an answer), L 42.4 %, G 60.5 %; score 1,856.0; confident wrong 31. With 222: D 55.7 %, L 51.8 %, G 69.1 %, confident wrong unchanged.

## 3. Rule for accepting a change

A change is accepted only if **everything** holds:

1. The score on the regression set grows beyond the bootstrap interval of `compare.py`, on the train halves first; the held-out halves and the gold set agree on the direction.
2. **No new confident wrong case** that is not a substantively acceptable swap (`compare.py` ends with exit code 1).
3. **Replay of the real log** (`tools/lab/replay`, production copy) where the change can move confident answers: none gets worse.
4. The change is off by default, has a test and is documented (`SETTINGS_REFERENCE.md`, `make docs-check`).
5. What changes the schema, the embedding model, the cascade order or adds a dependency is approved beforehand (`AGENTS.md`); a model is used only with a licence that covers its training data.
6. Settings change through a migration in `postgres/migrations/` with a release manifest (`migrate.py gen-release` on an isolated database), never by hand.

## 4. Check after every step

After every step (migration, code change, measurement, new tool) run:

```bash
tools/lab/regression/check_step.sh              # after any step
tools/lab/regression/check_step.sh --regression # also after a change to search, settings or data (~2 min)
```

| Check | What it guards |
|---|---|
| Python compilation | no syntax error in the code, tools, tests |
| tests on the host | regression set, variants, neutral answers, lint and fingerprint of migrations |
| `make docs-check` | links, sources and headers of the documentation |
| tests in the search image | negation lemmas, stop-words, diacritics, typo repair, the n-gram chip, the order of tied entries — on the code of this checkout, not of an old image |
| normalization parity | `contracts/parity` (`AGENTS.md`: before and after a normalization change) |
| regression set (`--regression`) | the regression set and the gold set G against their accepted baselines, with migrations 218–221 in a transaction and `rank_ties` on; fails on a new confident wrong case |
| git state | what is uncommitted and unpushed, containers left over after a measurement |

The script ends with code 0 (`ALL CHECKS PASSED`) or prints what failed. The accepted baseline (`accepted.json`) is overwritten only after a change that was approved.

## 5. Findings

The numbers behind each point are in the ledger.

1. **Where the right answer is lost.** In more than half of the missed cases the right entry shares a content word with the query and is only ranked below the three offered questions (an order problem); in the rest it shares no word at all (a vocabulary problem that only content or meaning can solve).
2. **What helped:** lemma only (219), politeness words out of the coverage (218), typo repair (220), more candidate variants (221), and the n-gram chip (222) for derived and colloquial forms.
3. **What did not help** and is not to be retried without a new reason: stem only, colloquial endings, tie rules on coverage, reverse coverage, other thresholds, mined synonym pairs, bulk alternatives, a confident answer by a single wording, more confident answers when two signals agree.
4. **Content is the largest lever.** Most queries without a confident answer ask about a topic the school has no answer for, or word an existing answer very differently.
5. **The written sets overstate reality.** A and B are paraphrases of the curated questions; D (written from the answers), L and G (real wordings) are lower and are the ones to judge by.
6. **A local embedding model** would add the right answer to the offer in more cases still. The model measured (`multilingual-e5-small`) lists MS MARCO, whose licence excludes commercial use, among its fine-tuning data; no model is used until a licence covering the training data is confirmed.

## 6. Adding new content without making things worse

New or changed answers can **steal a confident answer** from old records (sibling questions, similar topics) and they change word weights. The sets can measure that:

1. **Prepare packs.** A directory with the school's CSV packs **as they will be after the change** (old QA + new or changed), in the format of the QA portal export.
2. **Lint the alternatives** (`alt_lint.sh PACKS_DIR`): an alternative that brings no new lemma to its entry changes nothing for `qa_lexical`, which covers a query against the union of the entry's lemmas. The flag `union_cross` shows where the growing union newly covers a variant of another entry; it only sorts what to look at (it ranks the entries that later stole answers with AUC 0.66–0.71), the decision is the regression set below. Adding many alternatives to existing entries raised the confident errors (LEDGER, 3 Nov): write few, and measure every batch.
3. **Run the check:**
   ```bash
   python3 tools/lab/regression/content_check.py --packs-dir DIR [--sweep]
   ```
   The script converts the packs to a list of questions (`build_index.py`, the old `idx` stay), runs the whole regression set (A, B, C, D, L, N) with the deployed settings and compares with the accepted baseline. It prints:
   - the score before and after by set,
   - **how many times a new record stole a confident right answer** and **how many times it answered an out-of-content question confidently**,
   - **which new records attracted the most foreign queries** (to rewrite or merge),
   - with `--sweep` also the thresholds `t_answer` and `m_margin` around today's values.
   It ends with code 1 when there is a stolen answer or a confident out-of-content answer (limits `--max-stolen`, `--max-n-confident`).
4. **Decide on the attracting records:** make the wording of the question more precise, mark substantively identical pairs as equivalent (`build_cases.py --equiv-csv`), or accept the record if it really is the better answer.
5. **Test questions for the new records.** The check prepares `testovaci_otazky_nove.xlsx` (a person writes questions from the answers as for set D); after it is filled in, `build_cases.py --d-csv … --d-map …` adds them to the sets.
6. **After acceptance:** regenerate `otazky_idx.json` (`build_index.py --like`), a new baseline `accepted.json`, write it into the ledger and **upload the whole QA again with the current image** (normalization 3.3.0), so that old and new records have the same forms.
7. **Go through the thresholds again** (`--sweep`) when the content grows by more than a fifth.

**Tried on a simulation:** adding 65 records from another school (similar topics, different answers) worsened the score from 1,835 to 1,715 and the confident errors from 31 to 61; the check found 6 stolen confident answers and listed the records that attract foreign queries the most. The simulation is deliberately unfavourable (substantive duplicates), but it shows that the check does uncover the risk. Changing the thresholds does not save the damage: at `t_answer` 0.55 to 0.65 and `m_margin` 0.04 to 0.12 the score is 1,683 to 1,708 (today's setting 1,704), so 0.65 lowers the confident errors (61 → 50) but loses 113 confidently right answers. It has to be solved through content (make wordings more precise, merge duplicates), not thresholds.

## 7. Limits of the measurement

| Limit | Note |
|---|---|
| Most figures come from written queries | the gold set G and the log replay carry real wordings; G is small (81 cases), so differences below 5–10 points are noise there |
| The lab measures with `rank_ties` on | operation without it may settle a few ties differently after the packs are uploaded again |
| No documents in the measurement | the regression set covers the curated answers only; with documents in scope the cascade without a key reaches the generation, which fails and returns `llm_unavailable` (about a quarter of the logged questions, see the ledger, 8 Nov) unless migration 223 is applied |
| Production lemmatizes with an older simplemma | after normalization 3.3.0 is deployed all lemmas change and the data must be uploaded again (see the deployment note) |
| Other languages | measured for Czech only |
