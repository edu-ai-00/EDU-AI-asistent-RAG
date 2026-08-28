---
id: docs.settings-reference
status: current
components: [settings, retrieval, ranking, chunking]
sources:
  - search/app/settings.py
  - search/app/executor.py
  - search/app/nodes/answer.py
  - search/app/rerank.py
  - postgres/init_clean/02_seed_config.sql
last_verified: 2026-08-28
---

# Settings reference — the `edu_ai.settings` row

Almost nothing about retrieval behaviour is hard-coded. One JSONB document
decides which retrieval steps run, in what order, when each may stop the
cascade, how aggressively text is chunked at ingest, and how the answer is
assembled. This page documents that document field by field.

> Values shown are those of the **reference configuration** shipped in
> `postgres/init_clean/02_seed_config.sql` (label `v3`). They are a working
> baseline measured on a real corpus, not hard defaults — every one of them can
> be changed per organisation without touching code.

---

## 1. The table

| Column | Type | Meaning |
|---|---|---|
| `setting_id` | uuid | Identity of the configuration version. |
| `label` | text | Human name of the version, e.g. `v3 — organisation X`. |
| `is_active` | boolean | Only active rows are considered. |
| `config` | jsonb | The configuration itself — everything below. |
| `notes` | text | Free-form provenance: what was measured, why a value changed. |
| `created_at` | timestamptz | When the version was created. |
| `legal_entity_code` | text | Owning organisation, or `NULL` for the platform-wide row. |

### Resolution

`get_active_setting(legal_entity)` in [search/app/settings.py](../search/app/settings.py)
walks **up** the entity hierarchy to the organisation and returns that
organisation's active row; if the organisation has none, the platform-wide row
(`legal_entity_code IS NULL`) is used. There is no merging — the nearest owner
supplies the whole document.

Reads are cached in-process with a **5 second TTL**. A settings edit therefore
takes effect within five seconds; no restart is needed. (The cache used to live
for the whole process lifetime, which made every edit look like it had been
ignored — hence the TTL.)

If the table is missing or has no active row, a built-in `_FALLBACK` document is
used so that a configuration problem degrades search rather than taking the
service down.

### Two runtimes read the same document

| Section | Consumer |
|---|---|
| `pipeline`, `search`, `context`, `visibility`, `normalization`, `tokenization` | the retrieval service (`search/app/…`) |
| `chunking`, `engines` | the Langflow ingestion flows (`langflow_flows/ingestion/*.json`) |

This matters when a parameter appears to do nothing: a chunking parameter is
consumed at **ingest** time, so changing it only affects documents ingested
afterwards. Existing chunks keep the shape they were written with.

### Changing a value

Configuration is data, so it changes through a **numbered migration** in
`postgres/init/`, never through an ad-hoc `UPDATE`. Two traps:

1. **Settings are per organisation.** A new flag must be written to *every*
   active row, not only the platform-wide one. A migration that updates one row
   silently leaves the other organisations on the old behaviour.
2. **`jsonb_set` does not create intermediate objects.** Adding
   `params.rerank_prefilter.backend` to a step that has no `rerank_prefilter`
   object writes nothing and reports success.

---

## 2. Global sections

### `search`

| Key | Reference | Meaning |
|---|---|---|
| `max_result_documents` | `3` | Upper bound on how many distinct documents may contribute passages to one answer. |

### `context` — follow-up resolution

Not a pipeline step: it must run before `prepare` builds the token forms.

| Key | Reference | Meaning |
|---|---|---|
| `enabled` | `true` | Resolve pronouns/ellipsis against the previous turns ("and when is it?"). |
| `max_turns` | `4` | How many previous turns of the session may be considered. |
| `window` | `2 hours` | Age limit on those turns; older history is not context. |
| `model` | `gpt-4.1-mini` | Model that rewrites the follow-up into a standalone question. |
| `max_tokens` | `120` | Output cap for that rewrite — it is a query, not an answer. |

### `engines` — lemmatizer and stemmer per language

An array; one entry per (language, kind). Each entry:

| Key | Meaning |
|---|---|
| `kind` | `lemmatizer` or `stemmer`. |
| `name` | Implementation: `simplemma`, `dolamic_savoy_light`, `snowball_english`, `snowball_german`, … |
| `language` | ISO code the entry applies to (`cs`, `en`, `de`, `es`). |
| `column` | Target column written at ingest: `lemma_text` or `stem_text`. |
| `active.ingest_docs` / `active.ingest_qa` | Whether the engine runs for documents / for curated QA pairs. |
| `params.min_len` | Minimum token length the stemmer will touch. |
| `params.aggressive` | Czech light stemmer only: aggressive rule set off by default. |

The registry of languages themselves is **not** here — it is
`edu_ai.code_list(language)`. Add a language there first; the engines array then
gives it a lemmatizer and a stemmer.

### `normalization` and `tokenization`

| Key | Reference | Meaning |
|---|---|---|
| `tokenization.active` | `true` | Build the token forms at all. |
| `normalization.lowercase` | `true` | Case-fold before matching. |
| `normalization.strip_diacritics` | `true` | Fold `č → c` so that unaccented typing still matches. |
| `normalization.drop_stopwords` | `true` | Apply the `edu_ai.qa_stopword` list. |

Order is deliberate and differs from the on-device specification: the server
**reduces first** (lemma/stem) and strips diacritics afterwards, because both
the simplemma dictionary and the Czech stemmer rules need the diacritics to be
present. The stop-word list itself is a code list, not code and not config.

### `visibility`

| Key | Reference | Meaning |
|---|---|---|
| `search_requires_user` | `false` | Whether a query must carry an authenticated user. |
| `entity_visible_to_parent` | `true` | Enforced at query time: a unit's `entity` documents are visible to its parent organisation and, through it, to sibling units. |
| `child_may_publish_organization` | `true` | Enforced at ingest: a unit may mark a document `organization`, which reaches the parent and the siblings — never another legal person. |

Both tighten when set to `false`. Cross-tenant isolation is not configurable.

---

## 3. The pipeline

`config.pipeline` is an **ordered array of steps**. Order in the array is
execution order; steps can be reordered, disabled, or given different stop
conditions without code changes.

Control fields present on every step:

| Field | Meaning |
|---|---|
| `step` | Step name; binds to the node implementation in `search/app/nodes/`. |
| `enabled` | `false` skips the step entirely. |
| `on_hit` | `stop` ends the cascade when this step produced a result; `continue` passes its candidates on and keeps going. |
| `only_if_empty` | Run only when no earlier step produced candidates. |
| `only_if_max_coverage_below` | Run only when the best candidate's term coverage is below this ratio — "the lexical pool is weak, try vectors". |
| `run_on_timespan` | Run even for date/period questions, where lexical matching is typically poor. |
| `params` | Step-specific parameters — sections below. |

Reference order: `route → qa_exact → qa_lexical → document_lexical →
rag_keywords → qa_vector → rag_vector → document_siblings → answer`. Curated QA
pairs are tried before document retrieval; vectors are the last resort.

### 3.1 `route` — admission and scope

No LLM, no retrieval: decide whether the question may be answered at all, and
which QA packs are in scope for the asker's grade.

| Parameter | Reference | Meaning |
|---|---|---|
| `phrase_slack` | `3` | Token distance still counted as a phrase match when matching the blocklist. |
| `guard_category_order` | `self_harm, hate, sexual, violence, profanity` | Evaluation order; the first category that trips decides the response. |
| `guard_block_min_severity.<category>` | `1` | Severity at which that category blocks. `1` = block on any hit. |
| `guard_outcome.self_harm` | `self_harm` | Outcome name, which selects the response — not a plain refusal. |
| `guard_message_key.self_harm` | `guard_self_harm` | Key into `edu_ai.system_message`, whose rows carry the wording per language (crisis line, "talk to an adult you trust"). |

The response to a risky question is **data, not code**: severity, outcome and
message key live here, and the wording lives in `system_message` rows — one per
language.

### 3.2 `qa_exact` — curated pairs, exact match

No parameters beyond `on_hit: stop`. A normalized exact question match answers
immediately.

### 3.3 `qa_lexical` — curated pairs, lexical match

| Parameter | Reference | Meaning |
|---|---|---|
| `form` | `norm, lemma, stem` | Token forms matched against, in that order. |
| `fetch_k` | `50` | Candidate QA entries pulled before ranking. |
| `synonyms` | `true` | Expand the query through `edu_ai.qa_synonym` groups (abbreviations, local names). |
| `suggest_top_n` | `3` | How many near misses are offered as "did you mean". |
| `trgm_suggest_min` | `0.4` | Trigram similarity floor for a near miss to be offered — typo tolerance. |
| `other_grade_top_n` | `2` | Safety net: how many matches from a *different* grade may be shown when the asker's own grade has none. |
| `other_grade_min_ratio` | `0.5` | How good such an other-grade match must be, relative to the best match, to be worth showing. |

### 3.4 `document_lexical` — full-text over chunks

The primary document retriever: BM25 over the generated `bm25_text` column
(normalized text + lemmas + stems + heading lemmas), with a heading pass, an IDF
coverage gate, and a trigram fallback for typos.

| Parameter | Reference | Meaning |
|---|---|---|
| `form` | `lemma, stem` | Token forms used for matching. |
| `fetch_k` | `30` | Chunks pulled before ranking. |
| `bm25_lexical` | `true` | Use BM25 ranking rather than plain boolean matching. |
| `body_weight` / `heading_weight` | `B` / `A` | PostgreSQL `setweight` classes: a heading match outranks a body match. |
| `bm25_min_ratio` | `0.65` | A candidate must reach this share of the best BM25 score to survive. |
| `bm25_max_df_share` | `0.25` | Terms occurring in more than 25 % of chunks are too common to carry meaning and are ignored. |
| `heading_pass` | `true` | Run a separate pass over headings only. |
| `heading_max_df_share` | `0.25` | Same document-frequency cut for the heading pass. |
| `heading_numeric_pass` | `false` | Whether a bare number in a heading may match on its own. |
| `idf_coverage` | `true` | Score candidates by how much of the question's *informative* mass they cover, not by term count. |
| `min_coverage` | `0.5` | A candidate must cover at least half of that mass. |
| `max_concepts` | `8` | Cap on distinct concepts extracted from one question. |
| `topical_admission` | `true` | A chunk that matches only a generic concept is not admitted. No round is entitled to return something. |
| `trgm_fallback` | `true` | Fall back to trigram similarity when full text finds nothing. |
| `trgm_fetch_k` | `15` | Candidates for that fallback. |
| `trgm_min_sim` | `0.35` | Similarity floor for the fallback. |
| `trgm_min_typo_coverage` | `0.5` | How much of the question the fuzzy match must still cover — prevents one fuzzy token from carrying a whole hit. |
| `rank_tiebreak_weight` / `rank_tiebreak_cap` | `0.001` / `0.999` | Deterministic tie-break so equal scores order stably. |

### 3.5 `rag_keywords` — LLM keywords assigned at ingest

Disabled in the reference configuration: measured on a 42-question set it moved
the first passage in 2 cases while adding 134 chunks to the pool.

| Parameter | Reference | Meaning |
|---|---|---|
| `form` | `lemma, stem` | Token forms matched against `document_chunks.keywords`. |
| `fetch_k` | `30` | Chunks pulled. |
| `min_overlap` | `2` | Minimum number of overlapping keywords. |
| `heading_quality` | `1.0` | Weight of a heading-derived keyword relative to a body one. |
| `topical_admission` | `true` | Same generic-concept gate as above. |
| `topical_max_df_share` | `0.25` | Document-frequency cut for that gate. |

### 3.6 `qa_vector` — curated pairs, semantic match

| Parameter | Reference | Meaning |
|---|---|---|
| `cosine_min` | `0.65` | Similarity floor. Below it the pair is not an answer to this question. |

### 3.7 `rag_vector` — document chunks, semantic match

| Parameter | Reference | Meaning |
|---|---|---|
| `top_k` | `5` | Chunks kept. |
| `fetch_k` | `30` | Chunks pulled before filtering. |
| `cosine_min` | `0.45` | Similarity floor. |
| `heading_quality` | `1.0` | Weight of the heading part of the embedded text. |
| `grade_query_enrich` | `true` | Prefix the embedded query with the asker's grade. |
| `grade_query_prefix` | `Pro {grade}. ročník: ` | The prefix template; `{grade}` is substituted. |
| `only_if_max_coverage_below` | `0.75` | Run only when the lexical pool is weak. |
| `run_on_timespan` | `true` | Run for date questions even so. |

`document_chunks.embedding` is the only vector store in the system.

### 3.8 `document_siblings` — neighbouring chunks

Disabled in the reference configuration.

| Parameter | Reference | Meaning |
|---|---|---|
| `window` | `0` | How many neighbouring chunks to pull around a hit. |
| `grade_recall` | `from_pool_docs` | Where to look for the asker's grade when the pool has none. |
| `grade_recall_max` | `8` | Cap on that recall. |
| `max_chunks_per_element` | `6` | Cap per document element (list, table, section). |

### 3.9 `answer` — fusion, reranking, generation

The largest section: it fuses the pools, orders them, decides what reaches the
model, and constrains what the model may write.

**Fusion and weighting**

| Parameter | Reference | Meaning |
|---|---|---|
| `rrf_k` | `1` | Reciprocal-rank-fusion constant across retrievers. |
| `bm25_weight` | `0.03` | Weight of the lexical score in fusion. |
| `keyword_weight` | `1.0` | Weight of the keyword rank — keywords are the truest content signal. |
| `vector_element_weight` | `1.0` | Weight of the vector rank. |
| `heading_match_boost` | `0.12` | Bonus when the question matches the chunk's heading. |

**Ordering is tiered, not scored.** This is the single most important thing to
understand before touching any weight in this section, so it has its own part
below (§3.10). The short version: the result order is a **tuple**, and a
parameter that lives inside the score has no effect at all while a tier above it
already separates the chunks.

| Parameter | Reference | Meaning |
|---|---|---|
| `grade_scope` | `focus_if_match` | Grade handling: focus on the asker's grade when the pool contains it, otherwise keep everything. |
| `grade_chunk_min_ratio` | `0.8` | How dominant the matching grade must be before the focus applies. |
| `priority_tier` | `true` | Documents marked `search_role = 'priority'` form their own, higher tier. |
| `priority_min_ratio` | `0.65` | How good a priority chunk must be, relative to the best, to keep that privilege. |
| `focus_gate` | `true` | Restrict the pool to the document that best matches the question. |
| `focus_max_df` | `0.05` | Only rare terms may drive that focus. |
| `focus_require` | `all` | All focus terms must be present. |
| `domain_match` | `true` | Prefer chunks whose domain matches the question's. |
| `domain_kw_max_df_share` | `0.25` | Document-frequency cut for domain keywords. |
| `binding_fallback` | `true` | Fall back to the domain × school-level binding when the question names no grade. |
| `source_min_ratio` | `0.5` | Score floor for a chunk to be cited as a source. |
| `source_ratio_anchor` | `tier_first` | That ratio is measured against the best chunk **in the same tier**, not against the global best. |
| `max_chunks_per_document` | `6` | Cap per document, so one long document cannot crowd out the rest. |

**Reranking** — a two-stage arrangement: a hosted cross-encoder selects *which*
candidates reach the judge; an LLM judge then grades them.

| Parameter | Reference | Meaning |
|---|---|---|
| `rerank_mode` | `cloud` | `off` \| `local` (in-process cross-encoder) \| `cloud` (LLM judge) \| `jina` (hosted). |
| `rerank_model` | `gpt-5.6-luna` | Judge model in `cloud` mode. |
| `rerank_top_k` | `20` | How many candidates the judge sees. |
| `rerank_output` | `graded` | `graded` (2/1/0 per passage) rather than a permutation. |
| `rerank_reorder` | `true` | Whether the judge may reorder, or only veto. |
| `rerank_min_keep` | `6` | Floor on survivors, so a strict judge cannot empty the pool. |
| `rerank_score_min` | `0` | Minimum grade to keep. `0` keeps everything the judge did not veto. |
| `rerank_seed_top_n` | `12` | Top candidates guaranteed to reach the judge. |
| `rerank_require_grade` | `2` | Grade at which a passage counts as a real answer. |
| `rerank_grade_ordering` | `false` | Whether the judge's grade may override the grade tier. |
| `rerank_passage_chars` | `500` | Characters of each passage shown to the judge. |
| `rerank_timeout_s` | `15` | Timeout; on expiry the pre-rerank order stands. |
| `rerank_reasoning_effort` | `low` | Reasoning effort for the judge. |
| `rerank_log_passages` | `false` | Log full passages — debugging only, verbose. |
| `rerank_prefilter.backend` | `jina` | Prefilter backend; empty disables the prefilter. |
| `rerank_prefilter.model` | `jina-reranker-v3` | Hosted model used. |
| `rerank_prefilter.top_n` | `20` | How many candidates it hands to the judge. |
| `rerank_prefilter.max_in` | `40` | Hard cap on what is sent — cost and latency control. |
| `rerank_prefilter.passage_chars` | `400` | Characters per passage sent. |
| `rerank_prefilter.keep_anchor` | `true` | The current best candidate always survives the prefilter. |
| `rerank_prefilter.timeout_s` | `10` | Timeout; on expiry the prefilter is skipped, not fatal. |

The prefilter **selects, never reorders**: it decides *which* 20 candidates the
judge sees, and the tier order is restored afterwards. It runs only when the
pool is larger than `rerank_top_k`, so short pools cost nothing.

**Thin-chunk guards** — a chunk that is technically a hit but too small to
answer anything.

| Parameter | Reference | Meaning |
|---|---|---|
| `thin_max_chars` | `120` | Below this length a chunk counts as thin. |
| `thin_guard_seed` | `true` | A thin chunk may not seed the focus. |
| `thin_guard_judge` | `true` | A thin chunk may not be the judge's anchor. |
| `thin_require_digit` | `false` | Whether a thin chunk must contain a digit (a date, a number) to survive. |
| `seed_scan_depth` | `15` | How deep to scan for a usable seed. |

**Generation**

| Parameter | Reference | Meaning |
|---|---|---|
| `model` | `gpt-5.6-luna` | Answering model. |
| `temperature` | `0` | Deterministic. |
| `reasoning_effort` | `medium` | Reasoning effort. |
| `max_tokens` | `2000` | Output cap. |
| `token_budget` | `8000` | Budget for the assembled context. |
| `chars_per_token` | `4` | Conversion used to estimate that budget. |
| `content_top_n` | `5` | Passages passed to the model as content. |
| `sources_top_n` | `3` | Sources listed under the answer. |
| `include_excerpt` | `true` | Include a quoted excerpt with each source. |
| `excerpt_max_chars` | `4000` | Cap per excerpt. |
| `excerpt_cut_ratio` | `0.6` | Where a long excerpt is cut. |
| `attrib_best_min` | `0.25` | Attribution floor for the best source. |
| `attrib_positional_min` | `0.15` | Attribution floor by position in the answer. |
| `clarify_max_match_score` | `1.0` | Above this score the answer is given rather than a clarifying question. |
| `related_suggestions_top_n` | `3` | Related questions offered afterwards. |
| `log_candidates_n` | `20` | Candidates written to the query log for later analysis. |

The prompt itself is **not** here. All LLM prompts live versioned in
`edu_ai.prompt_registry` and are fetched by a technical key.

### 3.10 How the answer is ordered — tiers, not a single score

Since the "single score" refactor, `_sortkey` in
[search/app/nodes/answer.py](../search/app/nodes/answer.py) does **not** sort by a
score. It sorts by a tuple, compared left to right. An earlier element decides
outright; the score only breaks ties **inside** a tier.

    (sib, gt, p, [h,] a, −fused, −score)

| Element | What it separates |
|---|---|
| `sib` | Evidence first, then a sibling chunk pulled in only for context. |
| `gt` | **Grade tier**, two-axis: own school + own grade > own school + all grades > organisation + all grades > no grade at all. It holds even when *no* chunk carries the asker's exact grade — the case the earlier binary "exact grade or not" could not express, because it never fired. |
| `p` | **Priority** — the source-authority tier. It fires even on a grade-less question, and is then the primary tier. This is what lets the school's own binding rules beat an external reference guide. |
| `h` | **Heading match**, present **only** in the grade-hard branch, and deliberately *before* authority there: a supply list whose heading *is* the question must beat the grade-6 section of the school rules, even though the rules rank higher on authority. |
| `a` | Within the priority tier, the document type's `sort_order` — the authority ladder (rules = 10 beats information page = 40). Outside the tier it is a constant and orders nothing. |
| `−fused` | The combined score. `bm25_weight`, `heading_match_boost`, `vector_element_weight` and `keyword_weight` all live **here**, at the second-to-last position. |
| `−score` | The raw retrieval score. |

`rrf_k = 1` in the reference configuration makes score gaps large, which is why
authority has to be a hard tier: an additive boost could not order it reliably.

#### The consequence: a parameter can be live but unreachable

A weight inside `fused` has **no effect whatsoever** while any earlier tier
separates the chunks — and full effect the moment it does not. Measured on
*"Co se hodnotí u přijímacích zkoušek?"*:

| `priority_tier` | `bm25_weight = 0` | `bm25_weight = 5` |
|---|---|---|
| on (shipped) | 1 source | 1 source — no change |
| off | **3 sources** | **1 source** |

`bm25_weight` is therefore live, but only *conditionally reachable*. It belongs
in the configuration precisely **because** it is invisible today: whoever turns
`priority_tier` off gets a completely different ordering, and needs the knob to
exist.

#### What this means for the parameter-liveness harness

`scripts/param_liveness.py` varies one key at a time while every other key stays
at its shipped value. Any parameter that only matters when a *different* one
changes therefore reports "no effect". So:

* A "no effect" verdict from that harness means **"no effect at this
  configuration"** — never "dead". Do not delete a parameter on its evidence
  alone.
* A clean sweep ("all read and used, nothing dead") holds for **one**
  configuration, not in general.
* Deleting a parameter is safe only on **static** grounds — read into a variable
  and never used — because that holds regardless of configuration.
* Fixing the harness means sweeping *pairs*, or at least switching the tier flags
  off while sweeping the score parameters.

### 3.11 Choosing a reranker

`rerank_mode` selects one of four arrangements. They are not interchangeable —
they differ in what they can decide, what they cost, and whether they are
reproducible.

| Mode | What it is | Latency / cost | Deterministic |
|---|---|---|---|
| `off` | No reranking; the tier order stands. | none | yes |
| `local` | Cross-encoder in the service process (CPU). | ~100 ms, no API | yes |
| `cloud` | LLM judge over the top-K passages. | one LLM call per query | **no** |
| `jina` | Hosted cross-encoder as the ranker. | one HTTP call | yes |

**`off`** is the honest baseline and the right setting while debugging
retrieval: if an answer is wrong with the reranker off, the problem is in the
retrieval steps or in the corpus, not in the ordering.

**`local`** runs a cross-encoder in-process. It resolves the semantic cases BM25
cannot — a blog *about* a topic versus the rules *governing* it — without an
external API, so it is the option when no data may leave the deployment. It
pulls in torch and transformers, which is most of the image size.

**`cloud`** is what the reference configuration uses. An LLM grades the top-K
passages and the grades feed back into the ordering. Two settings matter more
than the model choice:

* `rerank_output = graded` (2/1/0 per passage) rather than `order`
  (a permutation). Measured 2026-08-23 on three identical runs of the same
  question: identical pool, identical fused scores, identical judge positions
  1–2 — and **three different orders from position 3 down**. The chunk carrying
  the decisive rule reached the context in **one run out of three**. The answer
  was not choosing between facts; two runs never saw the fact. A permutation is
  free-form LLM output and is not reproducible; a per-passage grade is.
* `rerank_reorder = false` combined with the tier order restored afterwards
  makes the judge a **veto**, not a ranker. The tiers are computed by SQL and
  fusion and are identical on every run, so they decide the order; the judge
  only removes what does not answer the question.

**`jina`** as the *ranker* was measured on 2026-08-27 and is not recommended.
The v3.5 model was the worst of the family on our set; v3 and v2 recovered two
of the three failures, but the failure mode is severe: when the hosted model
puts the wrong passage first, the focus gate then discards the correct document
entirely, so one bad ranking decision loses the answer rather than degrading it.

**The arrangement that measured best is `cloud` + the Jina prefilter.** The
prefilter (`rerank_prefilter.backend = jina`) is a different job from ranking:
it scores the pool and decides **which 20 candidates reach the judge**, and the
tier order is restored immediately afterwards, so it can never reorder anything.
It runs only when the pool is larger than `rerank_top_k`, `keep_anchor` protects
the current best candidate, and `max_in` caps what is sent. Two independent runs
of the 42-question set, three passes each, scored **40/42 with zero changes
against the baseline** — the prefilter bounds cost and latency on large pools
without moving a single answer.

If you are unsure: leave `cloud` + prefilter as shipped, and switch to `off`
when you need to see what retrieval alone produces.

### 3.12 Models — what we measured

Every model below is configurable; none is compiled in. The notes are the
measured reasons for the current choice, not preferences.

| Role | Model in use | Where it is set |
|---|---|---|
| Answer generation | `gpt-5.6-luna`, `temperature 0` | `pipeline[answer].params.model` |
| Rerank judge | `gpt-5.6-luna`, reasoning effort `low` | `pipeline[answer].params.rerank_model` |
| Rerank prefilter | `jina-reranker-v3` | `pipeline[answer].params.rerank_prefilter.model` |
| Follow-up rewriting | `gpt-4.1-mini`, 120 output tokens | `context.model` |
| Embeddings | `text-embedding-3-small`, 1536 dims | `edu_ai.embedding_registry` |
| Chunking / enrichment at ingest | `gpt-5-mini` | the component's own input in the Langflow UI |

**Answer model, `temperature 0` (was 0.1).** Answer variance was the main
complaint in testing: two presses of the same question gave different answers.
Stepping through two runs seven seconds apart — same pool of 12 chunks, same 7
passages in context, same rerank positions — one answered and the other stayed
silent. Retrieval is deterministic; composition was what broke. Measured over
the regression set, three runs per question, majority vote:

| | answers | questions that varied | median RAG time |
|---|---|---|---|
| `temperature 0.1` | 33 / 5 | 8 | 4.7 s |
| `temperature 0` | 33 / 5 | **5** | **4.1 s** |

Three cases settled, no answer was lost, and the median got faster.

**The judge must not be weaker than the writer.** The judge's model used to live
in `docker-compose` (`RERANK_OPENAI_MODEL`) — the one piece that could not be
changed without a rebuild — and it was `gpt-4o-mini` while the answer was
written by `gpt-5.6-luna`. That mismatch explained what every measurement had
been pointing at: the small judge handed out exactly **one** grade-2 per query
(1 of 8, 1 of 20, and again 1 of 20 after the prompt was rewritten to demand
more), so the context was starved regardless of what retrieval had found.
Moving the model into the settings and matching it to the writer fixed it.

**Embeddings: one multilingual model for all languages.**
`text-embedding-3-small` covers cs/en/de/es from a single registry row, so
adding a language does not mean a second vector space. `document_chunks.embedding`
is the only vector store in the system.

**Ingestion chunker: `gpt-5-mini`.** `gpt-5.1-mini` returns 404 on this account,
and that family does not accept `temperature = 0`, so the chunking prompt is
kept short and language-agnostic instead of pinned by temperature.

**Jina reranker family.** Measured 2026-08-27: `v3.5` was the **worst** of the
family on our set; `v3` and `v2` recovered two of its three failures. `v3` is
therefore what the prefilter uses — and only as a prefilter, for the reason in
§3.11.

---

## 4. `chunking` — ingestion side

Consumed by the Langflow ingestion flows. Changes apply to **newly ingested**
documents only.

### `split_markdown_into_pages`

| Parameter | Reference | Meaning |
|---|---|---|
| `heading_level` | `2` | A line with 1..N `#` opens a new page. |
| `max_chars` | `0` | Hard page cap; `0` = unlimited. |
| `min_text_chars` | `3` | Below this a page is empty and dropped. |
| `merge_page_tables` | `true` | A table split across pages is put back together. |
| `strip_page_artifacts` | `true` | Drop headers, footers and page numbers. |

### `split_text_into_chunks.sizes`

| Parameter | Reference | Meaning |
|---|---|---|
| `chunk_size` | `900` | Target chunk length in characters. |
| `chunk_overlap` | `50` | Overlap between neighbours. |
| `min_chunk_chars` | `120` | Below this a chunk is merged into its neighbour. |
| `hard_min_chars` | `40` | Absolute floor; shorter output is discarded. |

### `split_text_into_chunks.headings`

| Parameter | Reference | Meaning |
|---|---|---|
| `split_at` | `true` | An inner heading ends the preceding chunk and opens a new one. |
| `max_len` | `60` | Longer lines are prose, not headings. |
| `in_embed_text` | `true` | The heading is embedded together with the body — this is what makes "which section is this from" answerable. |

### `split_text_into_chunks.bullet_blocks`

| Parameter | Reference | Meaning |
|---|---|---|
| `enabled` | `true` | Keep a bullet list together instead of cutting through it. |
| `markers` | `•`, `-`, `·` | Recognised bullet markers. |
| `min_bullets` | `3` | Minimum items for a block to count as a list. |
| `min_budget` | `200` | Minimum space that must remain for the block to be kept whole. |
| `head_max_chars` | `200` | Maximum length of the introducing line. |
| `head_requires_hash` | `false` | Whether that line must be a Markdown heading. |
| `repeat_head` | `false` | Off since migration 61: the heading now lives in its own field, so repeating it into every chunk only diluted the text. |

### `split_text_into_chunks.legal_section`

| Parameter | Reference | Meaning |
|---|---|---|
| `enabled` | `true` | Keep a numbered article/section together. |
| `max_chars` | `2500` | Up to this length; beyond it the section is split anyway. |
| `require_caps_first_line` | `true` | Recognise a section by its capitalised opening line. |

### `split_text_into_chunks.sentence_boundary`

| Parameter | Reference | Meaning |
|---|---|---|
| `max_lookahead` | `80` | How far to look for a sentence end rather than cutting mid-sentence. |
| `min_last_chunk_ratio` | `0.4` | A trailing chunk smaller than this fraction is merged back. |
| `short_text_prefix_max_chars` | `220` | Short documents are kept whole up to this length. |

### `validate_text_chunks` — what is not worth storing

| Parameter | Reference | Meaning |
|---|---|---|
| `min_word_count` | `4` | Fewer words is not a passage. |
| `min_lang_score` | `0.55` | Language-detection confidence floor. |
| `langdetect_ignore_word_count` | `5` | Below this many words, language detection is unreliable and is skipped. |
| `bullet_fragment.max_word_count` | `8` | A short chunk that is mostly bullet markers … |
| `bullet_fragment.min_bullet_ratio` | `0.8` | … at this ratio is a fragment, not content. |
| `garbage_fragment.max_word_count` | `15` | A short chunk … |
| `garbage_fragment.max_alpha_ratio` | `0.25` | … with under 25 % letters is OCR noise. |
| `qa_pairs.min_question_lines` | `2` | Recognising a Q&A block: at least two question lines … |
| `qa_pairs.min_non_question_lines` | `2` | … and two answer lines. |
| `qa_pairs.non_question_min_chars` | `20` | An answer line must have some substance. |

### `extract_grade_subject.llm`

| Parameter | Reference | Meaning |
|---|---|---|
| `enabled` | `true` | Let the model assign grade and subject to a chunk. |
| `trigger` | `signal` | Run only when a deterministic signal suggests a grade is present — not on every chunk. |
| `batch_size` | `20` | Chunks per call. |
| `max_chunk_chars` | `600` | Characters per chunk sent. |

### `split_text_into_chunks.llm`

| Parameter | Reference | Meaning |
|---|---|---|
| `enabled` | `true` | Let the model propose the split points. |
| `max_chars` | `6000` | Maximum text sent in one call. |
| `min_coverage_ratio` | `0.5` | If the returned split covers less than half the input, the deterministic split is used instead. |

---

## 5. Things that look like settings but are not

| Concern | Where it actually lives |
|---|---|
| LLM prompts | `edu_ai.prompt_registry`, versioned, fetched by technical key |
| Wording shown to the user | `edu_ai.system_message` (one row per language), `edu_ai.ui_label` |
| Stop words | `edu_ai.qa_stopword` |
| Synonyms and abbreviations | `edu_ai.qa_synonym` |
| Blocked topics | `edu_ai.qa_blocklist` |
| Languages | `edu_ai.code_list(language)` |
| Grades, subjects, document types, domains | `edu_ai.code_list`, `edu_ai.taxonomy_rules`, `edu_ai.taxonomy_binding` |
| Models used by ingestion components | the component's own input in the Langflow UI, not this document |

The rule: **behaviour** is configured here; **content** — anything a school
might want to word differently — belongs in its own table.

---

*Updated by Martin Šafránek on 2026-08-28.*
