# tools/lab/neutral_answers — review app for neutral QA answers

> **Status: lab tool.** It reads a QA workbook and writes review decisions to a
> local JSON file. It never touches the database, the index or a QA pack that
> is already uploaded; the exported CSV goes through the normal QA pack upload.

Status, inputs/outputs still to build and the plan for questions that need a
manual answer: [PLAN.md](PLAN.md) (Czech).

A curated answer must fit **any** phrasing of its question and **any** asker
(pupil, parent, teacher). The app proposes a neutral rewrite of each answer by
deterministic rules (no LLM), shows the original next to the proposal with the
differences highlighted, and lets a curator approve, reject or edit it.

## Run

```bash
tools/lab/neutral_answers/run.sh ~/Downloads/faq_jpz_9-26.xlsx
tools/lab/neutral_answers/run.sh "$HOME/Downloads/Otázky EDU.xlsx" --sheet Přijímačkymaturita
```

Open <http://127.0.0.1:8770/> (published on localhost only). `run.sh` starts
`server.py` inside the `edu_ai_search` image, read-only and without
capabilities, so the machine view uses the same `eduai_normalize` and
`simplemma` as the search service. The stoplist and the engines come from
[`contracts/parity/stoplist.json`](../../../contracts/parity/stoplist.json), the
snapshot the parity vectors use; there is no database connection.

Options: `--sheet NAME`, `--subject uchazeč|žák` (the neutral subject),
`NEUTRAL_ANSWERS_PORT`, `NEUTRAL_ANSWERS_IMAGE`, `EDU_AI_STATE_DIR`.

## What it shows

| Column | Content |
|---|---|
| Questions | the canonical question and its alternatives (the variants that lead to the answer) |
| Original answer | as in the workbook |
| Proposed answer | word-level diff against the original; ✎ marks a manual edit |
| Rules | one tag per rule that fired: **auto** (mechanical), **check** (needs a person: Czech agreement), **⚠** (seen, not fixable) |
| Status | pending / approved / rejected, with ✓ ✕ ✎ ↺ |

The **machine view** (per row) lists the norm, lemma and stem tokens of every
question and of the proposed answer, the stopwords that are dropped, and
warnings: a negation the lemma loses (`nesmím → smět`), a query the stoplist
empties, text without diacritics, words the lemmatizer leaves unchanged.

Rules (`rules.py`): typography, smileys, `by jste → byste`, first person,
familiar *tykání* → polite *vykání*, a leading *Ano/Ne*, role words (*syn,
dcera, dítě* → *uchazeč*, with subject–verb agreement), a missing link for
"kde najdu…" questions, and a missing-information template instead of "nevím".
Polite *vykání* ("Co nejdříve kontaktujte školu.") is kept on purpose.

## Data and exports

Decisions: `$EDU_AI_STATE_DIR/neutral_answers/<workbook>.decisions.json`,
outside the repository (QA packs are private data). A decision remembers the
answer it was made for; if the workbook changes that answer, the row is shown
as *changed in source* and goes back to pending.

- **Přehled (CSV)** — `/export/review.csv`: every item with original, final
  text, status, rules and warnings.
- **QA sada (CSV)** — `/export/pack.csv`: approved items in the QA pack layout
  of [`search/app/qa_csv.py`](../../../search/app/qa_csv.py). `cat` and
  `domains` are left empty; fill them before an upload.

## Tests

```bash
python3 tests/test_neutral_answers.py
```
