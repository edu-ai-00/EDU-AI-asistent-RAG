---
id: docs.ai.qa-fastpath
status: current
components: [qa, retrieval]
sources:
  - search/app/nodes/qa_exact.py
  - search/app/nodes/qa_lexical.py
  - search/app/nodes/qa_vector.py
  - search/app/nodes/qa_common.py
  - search/app/nodes/qa_ngram.py
  - search/app/nodes/query_repair.py
  - search/eduai_normalize/eduai_normalize/negation.py
  - search/app/nodes/route.py
  - search/app/scope.py
  - search/app/domain_scope.py
  - search/app/config.py
  - postgres/init/140_qa_pack_school_grades_domains.sql
  - postgres/init/144_qa_scan_surface_parity.sql
last_verified: 2026-11-07
---

# QA fast-path (curated answers)

**Track:** human (AI-relevant). The curated question/answer path that runs before
any document retrieval and returns author-written answers verbatim (no LLM).
Control types as in [routing.md](routing.md).

The historical discrepancy list in the [QA rules and offline audit](../integrations/offline-qa-rules.md)
records wording corrected on 2026-10-12. The older verification date is retained
because this review checked the affected claims, not every source on this page.
No cascade/scoring behaviour changed.

Record shape: [../../contracts/schemas/qa-record.schema.json](../../contracts/schemas/qa-record.schema.json)
(do not inline it here). Physically: `qa_entries` (answers) + `qa_variants`
(indexed phrasings, three reduced forms) + `qa_embedding` (optional vectors),
scoped by `qa_pack_manifest`.

All three stages fill `sources` from the curated entry's own citations — what the
**author** put in the pack's `sources` column, not retrieval provenance. Those
items carry only rank, title and URL, and nothing verifies the links. A non-empty
`sources` therefore does **not** imply a generated answer; `answer_source` is what
tells the two apart.

## Match stages

### qa_exact (`enforced`)

Order-sensitive string equality: for each configured `form` (default
`[norm, lemma]`), compare the space-joined query tokens to the variant's stored form. A hit
returns that entry's answer immediately with a match score of 1.0. Miss →
continue.

A hit also carries **related curated questions**: since the cascade stops here,
the runners-up are built by running `qa_lexical` for its suggestions only (its
answer and decision are discarded), so they pass that stage's floors and tiering.
Only its floored lists count — decision C's below-threshold entries never ride
along with a verbatim answer. The entry just answered is removed, and duplicates
are dropped on the question TEXT, because the same question may exist under
several entries (own school, organisation). Off with `related_suggestions: false`.

**Repeats:** a suggestion the same conversation already asked is never offered
back, and one already shown but not taken is demoted to the end rather than
dropped — see `search/app/conversation.py`. Applied at the response boundary, so
it holds for every outcome and every client; it needs a `conversation_id` on the
request and costs one indexed read when there is one.

**Empty lists are filled from the CATEGORY, not from lower-scoring candidates.**
When the ranking offers nothing above `t_suggest`, the chips come from other
entries a curator filed under the same `tags->>'category'` as the answered one,
**within that entry's own pack**, in the same entity/validity scope and compatible with the answered entry's school-grade IDs (or with no declared grades),
with pack, identity, validity and domain restrictions unchanged. Pack-local because a category name means what
its own pack's author meant: faq-provoz files report cards under `Hodnocení` and
retake exams under `Zkoušky`, and the per-school packs repeat 31 of 33 questions
verbatim, so matching a name across packs mixes topics and offers the same
question back under another entity's answer. With query concepts they are ordered by weighted coverage; without concepts by `entry_key`. Per-conversation dedupe rotates
them as a user works through a topic. Off with `related_category_fill: false`.
The alternative — offering the next-best SCORING candidates — was built, measured
and reverted: it filled every hole, but with chips like "Jak funguje další kolo
přijímacího řízení?" under "Jak funguje odhlašování obědů?". In a 100-entry pack
even a common verb ("funguje", "požádat") has a df share under 25 %, so the
rare-concept gate admits it; the shared word is grammar, not topic. An entry with
no category yields nothing — `faq-jpz` has all 110 blank, which is pack work.

**Historical relevance measurement** (2026-09-03,
80 real questions, `tools/measure_related_chips.py`). Raising `t_suggest` does not
work: a right and a wrong chip routinely score identically (0.3482 for both on one
query), so a floor cuts them at the same rate, and at 0.45 more than half the
answers lost every chip. An admission gate on a rare shared concept
(`related_admission: rare`) removes almost nothing at df share 0.25. Requiring the
query's MOST discriminative concept was tried and rejected — the most useful
related question is usually the sibling that differs in exactly that concept
("…test z matematiky" → "…z českého jazyka"), so it cut the best chips first.

### qa_lexical (`enforced`; thresholds `evaluated`, uncalibrated)

BM25-style ranking with synonym expansion, then an A/B/C decision:

- Build an OR tsquery from `{norm, lemma}` tokens expanded via `qa_synonym`
  (entity groups only; interrogative groups are NOT expanded — expanding them
  floods the OR-query and collapses the top1↔top2 margin). Rank active variants
  with `ts_rank_cd`, aggregate variants → entries.
- **coverage** = IDF-weighted share of the query's CONTENT concepts present in the top entry; a plain share is used only when no usable weights exist. **margin** =
  `(rank1 − rank2) / rank1`.
- Decision:

  | Branch | Condition | Result |
  |---|---|---|
  | **A** answer | `coverage ≥ t_answer` AND `margin ≥ m_margin` AND topical gate does not reject | curated resolved answer; step on_hit controls stopping |
  | **B-deferred** | `coverage ≥ t_suggest` (not A) | retain up to `suggest_top_n` suggestions and continue; present them only if later steps do not answer |
  | **C** fall | `coverage < t_suggest` | continue cascade |

- Thresholds resolve pipeline params → winning pack manifest → environment/code fallback, in
  that order. They are `evaluated` against the eval sets but **not calibrated** —
  do not treat them as tuned. Their values are in
  [../SETTINGS_REFERENCE.md](../SETTINGS_REFERENCE.md) §3.3, the canonical owner.
- **Typo fallback:** if the tsquery matches nothing, a trigram similarity search
  can produce **suggestions only**, never a confident answer, above a configured
  similarity floor.
- **Other-grade safety net:** a small
  number of matches from a *different* year may be offered, provided they are
  good enough relative to the best match. The answer then states which year the
  passage applies to. Other-grade suggestions can also accompany an answer, not only an empty own-year result, so a rule for ninth-graders is never presented to a
  second-grader as if it were theirs.

### Additions for operation without a key (`evaluated`)

Without an OpenAI key `qa_vector` and the document vectors stay idle, so the
lexical stage carries the curated answers alone. These additions are parameters
of the `qa_lexical` step, documented in
[../SETTINGS_REFERENCE.md](../SETTINGS_REFERENCE.md) §3.3; each is off until a
setting (a migration) turns it on, and each was measured with the regression set
in `tools/lab/regression` (method: `METHOD.md`, numbers: `LEDGER.md` there).

| Parameter | What it changes | Migration |
|---|---|---|
| `form: [norm, lemma]` | matches the lemma only, not the stem | 219 |
| `query_repair: ["typo"]` | a query word the dictionary of its language does not know (undecided query: the school's languages) becomes the one curated word within edit distance 1 | 220 |
| `fetch_k: 200` | more candidate variants before the aggregation, so an entry's union is complete | 221 |
| `offer_ngram` | the LAST offered question (offer or fallback) by character 3-5-grams, for derived and colloquial forms; confident answers and the chips beside an answer never change | 222 |
| `negation_conflict` (also on `qa_vector`) | no confident answer across opposite polarity ("nepřijde" × "přijde"); a negation is what the lemmatizer made one (eduai_normalize 3.4.0), compared on the entry's lemmas | 224 |
| `rank_ties` | ties in the ranking settled by the question text instead of the row order, so a re-upload of the packs cannot move them | 225 |
| `skip_without_key` (answer step) | without a key the generation is not tried; the question ends with the parked offer or `no_answer` instead of `llm_unavailable` | 223 |

The negation check (`negation_conflict`, row above) withholds the confident answer
when the query and the entry hold a word with opposite polarity. Until 10 Nov 2026
it was always on and read every folded word starting with "ne" as a negation;
it is now a parameter (migration 224) and counts only the negations the lemmatizer
made.

Known interaction: with a key, `qa_vector` and the generated answer reuse the
offer parked by `qa_lexical` as related questions, so `offer_ngram` can show its
chip beside those answers too. That is not measured (no key in the lab).

### qa_vector (`enforced`)

Runs after `qa_lexical` fell through and the document steps have gathered — the
last curated attempt before generation. It embeds the question with the active
registry model and cosine-searches the curated vectors. Above the configured
floor it returns that entry's curated answer; below it, the cascade continues to
the document vectors and then to generation.

## Scope / partitioning

Every QA query is filtered to the packs the caller may see. The scope is
resolved in `route` from the caller's legal entity, role and year together with
pack validity, and the same entity and visibility rules are applied through
`search/app/scope.py` as for documents. A caller entitled to no pack is given a
sentinel that matches nothing — an empty list would be read downstream as "all
active packs". `enforced`.

### Thematic areas (`enforced`)

When the request carries `domains`, a curated answer may only be returned — or
offered — if its own row carries one of the chosen areas. See
[routing.md](routing.md) for how a code becomes a set of ids and why the three
states (`None` / ids / empty) are not two.

**A pack label is a pre-selection; the ENTRY label is the filter.** `route`
drops packs whose manifest carries none of the chosen areas, but a pack may
legitimately be labelled for several — a mixed operations pack holds both a
catering and an attendance question — and passing it does not make its every
answer relevant. So every QA query also filters the **scanned row**:

| Stage | Scanned table | Column |
|---|---|---|
| `qa_exact`, `qa_lexical`, the trgm typo fallback | `qa_variants` | `domain_ids` |
| `qa_vector` | `qa_embedding` | `domain_ids` |
| `route._corpus_topics` (direct offer) | `qa_entries` | `domain_ids` |

The column is on the scan surface and not reached through a join, and that is
not a preference: `qa_vector` orders by `embedding <=> query` and takes a
`LIMIT`, so a predicate applied to the returned rows would narrow an
already-chosen top-k instead of choosing a top-k within the area. Migrations 140
and 144 denormalised `domain_ids` onto both surfaces for exactly this. One
helper — `qa_common.domain_filter` — builds the fragment for all of them, so a
new QA query cannot be written without one.

**Suggestions are bound by the same filter as answers**, because a chip is a
query the user is invited to send:

- the ranking's runners-up (they come from the already-filtered candidate query);
- the **category top-up**, at *both* its call sites — `qa_common._scope_frags`
  and the fill inside `qa_lexical` decision A. A curator's category groups
  entries *within a pack* and says nothing about areas, so in a mixed pack the
  second call site offered a catering question under an attendance search until
  it was fixed (caught by `tests/test_domain_scope.py`);
- the **other-year safety net**, which relaxes the year and *only* the year;
- the LLM **variant probe** in `answer.py`, which re-runs `route` + `qa_exact` +
  `qa_lexical` over a copied state — the copy carries the area scope, or it
  would search the whole corpus and then offer what it found there.

**The IDF denominator is deliberately NOT narrowed.** `concept_idf_weights`
measures how discriminative a concept is *in the packs `route` selected*, and
that number feeds the A/B/C threshold. Re-measuring it inside one area moves a
calibrated bar with the user's choice of area; on the document side the same
mistake made a three-chunk area mark every query token generic and turned a
correct filter into a `no_answer`. Eligibility is the ranked query's job and
that **is** filtered, so nothing out of area can be an answer, a suggestion or a
chip — only the weights among admitted rows are computed pack-wide.

**Labelling integrity is a data property, not a code property.** The filter
reads the *scanned* copy; if it disagrees with `qa_entries`, the filter and the
curator disagree. `scripts/domain_inventory.py` reports that per school, and
distinguishes the harmless case (same set, different array order — irrelevant to
array overlap; 274 such rows on production, 2026-09-06) from a genuine mislabel.

## Authoring → serving

Curated packs are authored as CSV and ingested by the Langflow QA flow, outside
this module. The reduced token forms MUST follow the parity contract
([../../contracts/parity/](../../contracts/parity/)); the index columns are built
from them. Retrieval reads the eligible content; the separate internal QA administration router also writes curated data.

Upload is a **merge** by default: a row present in the file is inserted or
updated, a row marked inactive is deactivated, and a row **absent** from the file
is left alone. Deleting is done through the active flag, not by removing a line,
so a botched spreadsheet edit can at worst fail to update something. `replace`
mode deactivates what the file omits.

## Invalidation

Soft-versioning via `is_active` (`enforced`): re-ingesting a pack deactivates the
previous rows and inserts the new ones as active; only active rows are matched.
The runtime scope also applies `valid_from`/`valid_to` as a hard eligibility filter; absence of a bound does not imply a TTL. A normalization/stemming change requires
regenerating parity vectors and re-ingesting the pack (the index bakes the tokens
in).

## Miss behavior

An exact miss falls to lexical; lexical branch C continues into the document
steps; a vector score below the floor continues to the document vectors and
generation. If nothing curated or generated resolves the question, the executor's
safety net returns `no_answer` — which is the correct outcome for a question the
corpus does not answer, not a failure.

Without a key the `no_answer` result still carries the offered questions parked
by `qa_lexical` (verified through the whole cascade). When documents are in
scope, the document steps may gather passages and the generation then fails:
the result is `llm_unavailable` ("Dočasný technický problém" in the chat) with
the same offered questions. Measured on a replay of a production log with its
documents: about a quarter of all questions end that way without a key
(`tools/lab/regression/LEDGER.md`, 8 Nov 2026). With `skip_without_key` on the answer step
(migration 223) they end in `no_answer` with the same offered questions instead.
