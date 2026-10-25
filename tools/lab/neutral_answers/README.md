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
tools/lab/neutral_answers/run.sh ~/Downloads/pack.csv        # a QA pack CSV (portal, /v1/qa-export)
```

A `.csv` is read as the application reads a QA pack: delimiter `,` or `;`, the
same header aliases, variants and sources one per line. Its `entry_key`, `cat`,
`category_code`, `domains` and `is_active` are kept and written back unchanged.

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
- **QA sada (CSV)** — `/export/pack.csv`: the **whole** set in the QA pack
  layout of [`search/app/qa_csv.py`](../../../search/app/qa_csv.py) (comma,
  CRLF, UTF-8 BOM). Only an approved item carries the new wording; every other
  one (unchanged, rejected, not yet reviewed) keeps its original, so no
  unreviewed rewrite is uploaded and no entry drops out of a pack replaced on
  upload. The button and the file name (`…-pack-ceka-N.csv`) say how many
  proposals still wait. From a workbook, `entry_key` and `domains` are left
  empty on purpose: the reader derives a stable key from the question, and an
  empty `domains` inherits the upload's areas (`[]` would clear them).
  `category_code` is written only when the input CSV had the column (an absent
  column keeps an entry's category, an empty cell clears it).

## Address (`--osloveni`)

`--osloveni vy` (default) turns tykání into polite vykání, `ty` turns vykání
into tykání, `beze` leaves the address alone. Certain forms go by table
(kontaktujte → kontaktuj, nemáte → nemáš, jste se → ses); a pronoun whose
singular depends on case or gender (vám → ti/tobě, vaše → tvoje/tvá), generic
verb endings and plural participles (abyste přišli → abys přišel) are offered
for a check. A rewrite approved for another address becomes "jiné oslovení"
and waits for a new approval; until then the pack keeps the original answer.

## Questions from the log (`--queue`)

```bash
python3 tools/lab/neutral_answers/log_queue.py REPLAY.jsonl LOOKUP.json --entity SCHOOL --out queue.json
tools/lab/neutral_answers/run.sh pack.csv --queue queue.json
```

[`log_queue.py`](log_queue.py) groups the logged questions a replay
([`tools/lab/replay`](../replay/README.md)) left without a confident curated
answer — same school, same lemmas, most frequent first — with every wording,
what the search offered, how the production answered, and the curated entry
that answered it before if that one is switched off now. With `--queue` the
app shows them at `/queue.html` ("Dotazy z provozu"): the curator adds a
question as an **alternative** to an answer or marks it **missing an
answer** (the earlier "ambiguous" and "skip" choices were dropped from the page;
the server still reads such decisions). Nothing is preselected: a well covered offer can still be wrong.
Decisions go to `<workbook>.decisions.queue.json` next to the answer
decisions; the queue holds users' questions and stays outside Git.

### Topics (`--topics`)

```bash
python3 tools/lab/neutral_answers/topics.py queue.json --manual temata.json \
    --synonyms syn.txt --stopwords stop.txt --out topics.json
tools/lab/neutral_answers/run.sh pack.csv --queue queue.json --topics topics.json
```

The tab shows **topics** ("skupiny") instead of single groups, and the curator
decides once per topic. [`topics.py`](topics.py) starts from a hand-made split
(`--manual`, `{"topics": {group key: {"area", "title"}}}`, outside Git); a group
it does not know joins the most similar topic, else "Nezařazené". Without
`--manual` (or without `--topics` at all) the topics are the subgroups below.

Inside a topic, a blue bar joins the groups **the search itself treats as one
question**: the reduction of the QA cascade (lemmas, `coverage` stop-words,
synonym groups, IDF-weighted coverage, each side covering ≥ 0.6 of the other).
Measured on one log: 458 groups → 342, 93 % of the merged pairs belong
together; different words with the same meaning stay apart. A topic's
alternative therefore adds the most frequent wording of each bar (or every
wording, when ticked).

"Missing an answer" for a whole topic keeps the topic together; for a single
question it takes the question out of its topic, listed on its own ("Dotazy
vyjmuté ze skupin", first in the "Chybí odpověď" view); ↺ on it returns it to
the topic it came from.

A question or a whole topic can also be assigned to ANY answer of the school's
packs (every pack CSV in the input's folder; an answer the search offered is
paired with it by pack and question text): type a few words in "Přiřadit
odpověď…" and pick. A question assigned this way leaves its topic ("Dotazy
přiřazené k odpovědi"). The last item of the list writes a NEW answer
(question, text, pack, source); it is stored with the decisions, can be
assigned to further questions and edited, and `/export/queue.csv` carries its
text (`nova_odpoved`) until the pack export writes it as an entry.

**Warnings (⚠, tile "Upozornění")**, computed before the export: the same
wording added to two answers; a wording another answer already has; a wording
closer to another answer's question than to its own (similarity of the search's
reduction ≥ 0.6, lemmas when the normalizer is there) — the chat may answer
with that one; a written answer without a source, with a home page only, very
short, or with no question assigned. The review tab warns about a rewrite
approved unchanged although it had spots to check, approved despite a flag, or
very short.

Per group the curator can move it to another topic or a new one, mark it as
missing an answer (which also takes it out of its topic, into "Dotazy vyjmuté
ze skupin"), or take it out of the topic for later ("Odložené"). Topics of
one's own come first, the one added to last on top. The row
stays in place, struck through and with an undo button, until the next click
elsewhere. Moves, topics of one's own and topic decisions are stored in the
same decisions file; `/export/queue.csv` lists every group with its topic,
decision and the wording it adds.

`syn.txt` and `stop.txt` are exports of `qa_synonym` / `qa_stopword` (lang
`cs`), e.g. `psql … -At -F'|' -c "SELECT group_id, kind, term_norm,
term_lemma, term_stem FROM edu_ai.qa_synonym WHERE is_active AND lang='cs'"`
and `"SELECT scope, term_norm, term_lemma FROM edu_ai.qa_stopword WHERE
is_active AND lang='cs'"`. Without them the lemmas alone are compared.

## Tests

```bash
python3 tests/test_neutral_answers.py
```
