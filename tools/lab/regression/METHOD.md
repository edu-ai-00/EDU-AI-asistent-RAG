# Answer-search quality without an OpenAI key — method and measurements

*Technical description of the measuring tool: [README.md](README.md). Every attempt with its numbers: [LEDGER.md](LEDGER.md).*

## 1. Goal and principles

**Goal:** the assistant finds the right curated answer as often as possible **without an OpenAI key**. The existing `qa_vector` and `rag_vector` (vectors from OpenAI) stay idle without a key, so there is no search by meaning, no documents through embeddings and no generated answers; a QA answer is returned verbatim and relies on words alone.

**Principles of the work:**

1. **No new hand-written answers or alternatives.** An improvement must not depend on someone writing more wordings of a question.
2. **No large change to the original code.** Changes are an optional step or parameter, off by default, small and reversible.
3. **Measure before proposing; a change to the decision logic is always approved first** (`AGENTS.md`, "Ask first").
4. **Guard against overfitting:** whatever came out of analysing some data is verified on other data (set B, the real log).
5. **A confident error is worse than no answer.** Score: confident right +1, right in the offer +0.5, confident wrong −3.
6. **Real data:** the copy of the production log may be used by the tools locally only. Query texts are not printed outside, not stored in Git and not put in documentation; only counts and summaries leave.

## 2. Measurements so far

### Settings changes (migrations 218–221, manifest `R221`)

| Change | What it does |
|---|---|
| 218 | politeness and filler words do not count towards coverage (21 words) |
| 219 | QA searches by lemma only, not by stem |
| 220 | typo repair in the query (`query_repair: ["typo"]`) |
| 221 | `qa_lexical` pulls 200 candidate variants before the aggregation to entries (`fetch_k` 50 → 200) |

An offer with 5 buttons (migration 221) was tried and withdrawn: the number of buttons stays at the original **3**.

### Regression set (QA only; written queries and logged wordings, not real traffic)

State with migrations 218–220 and the original 3 buttons. "Sees" = confident, in the offer, or in the fallback (the nearest questions that the chat shows at a dead end). Percentages for D count the 174 questions that have an answer.

| Set | The user sees the right answer: before → after | Score after | Confident errors after |
|---|---|---|---|
| A (1,470 paraphrases) | 84 → 83.7 % | 834.0 | 10 |
| B (1,240, independent authors) | 73 → 74 % | 469.5 | 8 |
| D (independent, written from the answers) | 44 → 45 % | 77.5 | 5 |
| L (85 logged wordings, weak label) | 39 → 40 % | −5.5 | 7 |

State after migration 221 (`fetch_k` 200), the migration itself in the transaction:

| Set | Sees the right answer | Score | Confident errors |
|---|---|---|---|
| A | 85.4 % | 847.0 | 10 |
| B | 76.1 % | 487.0 | 8 |
| D (174 with an answer) | 51.1 % | 79.0 | 5 |
| L | 45.9 % | −3.0 | 7 |

Overall 1,818.5 → 1,853.0 (interval +26 … +43.5), 72 cases better, 3 worse, no new confident wrong answer.

Confidently right answers grew by about 4 points. The written sets A and B do not show a gain in "sees"; the gain is small and only visible on D and in the replay of the log.

### What was tried and does not help

| Attempt | Result |
|---|---|
| stem only instead of lemma | more errors in the log (pořadí → dohled) |
| colloquial endings ("testama") | overfitting (A +10, B +1.5) |
| tie rule by coverage difference (`coverage_gap`) | score down, log +7/−9 |
| `coverage_reverse`, `stem_backoff` | more errors / worse than lemma |
| thresholds `t_answer`, `m_margin` | today's 0.60 and 0.08 are nearly optimal; 0.55 adds +8 confident errors on A |
| `t_suggest` 0.2 | gain only on paper (the fallback is shown to the user anyway), not in "sees" |
| searching the answer text too | answers are short (~138 characters); only 58 of 276 missed cases share a word, 18 would be in the top 5 |
| synonyms uzpůsobení/český | neutral on independent data; the +20 gain came from the same queries (a data fix, not an improvement) |

### Where the ceiling of word-based search is

On set B: 21 % confidently right, another ~56 % in the offer or fallback, **about 23 % remain** where the right answer is not visible. The group "different" (different words, same meaning) makes up 17 % of the set and word rules cannot solve it. In addition, ~8 of the 19 "confident errors" are substantively acceptable swaps of neighbouring questions (a measurement error, not a chat error).

### A local model

Model `intfloat/multilingual-e5-small` (471 MB, `sentence-transformers` is already in the repository). Vectors of 1,063 wordings and 2,710 queries in 33 s on CPU.

| Set | Right from the model alone: 1st place / top 5 | Today (sees; 5-button stage) | Today + 3 from the model | Today + 5 from the model |
|---|---|---|---|---|
| B | 50 % / 71 % | 77 % | 85 % | 87 % |
| A | 67 % / 85 % | 86 % | 95 % | 96 % |

As a decider of a **confident** answer the model is not enough (B: at a strict threshold 129 right / 5 wrong against today's 261 / 9). It suits as a supplement to the offer. These numbers assume up to 8 buttons; see 5.4 for 3 and 4.

## 3. Rule for accepting a change

A change is accepted only if **everything** holds:

1. Regression set: the score on **B** grows (differences within ±5 points are noise), on A it does not drop noticeably.
2. **No new confident wrong case** that is not a substantively acceptable swap (`compare.py` ends with exit code 1).
3. **Replay of the real log** (`tools/lab/replay`, production copy): no confident answer gets worse.
4. The change is off by default, has a test and is in the documentation (`SETTINGS_REFERENCE.md`, `make docs-check`).
5. What changes the schema, the embedding model, the cascade order or adds a dependency is approved beforehand (`AGENTS.md`).
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
| tests in the search image | negation lemmas, stop-words, diacritics, typo repair — on the code of this checkout, not of an old image |
| normalization parity | `contracts/parity` (`AGENTS.md`: before and after a normalization change) |
| regression set (`--regression`) | against the accepted baseline `accepted.json`, with migrations 218–221 in a transaction; fails on a new confident wrong case |
| git state | what is uncommitted and unpushed, containers left over after a measurement |

The script ends with code 0 (`ALL CHECKS PASSED`) or prints what failed. The accepted baseline (`accepted.json`) is overwritten only after a change that was approved.

## 5. Findings

### 5.1 What limits the result

Three things bound the best achievable result, and no tuning changes them:

1. **Missing content.** In an analysis of the queue of logged queries (22–24 Oct), for 2,218 of 2,240 queries without a confident answer even the old version did not answer, because the answer is missing in the data. Answer selection does not help with that.
2. **Ambiguous queries and nearly identical questions** (who is entitled × how to apply). For some of them there is no single right answer.
3. **The numbers from sets A and B are optimistic** (written queries, and B has already been used for decisions). The real gain has to be verified on data that was not tuned on.

The independent set D (written by a person from the answers) showed that the written sets A and B **overstated** reality:

| Indicator | A (training) | B (validation) | **D (independent)** |
|---|---|---|---|
| the user sees the right answer (confident, in the offer, in the fallback) | 84 % | 74 % | **45 %** |
| confidently right | 41 % | 21 % | **13 %** |
| confidently wrong | 0.7 % | 0.7 % | **2.9 %** |
| out-of-content questions that get a confident answer | N: 1 of 444 | | BEZ: 0 of 47 |

Reason for the difference: the questions in D do not copy words from the answer, so they are closer to how people really ask. For short, colloquial questions (`otazka_2`) the user saw the right answer in only **39 %** and for normal sentences in 58 % (measured with the 5-button offer of the withdrawn migration 221; with 3 buttons both are lower).

### 5.2 The basis of measurement

| Weakness of a plain written set | How it is handled |
|---|---|
| **Questions outside the content are missing.** We do not know whether the assistant avoids answering confidently where no answer exists (the worst kind of error). | **Set N (negative):** questions to which the school QA has no answer (publicly collected questions after a quick pass, so that none the school QA can answer is included). Expectation: no confident answer (`entry_key: null`). 445 questions; 443 without a confident answer, 2 for manual review. |
| **B has already been used for decisions**, so it is not fully independent. | **Frozen set D:** questions written by a person from the *answers* (221: 174 with an answer, 47 "BEZ"). Never tuned on, used only for the final verification. Two wrong labels were corrected through a fixes file (`--fixes`, recorded in the ledger). |
| **Pairs of nearly identical questions count as an error.** | **Groups of equivalent answers:** pairs of records with a nearly identical answer are proposed automatically, a person confirms yes/no (40 pairs: 6× yes, 31× no, 3 empty). The regression set then takes either of them as right; 4 cases improved. |
| **We do not know what is noise.** | **Confidence interval:** a bootstrap and a paired test on every comparison. A difference below the noise limit does not count as an improvement. |

**Set C from the real log (weak labels).** A click on a button is not recorded (`selected_answer_key` is empty), but the chat sends the text of the clicked button as the next question, so it can be traced indirectly. Most offers in the log stand *beside a confident answer* (related questions) and a click on them is not the right answer to the first query. Only `suggest` (25) and `no_answer` (116) records have a real offer *instead of an answer*. That gave **17 usable clicks**, too few for statistics, so set C is only an orientation check.

**Principles for working with the sets** (details in [README.md](README.md), "Sets and principles"):

- **Training × test:** A is for training, B for validation (already consumed), D and the negative set N are frozen.
- **Split by school question** (70 % train / 30 % holdout), not by sentence, because both A and B are paraphrases of the same 210 questions.
- **Tune on `train` only**, the final number comes from `holdout` and from D.
- **Noise** is evaluated with a bootstrap for every change.

**Set L from the real log:** queries that production resolved by search by meaning through OpenAI, with its choice as a *weak label* (`make_log_set.py`; 85 distinct wordings, asked 262×; 30 with a strong match ≥ 0.75, 55 borderline). **Selection bias:** a query gets into this set only if the word search failed, so L is by nature hard queries and does not represent all traffic. The label is not the truth: of the 7 "confident errors", about half differ only by a nearby question.

| L (85 wordings), 5-button offer | Today | with the local model (lex2, τ 0.88) |
|---|---|---|
| the user sees the right answer | **48 %** | **66 %** (+18 points) |
| confidently right | 5 % | unchanged |
| confidently wrong | 8 % (7), about half a nearby question | unchanged |
| the model alone: right in top 3 / top 5 | 62 % / 74 % | |

On real wordings the gain from the model is **larger than on the written sets** (+18 against +7). With the original 3 buttons the same set gives 40 % today and 61 % with the model (see 5.4). It is a small set of weak labels, so it shows a direction, not an exact number.

**Replay of the real log with migrations 218–221 (4,442 queries, production copy, no key), measured with the 5-button offer of the withdrawn migration 221:**

It is evaluated against the **production choice** as a weak label (queries that production resolved from QA, 3,403 rows). The label is biased: the old code agrees with itself, so a *change* of behaviour looks like a disagreement. Log rows are weighted by the number of repetitions, so the distinct wordings are given as well.

| Version | the user sees the production answer (confident or in the offer) | confident answer different from production |
|---|---|---|
| production code (`main`) | 48.4 % | 11.7 % |
| after migration 217 | 48.4 % | 11.8 % |
| **218–221** | **48.8 %** | **11.5 %** |

- **Overall nothing got worse or better** (agreement 48.4 → 48.8 %).
- **Queries that production resolved through OpenAI (447 rows) see the right one 6 points more often**: 28.6 → 34.7 % (confident 0.4 → 3.8 %). These are real wordings that the word rules did not catch before.
- **Queries resolved by word search (211 rows): −7 points** (72.0 → 64.9 %). Behind it are **one wording asked 13× (a confident answer changed to a different confident one)** and **one wording 12× (a confident answer changed to an offer)**. That is two different wordings out of 211 rows, so it is about volume, not a general problem.
- **Transitions between the state after 217 and 218–221** (rows, distinct wordings): confident different → nothing 31 / 7; nothing → in the offer 30 / 8; confident same → confident different 13 / 1; confident same → in the offer 12 / 1; nothing → confident different 7 / 2. The changes in favour outweigh the changes against.
- **Documents:** for 8.2 % of the queries the first snippet differs from the production code, but that is an effect of the new normalization 3.3.0, not of migrations 218–221 (compared with the state after 217 the snippets agree in 99.8 %). The quality of documents is not measured.

### 5.3 Error analysis

The tool `analyze_errors.py` puts every case where the user **does not see the right answer** (neither in the offer nor in the fallback), or gets a **confidently wrong** one, into one group. Only **A, B and D:train** are analysed; `D:holdout` is not used. A word is "topical" when it is not general (a lemma in more than 5 % of questions, e.g. "škola", "žák", "dítě"). Result (521 cases, measured with the 5-button offer of the withdrawn migration 221, so with 3 buttons somewhat more cases fall into the groups):

| Group | A | B | D:train | Total | Share | Right in the local model's top 5 |
|---|---|---|---|---|---|---|
| unknown word (colloquial forms, derivations: "reparát", "matika", "třídnické") | 54 | 77 | 14 | 145 | 28 % | 48 % |
| no common topical word (different words, e.g. "zlomila prst" × "úraz") | 56 | 69 | 15 | 140 | 27 % | 40 % |
| one common topical word (the rest of the query dilutes the match) | 71 | 103 | 5 | 179 | 34 % | 61 % |
| two or more topical words, nothing anyway | 9 | 27 | 0 | 36 | 7 % | 67 % |
| confidently wrong: nearly identical question (data, not an error) | 2 | 4 | 1 | 7 | 1 % | 86 % |
| confidently wrong: another topic (a general word leads elsewhere) | 8 | 5 | 1 | 14 | 3 % | 86 % |

**What follows:**

1. **Most missed cases are not "a different meaning" but word search being confused by the form or length of the query.** Unknown forms and long queries where one topical word is lost among the others make up 62 % together.
2. **The local model would offer the right answer in more than half of them** (weighted over the 521 cases: about 53 %). The rest (~47 %) is not saved even by it, so that is the ceiling for the offer alone.
3. **There are few confidently wrong answers (21 of 521, 4 %)** and a third of them is a substantively acceptable swap. Confident errors are not the main problem; the main problem is that the right answer is missing from the offer.
4. **A table of forms for unknown words does not generalise.** Of 370 distinct unknown words, only 3 pairs repeat the same partner (≥ 3 cases): a long tail of one-off words. A looser table (37 pairs) improved the training questions, but on the held-out questions the difference is noise (A:holdout +2, B:holdout −0.5, D:train −0.5) and 5 confident errors were added.

### 5.4 Semantic offer with a local model

The idea: complete the offer "Nechtěl ses zeptat…?" also where the words do not match. **Confident answers do not change.** The model is `multilingual-e5-small`; the measurement uses A, B, `D:train` and N, never `D:holdout`.

It measures how often the user sees the right answer (confidently, or among at most 5 buttons). Three ways of bringing the model into 5 buttons (threshold τ = the lowest similarity from which the model offers a button):

| Way | A (today 86.5 %) | B (today 77.3 %) | D:train (today 53.8 %) | out-of-content questions with a button from the model |
|---|---|---|---|---|
| **fill**: the model only fills the free places, τ ≤ 0.84 | 91.0 | 81.8 | 60.3 | 60–66 % |
| **lex2**: the first two word buttons stay, the model adds three more, τ = 0.88 | 93.4 | 83.1 | 60.3 | **22 %** |
| **lex2**, τ = 0.84 | 93.7 | 82.8 | **66.7** | 91 % |
| **rrf** (merging the rankings), τ = 0.88 | 93.4 | 84.3 | 60.3 | 22 % |

- **The gain with 5 buttons is +6 to +7 points on A and B and +6.5 to +13 points on `D:train`**, depending on the threshold.
- **The price of the threshold:** a lower τ raises the gain, but the model then adds a button to almost every out-of-content question (τ = 0.84: 91 %). Today's system shows at least one button for **69 %** of such questions (average 2.5). τ = 0.88 with the `lex2` policy keeps most of the gain and the model speaks up for out-of-content questions in only 22 %.
- **Costs:** 11 ms per query, model load 8–10 s, peak memory ~1.1 GB (the whole process with torch).
- **Threshold on both halves of N (τ = 0.88, `lex2`):** training 24.5 %, held-out 17.6 %, so it does not behave worse on the held-out half.

**With 3 and 4 buttons (the original number is 3):** policy `lex2` (the first two word buttons stay, the rest by meaning), threshold τ = 0.88; "today" is today's state with 3 buttons:

| Number of buttons | A (today 83.7 %) | B (today 74.0 %) | D:train (today 51.3 %) | L (today 40.0 %) | out-of-content questions with a button from the model |
|---|---|---|---|---|---|
| **3** (`lex2`; 2 word + 1 by meaning) | **90.3 % (+6.6)** | **79.1 % (+5.1)** | **56.4 % (+5.1)** | **61.2 % (+21.2)** | ~23 % |
| 4 (`lex2`) | 92.3 % (+8.6) | 81.5 % (+7.5) | 57.7 % (+6.4) | 64.7 % (+24.7) | ~23 % |

The gain with **3 buttons is almost the same as with 5**, because today's word buttons in 3rd to 5th place are weak anyway. So the semantic offer **brings roughly +5 to +7 points even with 3 buttons** and +21 on the real wordings from the log (L).

**Comparison of two models** (`paraphrase-multilingual-MiniLM-L12-v2` has a different similarity scale, τ 0.30–0.70, so they are compared at the same rate of buttons for out-of-content questions, around 20 %, policy `lex2`):

| Model | alone in top 5 (A / B / D:train) | A | B | D:train | time per query | peak memory |
|---|---|---|---|---|---|---|
| **multilingual-e5-small** (τ = 0.88) | 85.1 / 71.5 / 53.8 | **93.4** | **83.1** | **60.3** | 9–11 ms | ~1.1 GB |
| paraphrase-multilingual-MiniLM-L12-v2 (τ ≈ 0.65) | 82.7 / 68.0 / 50.0 | ~91 | ~80.5 | ~57.7 | 13–35 ms | ~1.45 GB |

e5-small is better on all three sets (by ~2.5 points at the same rate of buttons), faster and less demanding on memory.

**A stronger model (`multilingual-e5-base`):** alone it is better (right answer in top 5: A 85 → 91 %, B 72 → 77 %), but after merging with today's buttons (3 buttons, `lex2`, τ 0.88) it adds only about **+2 points on A and B** and **nothing on D:train and L** (56.4 → 56.4 %, 61.2 → 60.0 %). It is 3× slower (32 ms against 9.7 ms) and needs 360 MB more memory, so the size of the model is not the bottleneck. Adding the **text of the answer with a small weight (0.25)** makes the model's ranking 2 to 3 points better on A and B, for the first answer about +1 point.

**Not implemented.** The step would need a change to `executor.py` (the list of cascade steps) and a new flag in the settings, that is, a change to the core. Measurement, model choice, policy and threshold are done and recorded here.

## 6. Adding new content without making things worse

New or changed answers can **steal a confident answer** from old records (sibling questions, similar topics) and they change word weights. The sets can measure that:

1. **Prepare packs.** A directory with the school's CSV packs **as they will be after the change** (old QA + new or changed), in the format of the QA portal export.
2. **Lint the alternatives** (`alt_lint.sh PACKS_DIR`): an alternative that brings no new lemma to its entry changes nothing for `qa_lexical`, which covers a query against the union of the entry's lemmas.
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
| All figures except the log replay and set L come from written queries, not from operation | the replay of the log is part of the rule for accepting a change (part 3) |
| Production lemmatizes with an older simplemma | after normalization 3.3.0 is deployed all lemmas change and the data must be uploaded again (see the deployment note) |
| QA does not filter by user role | not addressed here |
| The ceiling is set by the content | answer selection does not replace missing answers (part 5.1) |
