---
id: docs.ai.qa-fastpath
status: current
components: [qa, retrieval]
sources:
  - search/app/nodes/qa_exact.py
  - search/app/nodes/qa_lexical.py
  - search/app/nodes/qa_vector.py
  - search/app/nodes/qa_common.py
  - search/app/nodes/route.py
  - search/app/scope.py
  - search/app/config.py
last_verified: 2026-08-28
---

# QA fast-path (curated answers)

**Track:** human (AI-relevant). The curated question/answer path that runs before
any document retrieval and returns author-written answers verbatim (no LLM).
Control types as in [routing.md](routing.md).

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

### qa_lexical (`enforced`; thresholds `evaluated`, uncalibrated)

BM25-style ranking with synonym expansion, then an A/B/C decision:

- Build an OR tsquery from `{norm, lemma}` tokens expanded via `qa_synonym`
  (entity groups only; interrogative groups are NOT expanded — expanding them
  floods the OR-query and collapses the top1↔top2 margin). Rank active variants
  with `ts_rank_cd`, aggregate variants → entries.
- **coverage** = fraction of the query's CONTENT concepts (lemma form, minus
  function words and interrogatives) present in the top entry. **margin** =
  `(rank1 − rank2) / rank1`.
- Decision:

  | Branch | Condition | Result |
  |---|---|---|
  | **A** answer | `coverage ≥ t_answer` AND `margin ≥ m_margin` | the curated answer, stop |
  | **B** suggest | `coverage ≥ t_suggest` (not A) | up to `suggest_top_n` "myslel jsi?" questions, stop |
  | **C** fall | `coverage < t_suggest` | continue cascade |

- Thresholds resolve pack manifest → pipeline params → environment fallback, in
  that order. They are `evaluated` against the eval sets but **not calibrated** —
  do not treat them as tuned. Their values are in
  [../SETTINGS_REFERENCE.md](../SETTINGS_REFERENCE.md) §3.3, the canonical owner.
- **Typo fallback:** if the tsquery matches nothing, a trigram similarity search
  can produce **suggestions only**, never a confident answer, above a configured
  similarity floor.
- **Other-grade safety net:** when the asker's own year yields nothing, a small
  number of matches from a *different* year may be offered, provided they are
  good enough relative to the best match. The answer then states which year the
  passage applies to, so a rule for ninth-graders is never presented to a
  second-grader as if it were theirs.

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

## Authoring → serving

Curated packs are authored as CSV and ingested by the Langflow QA flow, outside
this module. The reduced token forms MUST follow the parity contract
([../../contracts/parity/](../../contracts/parity/)); the index columns are built
from them. This module only reads.

Upload is a **merge** by default: a row present in the file is inserted or
updated, a row marked inactive is deactivated, and a row **absent** from the file
is left alone. Deleting is done through the active flag, not by removing a line,
so a botched spreadsheet edit can at worst fail to update something. `replace`
mode deactivates what the file omits.

## Invalidation

Soft-versioning via `is_active` (`enforced`): re-ingesting a pack deactivates the
previous rows and inserts the new ones as active; only active rows are matched.
There is no time-based expiry. A normalization/stemming change requires
regenerating parity vectors and re-ingesting the pack (the index bakes the tokens
in).

## Miss behavior

An exact miss falls to lexical; lexical branch C continues into the document
steps; a vector score below the floor continues to the document vectors and
generation. If nothing curated or generated resolves the question, the executor's
safety net returns `no_answer` — which is the correct outcome for a question the
corpus does not answer, not a failure.
