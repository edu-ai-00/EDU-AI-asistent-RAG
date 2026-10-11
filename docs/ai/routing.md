---
id: docs.ai.routing
status: current
components: [router, api]
sources:
  - search/app/nodes/route.py
  - search/app/nodes/resolve_context.py
  - search/app/executor.py
  - search/app/messages.py
  - search/app/scope.py
  - search/app/domain_scope.py
  - search/app/server.py
  - search/app/conversation.py
  - search/app/settings.py
  - search/app/config.py
last_verified: 2026-09-18
---

# Routing

**Track:** human (AI-relevant). How a question is admitted, scoped and ordered
through the cascade. Control types: `enforced` (deterministic code) · `prompt`
(prompt-requested, not guaranteed) · `evaluated` (checked by an eval set) ·
`intended` (desired, unenforced — a gap).

Acronyms: **QA** = curated question/answer pack; **RAG** = retrieval-augmented
generation.

## Two things called "routing"

1. **The `route` node** — a per-query guard plus pack scope. No LLM, no
   retrieval.
2. **The cascade order** — which retrieval steps run, in what order, and under
   what condition. This is data: it lives in `edu_ai.settings.config.pipeline`
   and is walked by `search/app/executor.py`. A built-in fallback order applies
   only when the table has no active row.

## Cascade order (`enforced`, data-driven)

Reference order:

```
route → qa_exact → qa_lexical → document_lexical → rag_keywords → qa_vector
      → rag_vector → document_siblings → answer
        └───── curated QA ─────┘ └──── documents (gather) ────┘ └ LLM ┘
```

- A step with `on_hit: stop` ends the cascade once it resolves the query.
  `on_hit: continue` steps only add candidates to the pool.
- Run conditions (`only_if_empty`, `only_if_max_coverage_below`,
  `run_on_timespan`) are evaluated per query, so an enabled step may still not
  run. Their meaning is documented in [retrieval.md](retrieval.md).
- Principle: **curated QA beats generated RAG; lexical beats vector.**
  `qa_vector` is the last curated gate before the generated answer.
- Any step can be disabled or reordered by editing the settings row — no code
  change. An unknown step name is skipped with a warning.

Which steps are enabled in the reference configuration, and why, is documented in
[../SETTINGS_REFERENCE.md](../SETTINGS_REFERENCE.md) §3, the canonical owner of
those values.

## Before the cascade: follow-up resolution

`resolve_context` rewrites an elliptical follow-up ("and when is that?") into a
standalone question using the recent turns of the session. It is **not a pipeline
step**: it must run before the token forms are built, and the pipeline starts
after that.

It is **enabled in the reference configuration** (`context.enabled: true` in all active seed rows; code default is also on; it needs a `conversation_id` with prior turns and costs one small LLM call when a rewrite is needed). If disabled in an organisation's settings, a
follow-up is answered by retrieval alone, so a question that carries no subject
of its own depends on whatever the words it does contain retrieve.

## The `route` node

### 1. Usability guard (`enforced`)

If a question reduces to no usable tokens at all — punctuation only, for example
— the cascade short-circuits with an `unusable` outcome and a message from the
message table. No retrieval runs.

### 2. Meta and identity answers (`enforced`)

Two kinds of question are not questions for the corpus, and both are answered
here without retrieval or an LLM:

- **"Co umíš?"** — the capabilities message, with the school's name and the live
  section headings of its own documents.
- **"Jak se jmenuje naše škola?"** — the school's name, read from
  `legal_entity.display_name`.

Detection is data in both cases: `edu_ai.qa_blocklist` rows with category `meta`
and `identity` hold the folded phrases per language, and the matcher excludes
both categories, since they are detection vocabulary and not terms to block on.
Matching is stricter than the blocklist: the **whole** query, after its filler
words are stripped, must equal a phrase. "Co umíš spočítat?" and "Jak se jmenuje
školník?" are real questions and stay on the cascade.

The identity path exists because the name is **metadata, not corpus**. It is in
no index, so no wording can retrieve it — measured on production 2026-09-11,
where an entity whose documents name it ten times still answered `no_answer`:
`skola` sits above the BM25 document-frequency ceiling and `jmenovat` has df 0
(`ZS_JEDOVNICE`, log 3926; `ZS_KAVCI_HORY_ZS`, log 3914).

Both answers report `answer_source: meta`; `flow_trace` tells them apart through
`trace_detail.guard` (`meta` or `identity`). Deactivating the rows in
`qa_blocklist` or `system_message` disables either path without a deployment. If
the entity has no name, or the message row is missing, the query falls through to
the cascade rather than returning a half-rendered sentence.

### 3. Blocklist guard (`enforced`)

Terms come from `edu_ai.qa_blocklist` (active rows), each carrying a category and
a severity.

- **Single terms** match whole tokens only: a term hits when it equals a `norm`
  or a `lemma` token. Never a substring — this avoids matching an innocent word
  that contains a blocked one.
- **Phrases** match as a whole-word sub-sequence of the query's tokens, with a
  configurable slack so an inserted word does not defeat the match.
- When more than one category hits, a configured category order decides which
  one wins.

**What happens on a hit is configuration, not code** (decision D-16). The
settings row maps a category to an outcome and to a message key; the wording
lives in `edu_ai.system_message`, one row per language. A severity threshold per
category decides how strong a hit has to be to block at all.

The important consequence: a self-harm hit is answered with a crisis line and an
invitation to talk to a trusted adult — **not** with a refusal. Changing that
wording, or which categories block, requires no deployment.

**Failure behaviour:** if the blocklist cannot be read, the guard is skipped and
the query proceeds, logged. Availability over enforcement.

### 4. Pack and entity scope (`enforced`)

The node resolves which curated QA packs the caller may see, from the caller's
legal entity, role and year, together with pack validity. A caller entitled to no
pack at all is given a sentinel that matches nothing — deliberately, because an
empty list is read downstream as "all active packs", and the two must not be
confused. **A failure of pack selection now also yields that sentinel**: it used
to return the empty list, which opened every active pack of every school on the
one path nobody exercised.

The same entity and role rules are applied by every retrieval step through
`search/app/scope.py`. Visibility is described in [retrieval.md](retrieval.md).

### 5. Thematic area scope (`enforced`)

A request may carry `domains` — a set of DOMAIN codes from the caller's school
catalog — and every source the answer can come from must then carry at least one
of them. Absent or `null` means the search that ran before this field existed.

**It restricts, it never permits.** The area predicate is a separate SQL
fragment from the identity one (`scope.domain_where` beside `scope.where`) and
the two are ANDed, so a chosen area cannot reach a document the caller's entity,
role, visibility or validity would have excluded. Everything below is a
*narrowing* of an already-permitted set.

Three states, and the last two are not the same thing:

| Resolved value | Predicate | Meaning |
|---|---|---|
| `None` | *(no fragment at all)* | no thematic restriction; the SQL is byte-identical to the pre-feature query |
| non-empty ids | `domain_ids && ARRAY[…]` | the row must carry at least one chosen area |
| empty ids | `AND false` | the codes were valid and label nothing reachable — "no rows", **never** "no filter" |

Two operations that look like one (`search/app/domain_scope.py`): a code is
**validated** against `resolved_taxonomy_rules(entity, 'DOMAIN')`, which is
nearest-owner-exclusive, so a code another school defines is unknown here; and
it is then **expanded** to every active taxonomy id of that code owned inside
the caller's organisation or globally. Both halves are needed — one code has
several ids, and an organisation labels its own material with its own.

An **explicit filter excludes unlabelled sources**: array overlap is false for
NULL and for an empty array alike. An untagged source is not "in every area", it
is one nobody has classified yet, and which area it belongs to is a curatorial
decision — see [../DATA_MODEL_AND_SETUP.md](../DATA_MODEL_AND_SETUP.md).

**Where it applies in this node.** The manifest's own `domain_ids` filters the
pack pre-selection, so a pack that cannot contribute is not scanned — and the
*offer* list (the other-year safety net) is filtered the same way, because a
button is as much a result as an answer. This is a pre-selection only: a mixed
pack passes on any one of its areas and is then narrowed row by row, per entry —
see [qa-fastpath.md](qa-fastpath.md).

`route._corpus_topics` — the "what can you answer?" offer — filters **both** of
its halves itself, the curated questions and the document headings, because it
runs before `pack_ids` exists and can inherit nothing. Its heading query now
also joins metadata on `file_uuid` **and** `processing_uuid`; on `file_uuid`
alone the active metadata row matched the chunks of every processing run of that
file, so a re-ingested document could offer a heading its current version no
longer has.

**No automatic widening, anywhere.** When nothing in the chosen areas matches,
the answer is the existing `no_answer`; the client may offer an explicit "search
every area" action, which is a new request with `domains: null`. A failure in a
helper step never turns an explicit filter into an unrestricted search — the
cascade falls through to the next *restricted* step and finally to `no_answer`.

**`domain_match_boost` is unchanged.** It orders results *within* what the
filter already admitted; it has never been able to widen a scope and still
cannot.

### 6. History, scoped the same way (`enforced`)

Both readers of earlier turns filter by the request's areas, null-safely
(`domains IS NOT DISTINCT FROM …`): `resolve_context._load_turns`, which feeds
the follow-up rewrite, and `conversation.load_seen`, which stops a chip being
offered twice. A row with `NULL` belongs to the unrestricted scope **alone**.

The client is expected to start a new conversation when the area changes, but
the server cannot rely on it — `conversation_id` arrives from the client and a
direct API caller may reuse it. Without this, switching area kept the previous
area's questions suppressed and rewrote a follow-up against a turn about
something the user had left. A failed history lookup costs the history, never
the filter.

The QA probes that decide *whether to spend a rewrite* also take the filter, and
here it works the other way round: a curated answer in an area the caller
excluded can never answer this turn, so letting it match would block a rewrite
the turn genuinely needs.

## Not done here

- **Intent routing** (question vs. document discovery) is deliberately not
  performed (`intended`). It would need an explicit request field or an LLM;
  every usable, admitted question proceeds through the whole cascade.
- **A response cache** before retrieval does not exist (`intended`) — see
  [caching.md](caching.md).

## Conflicts

If routing signals conflict — a blocklist hit on a question that would also match
a curated QA entry — the **guard wins**. It runs first and short-circuits before
any retrieval step. There is no scoring between the guard and retrieval.
