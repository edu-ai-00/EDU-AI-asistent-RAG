# tools/lab/regression — regression set for the curated-QA search

> **Status: lab tool.** It runs inside the search container against the live
> database, but in one transaction that is rolled back; it writes nothing to the
> database. The cases and the results are private data (a school's questions) —
> keep them outside the repository, e.g. in a private directory such as `$EDU_AI_STATE_DIR/regression/`.

A search change is accepted or rejected by numbers, not by single examples: the
same cases are run before and after, and the cases that got worse are listed.

| Script | Runs | Does |
|---|---|---|
| [`build_cases.py`](build_cases.py) | host | builds `cases.json` from the school's curated questions and sets of written test queries whose right answer is known; `--extra` adds hand-approved cases |
| [`run.sh`](run.sh) / [`run.py`](run.py) | search container | puts the curated questions in as temporary packs, runs every case through prepare → qa_exact → qa_lexical, writes a verdict per case |
| [`compare.py`](compare.py) | host | baseline × now: score per set, cases that got worse (new confident wrong answers first), CSV of the changes; exit code 1 on a new confident wrong answer |
| [`check_step.sh`](check_step.sh) | host | check after every step: tests, docs-check, parity, the regression set against the accepted baseline, git state (see [METHOD.md](METHOD.md) §4) |
| `make_negatives.py`, `make_clicks.py`, `make_d_template.py`, `make_equiv_candidates.py` | host / container | prepare the sets N, C, D and candidates for equivalent answers (see Sets and principles) |
| `make_log_set.py` | host | set L: real wordings from the log with a weak label (the OpenAI choice) |
| `build_index.py`, `content_check.py` | host | **check of new content**: turns CSV packs into a list of questions, runs the whole regression set against the baseline, prints stolen answers and attracting records, prepares a table for test questions (see [METHOD.md](METHOD.md) §6) |
| `make_gap_report.py` | host | overview of content gaps for the curator (Excel): topics, counts of queries without an answer, keywords and the nearest answer offered today; **without whole user queries** |
| `eval_semantic_variants.py` | search container | the model's ranking: score from the question only × with the answer text × without nearly identical buttons, hit@1/3/4 without thresholds; rejects `D:holdout` |
| `eval_first.py` | search container | success of the FIRST answer (confident answer, otherwise the first button): today, the model alone, the ceiling, re-ranking of candidates by the model, a rule with a threshold; rejects `D:holdout` |
| `eval_rank.py` | search container | offline estimate: what ranking the offer by the sum of weights of shared words would give (policies fill / lex2 / replace, threshold) and how many extra buttons out-of-content questions would get |
| `mine_word_pairs.py` | search container | mines candidates for one-word synonyms from the training questions (unknown word → word of the school's question) and writes a what-if SQL; verified on the held-out ones |
| `analyze_errors.py` | search container | error analysis: sorts unseen and confidently wrong cases into groups (A, B, D:train only), with the share the local model would find |
| `eval_semantic.py` | search container | phase 3: tests how many right answers a local model would add to the offer (policies fill / lex2 / rrf, threshold τ) and how many buttons it adds to out-of-content questions; rejects `D:holdout` |
| [`scoring.py`](scoring.py) | both | verdict and score (stdlib only, unit-tested in `tests/test_lab_regression.py`) |

## Case

`{id, set, pack, entry_key, query, category}`. `entry_key` is the curated answer
that must come back. `null` means no confident answer is right (ambiguous
question, or the answer is missing from the data): the offer "Nechtěl ses
zeptat…?" is correct, a confident answer is the failure.

Score per case: confident right **+1**, right one in the offer **+0.5**, nothing
or the offer without the right one 0, confident **wrong −3** (it says nothing
like "Nechtěl ses zeptat…?", the asker takes it for the answer).

## Use

```bash
# 1. once: the cases (A = written paraphrases, B = an independent set of other authors)
python3 tools/lab/regression/build_cases.py --questions otazky_idx.json \
    --set A=prepisy.json --set B=heldout.json --out cases.json

# 2. the baseline of the code as it is now
tools/lab/regression/run.sh otazky_idx.json cases.json base.json

# 3. after a change (code in this checkout, a setting, or SQL tried in the transaction)
tools/lab/regression/run.sh otazky_idx.json cases.json now.json \
    --config '{"query_repair": ["typo"]}' --with-sql what_if.sql
python3 tools/lab/regression/compare.py --cases cases.json --baseline base.json --now now.json --out diff.csv
```

The search code of this checkout is copied into the container, so an experiment
that is not in the image yet can be run. `--with-sql` (repeatable) is applied FIRST
and the active setting is read afterwards, so a migration that changes the
settings is measured exactly as written; `meta.qa_lexical` in the result file
holds the values the run used. `--config` overrides `qa_lexical` params on top,
for the run only.

Method and measurements of the search-quality work (goal, principles, rule for accepting a change, findings):
[METHOD.md](METHOD.md).

## Sets and principles (training × test)

| Set | What it is | Use |
|---|---|---|
| **A** | 1,470 written paraphrases of the school's 210 questions | **training**: tuned on |
| **B** | 1,240 paraphrases of a different style | **validation**: already used for decisions, counts as "consumed" |
| **D** | questions that a person writes from the *answers* (see `make_d_template.py`) | **test, frozen**: never tuned on, final verification only |
| **N** | questions outside the school's content (`make_negatives.py`) | no confident answer expected; training and held-out half |
| **D:train / D:holdout** | halves of D by school question | `train` may be used to choose between variants (e.g. models), `holdout` only at the end |
| **L** | real queries from the log that production resolved through OpenAI (`make_log_set.py`); label = its choice | test only (weak label, selection-biased); not tuned on |
| **C** | queries from the log where the user clicked an offer *instead of an answer* (`make_clicks.py`) | orientation only (17 cases), biased labels |

1. **Split by the school's questions, not by sentences.** Each of the 210 curated questions is deterministically `train` (70 %) or `holdout` (30 %), `build_cases.split_of`. The paraphrases of A and B come from the same questions, so a split by sentence would let tuning leak into the measurement. Tuning is on `train` only; the final number is taken from `holdout`. `compare.py --by-split` shows the two halves separately.
2. **Frozen sets (D, `holdout` and N) are not used for decisions**, only for the final verification. As soon as a decision is made on a set, it is "consumed" (B already is).
3. **Noise:** `compare.py` prints a confidence interval of the score change for every set (paired bootstrap). A change whose interval contains 0 is noise and does not count as an improvement.
4. **Equivalent answers:** records with a nearly identical answer are proposed by `make_equiv_candidates.py`, a person confirms yes/no and `build_cases.py --equiv-csv` takes them as equally right.
5. **Ledger of attempts:** every accepted and rejected attempt is written into [LEDGER.md](LEDGER.md) with the numbers.

**Corrections of wrong labels** after inspecting the results (a "no answer" question for which the school has an answer) are made with a `--fixes` file (`drop` / `relabel`) and written into [LEDGER.md](LEDGER.md); the results are not adjusted by that.

```bash
# all sets at once (the files with queries stay outside Git)
python3 tools/lab/regression/build_cases.py --questions otazky_idx.json \
    --set A=prepisy.json --set B=heldout.json --extra clicks.json --negatives negatives.json \
    [--d-csv sada_D_vyplnena.xlsx --d-map sada_D_mapa.json] [--equiv-csv shodne_odpovedi_kandidati.xlsx] [--fixes sada_fixes.json] --out cases.json
```

## Limits

- Cases only cover what the school's curated questions are written from; a
  question nobody wrote an answer for is not a case until it gets `entry_key: null`
  or an answer through the curator's decision (the queue of logged questions).
- A written set measures how well search copes with *those* paraphrases. Hold
  one set (B) back when tuning, otherwise the numbers only describe the set.
- Production decisions of the curator (queue of logged questions) are not wired
  in yet: add them through `--extra`.
- Set C is tiny: in the log most offers stand BESIDE a confident answer (related
  questions); only a click on an offer that replaces the answer is a label.
