# tools/lab/regression — regression set for the curated-QA search

> **Status: lab tool.** It runs inside the search container against the live
> database, but in one transaction that is rolled back; it writes nothing to the
> database. The cases and the results are private data (a school's questions) —
> keep them outside the repository, e.g. in `~/edu-ai/state/regression/`.

A search change is accepted or rejected by numbers, not by single examples: the
same cases are run before and after, and the cases that got worse are listed.

| Script | Runs | Does |
|---|---|---|
| [`build_cases.py`](build_cases.py) | host | builds `cases.json` from the school's curated questions and sets of written test queries whose right answer is known; `--extra` adds hand-approved cases |
| [`run.sh`](run.sh) / [`run.py`](run.py) | search container | puts the curated questions in as temporary packs, runs every case through prepare → qa_exact → qa_lexical, writes a verdict per case |
| [`compare.py`](compare.py) | host | baseline × now: score per set, cases that got worse (new confident wrong answers first), CSV of the changes; exit code 1 on a new confident wrong answer |
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
that is not in the image yet can be run. The run uses the active setting of the
entity; `--config` overrides `qa_lexical` params for the run only.

## Limits

- Cases only cover what the school's curated questions are written from; a
  question nobody wrote an answer for is not a case until it gets `entry_key: null`
  or an answer through the curator's decision (the queue of logged questions).
- A written set measures how well search copes with *those* paraphrases. Hold
  one set (B) back when tuning, otherwise the numbers only describe the set.
- Production decisions of the curator (queue of logged questions) are not wired
  in yet: add them through `--extra`.
