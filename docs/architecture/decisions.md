---
id: docs.architecture.decisions
status: current
components: [architecture]
sources:
  - search/app/executor.py
  - search/app/graph.py
  - search/app/nodes/answer.py
  - search/app/rerank.py
  - search/app/scope.py
  - search/app/settings.py
  - search/app/topical.py
last_verified: 2026-08-28
---

# Decision log

**Track:** human. Append-only. Newest last. Each entry: context → decision →
consequence. Record *why*, not *how* (the how lives in code + the `docs/ai/`
pages). Do not rewrite past entries; supersede them with a new one.

---

### D-1 — Deterministic QA→RAG cascade replaces parallel-RRF

**Context:** the earlier retrieval ran several strategies in parallel through a
Langflow agent and fused them with Reciprocal Rank Fusion.
**Decision:** replace it with a single deterministic cascade — curated QA beats
generated RAG, lexical beats vector, one LLM (`answer`) at the end.
**Consequence:** results are explainable and fast on the curated path
(single-digit ms); no cross-retriever fusion (see
[../ai/retrieval.md](../ai/retrieval.md)); the Langflow agent/search flows were
removed from the repo.

### D-2 — Config-driven executor is the live path; the StateGraph is reference

**Context:** the cascade must be reorderable / tunable without code changes.
**Decision:** `server.py` runs `executor.run_search`, which walks
`search_setting.config.pipeline`. `graph.py` (hard-coded `StateGraph`) is kept
only as a readable equivalent.
**Consequence:** order, enabled steps, and params are data (one active
`search_setting` row). `graph.py` can drift; the executor + active setting are
authoritative.

### D-3 — Coverage gate, not raw ts_rank, for lexical precision

**Context:** an OR tsquery matches a chunk/entry on one incidental shared token,
and `ts_rank_cd` scales with chunk size, so a rank floor cannot separate off-topic
matches across documents.
**Decision:** gate lexical retrieval on **coverage** (fraction of the query's
content concepts present), which is size-independent.
**Consequence:** `document_lexical.min_coverage` (0.5) and QA coverage thresholds;
off-topic queries fall through to `no_answer` instead of returning a weak hit.

### D-4 — Expand only entity synonyms, keep interrogatives literal

**Context:** expanding interrogative groups (kdy→termín/den…) floods the OR-query
and collapses the top1↔top2 margin, so confident answers stop passing the margin
gate.
**Decision:** expand only `entity` synonym groups; interrogatives stay literal and
are excluded from coverage concepts.
**Consequence:** margin stays meaningful; time-type questions still rank
date-bearing entries first. Logic in `search/app/nodes/qa_common.py`.

### D-5 — Reduce (lemma/stem) before folding diacritics

**Context:** simplemma's dictionary and the Dolamic–Savoy rules are keyed on
diacritics-intact Czech words.
**Decision:** tokenize → reduce on the diacritics-intact word → ASCII-fold LAST.
**Consequence:** deviates from a naive "strip diacritics first" order; the parity
contract ([../../contracts/parity/](../../contracts/parity/)) captures the exact
outputs both Python and Dart must reproduce.

### D-6 — Degrade, never 500, on LLM/DB trouble

**Context:** an OpenAI outage once 500-crashed `/search`.
**Decision:** each pipeline step catches its own errors and falls through; the
`answer` LLM failure degrades to `no_answer`; the guard skips on a blocklist DB
error; logging is fire-and-forget.
**Consequence:** availability over strictness — a masked outage looks like
`no_answer` (observability gap noted in `tests/README.md`). A 500 now
means a failure *outside* a step (see [../../contracts/errors.yaml](../../contracts/errors.yaml)).

### D-7 — Answer-language and grounding are prompt-level, not enforced

**Context:** the terminal answer must stay in the user's language and not invent
facts.
**Decision:** these are requested in the `answer_generator` prompt.
**Consequence:** they are `prompt` behaviors, not guarantees; documented as such
in [../ai/retrieval.md](../ai/retrieval.md). Refusal detection for the borderline
(domain-adjacent but unanswerable) zone is an open gap.

### D-8 — Result order is a tuple of tiers, not a single score

**Context:** a single fused score cannot express "the school's own binding rules
outrank an external guide" or "the asker's own year outranks a better-scoring
passage for another year" — an additive boost either does nothing or swamps the
score, depending on the gap.
**Decision:** order by a tuple compared left to right —
`(sibling, grade tier, priority, [heading,] authority, −fused, −raw)`. Earlier
elements decide outright; the fused score only breaks ties inside a tier.
**Consequence:** ordering is predictable and explainable, but a weight inside the
fused score has **no effect while a tier above it separates the candidates**.
Single-knob liveness sweeps therefore under-report: "no effect" means "no effect
at this configuration", never "dead parameter". Deleting a parameter is safe only
on static grounds. See [../SETTINGS_REFERENCE.md](../SETTINGS_REFERENCE.md) §3.10.

### D-9 — Source authority is a hard tier, not a boost

**Context:** with `rrf_k = 1` the score gaps between candidates are large, so no
additive weight orders authority reliably across queries.
**Decision:** documents whose type carries the priority role form their own tier,
and the document type's `sort_order` is the authority ladder inside it (binding
rules before an information page). The tier fires even when the question names no
year, and is then the primary tier.
**Consequence:** the school's own rules lead answers without tuning; a relevance
guard and a minimum score ratio keep an off-topic priority chunk from inheriting
the privilege.

### D-10 — The reranker is a veto, not a ranker

**Context:** an LLM asked for a permutation returns free-form output. Three
identical runs of one question over an identical candidate pool produced
identical positions 1–2 and three different orders from position 3 down, so the
passage carrying the decisive rule reached the model's context in one run out of
three. The answer was not choosing between facts; two runs never saw the fact.
**Decision:** the judge returns a **per-passage grade** (2/1/0), not an order.
The grades remove candidates; the tier order, computed by SQL and fusion and
therefore identical on every run, decides the sequence.
**Consequence:** retrieval is reproducible again. A strict judge cannot empty the
pool (a minimum-keep floor holds), and a judge timeout leaves the tier order
standing.

### D-11 — A hosted cross-encoder may select candidates, never rank them

**Context:** used as the ranker, a hosted reranker that puts the wrong passage
first causes the focus gate to discard the correct document entirely — one bad
ranking decision loses the answer instead of degrading it.
**Decision:** the hosted model runs as a **prefilter**: it scores the pool and
decides which candidates reach the judge, after which the tier order is restored.
It runs only when the pool is larger than the judge's window, the current best
candidate is protected, and the number of passages sent is capped.
**Consequence:** cost and latency are bounded on large pools with no measured
change in answers; the failure mode of a bad hosted ranking is removed, because
the hosted model never decides an order.

### D-12 — No retrieval round is entitled to return something

**Context:** a round that finds only a generic concept in common with the
question will still return its best candidate, and a plausible-looking wrong
passage is worse than silence.
**Decision:** every retrieval round applies a topical admission gate: a candidate
matching only a generic concept is not admitted, and document-frequency caps keep
common terms from driving a match.
**Consequence:** questions the corpus genuinely does not answer end in silence
rather than a confident irrelevance. Silence is a correct outcome, and evaluation
sets must contain negative cases that assert it.

### D-13 — Keywords live on the chunk, not in a table of their own

**Context:** keywords assigned at ingest were kept in a separate table, which
made every retrieval round join to reach the truest content signal there is.
**Decision:** keywords are an array column on the chunk; the keyword rank is
annotated onto candidates and weighted in fusion.
**Consequence:** one join fewer on the hot path. The dedicated keyword retrieval
round is disabled by default — measured over a fixed question set it moved the
first passage in two cases while adding a third more candidates to the pool.

### D-14 — Level and owner are resolved per chunk, not per document

**Context:** one legal person can operate a primary and a secondary school, and a
single document (a code of conduct) can contain a chapter for each. Resolving the
level per document makes "may I photograph in school" answer from the wrong
chapter.
**Decision:** each chunk carries its own school level and owning unit, derived
from the heading structure, and each chunk carries the heading path it sits
under. A level-focused question drops chunks of the opposite level.
**Consequence:** the two halves of a shared document are separable, and an answer
can name the section it came from. Chunks written before this must be re-ingested
to gain the fields; the level filter treats a missing value as "applies to both".

### D-15 — Prompts, wording and thresholds are data

**Context:** a prompt change or a reworded message required a redeploy, and the
same value existed in several places.
**Decision:** every LLM prompt lives versioned in a registry table and is fetched
by a technical key per request; every string a person reads lives in the message
and label tables, one row per language; every threshold lives in the settings
row. Code holds none of them.
**Consequence:** behaviour changes without a restart, and each fact has one
owner. The cost is that reading the code no longer tells you what the system
does — the active configuration does, which is why
[../SETTINGS_REFERENCE.md](../SETTINGS_REFERENCE.md) exists.

### D-16 — The answer to a risky question is configuration, not a refusal in code

**Context:** a blocked topic was answered with a generic refusal, which is the
wrong response when a child asks about self-harm.
**Decision:** the blocklist names categories; the settings row maps a category to
an outcome and a message key; the message table holds the wording per language.
A self-harm outcome answers with a crisis line rather than declining.
**Consequence:** a school can change what is blocked and how it is answered
without a code change, and the wording can be reviewed by people who do not read
code.

### D-17 — Configuration resolves to the nearest owner, and takes the whole set

**Context:** merging an organisation's registry with a unit's produces sets
nobody declared, and the result depends on insertion order.
**Decision:** settings and code lists resolve by walking up to the nearest owner
that has a row, and that owner supplies the **entire** set. No merging, no union.
**Consequence:** a unit either overrides a list completely or inherits it
completely, which is inspectable. The trap is that a new flag must be written to
every active settings row, not only to the platform-wide one.

### D-18 — Entity visibility propagates down as well as up

**Context:** a document a parent organisation published for itself was invisible
to its own units, so a unit's answer omitted rules that governed it.
**Decision:** the query scope includes the ancestors' entity-level documents, so
a subordinate unit sees what the organisation above it published.
**Consequence:** units see their parent's binding documents. Isolation between
different legal persons is unaffected and remains non-configurable.


### D-19 — Preserve the current production visibility model (2026-10-12)

**Context:** D-18 and the early multi-entity design describe hierarchy-based entity
visibility, but production and current source use a strict three-level model. A
read-only check of the running production `server.py` and `scope.py` matched
source c079bd8 byte for byte before documentation-comment cleanup. The executed
visibility predicates remain identical after that cleanup. The owner instructed us to retain production behaviour.

**Decision:** `entity` reaches only the owning entity; `organization` explicitly
shares within the tenant tree; `private` requires the verified owner within that
organisation. Hierarchy alone never expands `entity` access. The stored legacy
`entity_visible_to_parent` flag is unused by search.

**Consequence:** D-19 supersedes D-18. Documentation and source comments follow the
production rule; retrieval predicates, schema, settings and role checks are unchanged.
The early partition/RLS/global-scope proposal is historical, not an implemented contract.
