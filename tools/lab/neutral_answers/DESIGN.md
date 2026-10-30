# Neutral answers — what the tool does and how it is built

*Technical description of how to run it: [README.md](README.md).*

## 1. What the tool is for

A curated answer has to fit **any wording of the question** and **whoever is asking** (pupil, parent, teacher).
It should not start with "Ano/Ne" (yes/no), should not address one person informally, should not speak in the first person, and for "kde najdu…" (where do I find…) the source of the answer should lead to the place.

For every answer the tool **proposes a neutral wording by fixed rules** (without a language model). It shows the original and the proposed wording side by side, and the curator approves, rejects or edits the proposal by hand. It also shows a **machine view**: how the search sees the text of the questions and answers (lemma, stem, stop-words, a lost negation).

## 2. Functions

| Area | Description |
|---|---|
| Input: Excel file in the QA pack format | columns `canonical_q`, `answer`, `sources`, `Alternativa 1…n` |
| Input: "free" sheet | question in column A, answer in column G (the columns are fixed) |
| Input: CSV QA pack in the application format | export from the QA portal (`/v1/qa-export`); `entry_key`, `cat`, `category_code`, `domains`, `is_active` are taken over |
| Rules | typography, smileys, "by jste", first person, informal → formal address, "Ano/Ne" at the start, role words → "uchazeč" with verb agreement, missing link (a warning only: a source without an address or with only a home page; the address is not written into the text, the chat displays the sources itself), "nevím" → advice on where to ask further |
| Table | questions and alternatives, original and proposed answer with differences marked, rule labels, status |
| Decisions | approve / reject / edit by hand / revert, bulk approval of automatic changes |
| Machine view | norm / lemma / stem, removed stop-words, warnings (negation, diacritics, unknown words) |
| Storage | decisions are stored outside Git, a change of the answer in the source is detected |
| Export | overview of changes (CSV) and the whole QA pack in the application format (CSV); new wording only for approved items |
| Queue of logged queries | queries from operation without a confident answer, grouped by topic (see 6) |
| Tests | `tests/test_neutral_answers.py` |

## 3. How it works

```
Excel / CSV ──► rules propose a neutral text ──► the curator approves / edits in the browser
                                                   │
             file with the decisions (a state directory outside Git) ◄─┘
                                                   │
                         export CSV ──► upload of the QA pack to the system (scripts/upload_qa_packs.py / QA portal)
```

The application runs in a Docker container from the search image (`edu_ai_search`), so the machine view uses the same lemmatization as the assistant. It does not touch the database or the search.

## 4. Inputs

| Input | Format | Note |
|---|---|---|
| A set of questions and answers | `.xlsx` (first sheet or `--sheet`) or `.csv` | header `canonical_q, answer, sources, Alternativa…`, or question in A and answer in G |
| List of stop-words and stemmer settings | `contracts/parity/stoplist.json` | a snapshot from the library, not live data from the database |
| Neutral subject | parameter `--subject` | `uchazeč` (default) or `žák` |
| Form of address | parameter `--osloveni ty\|vy\|beze` | informal, formal, or leave the address alone |

## 5. Outputs

| Output | Where | Content |
|---|---|---|
| File with decisions | `<state directory>/neutral_answers/<workbook>.decisions.json` | status, manual edit, note, fingerprint of the original answer, time |
| Overview of changes | button "⇩ Přehled (CSV)" | all items: original and resulting wording, status, rules, warnings |
| QA pack | button "⇩ QA sada (CSV)" | the whole pack in the format of `search/app/qa_csv.py`; new wording only for approved items, the others in the original. From a CSV the columns of the pack are returned unchanged; from Excel `entry_key` stays empty (derived from the question) and `domains` empty (the areas from the upload are taken over) |

> Curated answers (QA) do **not reach the system through migrations**, but by uploading a QA pack. Migrations are only for settings: synonyms, stop-words, prompts.

## 6. Queue of logged queries

The query log (`question_answer_log`) is the most faithful picture of how people ask. Replaying the log (the tool `tools/lab/replay`) showed that a large part of the queries the assistant does not answer confidently have a flaw not in the search but in the data:

- **an alternative is missing:** the query ends only with an offer although a curated answer with a different wording exists,
- **the query is ambiguous:** it can be read two ways and should not be assigned to one answer,
- **the answer is missing altogether:** there is no curated answer for the query.

Changes of decision logic (an undecided order by coverage, the weight of a word regardless of negation) were measured and rejected: they worsened more queries than they fixed. The remaining cases are therefore handled through data.

### What the queue does

1. **Input:** the result of replaying a version (`tools/lab/replay`) and the QA packs of the schools (CSV from the portal or from a dump). The selected queries are those that ended **only with an offer** or **with nothing**.
2. **Grouping:** identical and nearly identical queries (same lemmas) are merged into one row with the number of occurrences and all wordings, sorted from the most frequent. Follow-up queries ("A kolik to stojí?") are left out: they make no sense without the conversation. With `--topics` the groups are further joined by topic: a decision is made once per group; inside a group a blue stripe joins queries the search treats as the same (the same method as QA: lemmas, synonyms, weight of words).
3. **Proposal:** for each group the answers the search offered, with coverage. The proposal comes only from the answers of the school the query belongs to.
4. **Choices for the curator:**
   - **add as an alternative** to the selected answer (the tool measures beforehand that it helps and does not break other questions),
   - **ambiguous:** it is not added anywhere, it is written down which answers the offer should contain,
   - **answer missing:** the query is put with the questions without an answer,
   - a query can also be moved to another or a new group, or postponed.
5. **Output:** a QA pack with the new alternatives (the same export as above), an overview `/export/queue.csv`, and a list of ambiguous queries and missing answers.

Modules: `log_queue.py` (grouping and proposal, with tests), `topics.py` (groups by topic), the card `/queue.html` (started with `--queue`).

### Data protection

The users' queries stay only in the local state directory; they do not get into Git or the export as text, except those the curator approves as an alternative.

## 7. Known limitations

- **Agreement in more complex sentences:** the rules replace "dcera" with "uchazeč", but do not fix "byla nemocná". These changes are marked "zkontrolovat" (check). A full solution would need a tool that can generate word forms (e.g. MorphoDiTa; its models are licensed for non-commercial use only).
- **Short answers after removing "Ano/Ne"** ("Můžete."): the rule only warns, the curator has to rephrase.
- **A free sheet has fixed columns** (question in A, answer in G).
- **Rule tables are in the code:** role words, verb forms and templates are not in a data file.
- **The machine view uses a snapshot of stop-words**, not live data from the database (for Czech they are the same today).
- **Two instances at once** need a different port and container name (`NEUTRAL_ANSWERS_PORT`, `NEUTRAL_ANSWERS_NAME`).
- **No login:** the tool is available only from the own machine (`127.0.0.1`).

## 8. Files

| File | What it contains |
|---|---|
| [`run.sh`](run.sh) | running in a Docker container |
| [`server.py`](server.py) | web server, storing decisions, exports |
| [`rules.py`](rules.py) | the rules of neutral rewriting |
| [`machine.py`](machine.py) | the machine view (lemma, stem, warnings) |
| [`xlsx_read.py`](xlsx_read.py) | reading Excel without additional libraries |
| [`static/index.html`](static/index.html) | the web page |
| [`log_queue.py`](log_queue.py) | queries from operation without a confident answer, grouped |
| [`topics.py`](topics.py) | groups by topic and queries the search treats as the same |
| [`static/queue.html`](static/queue.html) | the card Queries from operation |
| [`../../../tests/test_neutral_answers.py`](../../../tests/test_neutral_answers.py) | tests |
