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
  - search/app/settings.py
  - search/app/config.py
last_verified: 2026-08-28
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

It is **disabled in the reference configuration** (`intended`). With it off, a
follow-up is answered by retrieval alone, so a question that carries no subject
of its own depends on whatever the words it does contain retrieve.

## The `route` node

### 1. Usability guard (`enforced`)

If a question reduces to no usable tokens at all — punctuation only, for example
— the cascade short-circuits with an `unusable` outcome and a message from the
message table. No retrieval runs.

### 2. Blocklist guard (`enforced`)

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

### 3. Pack and entity scope (`enforced`)

The node resolves which curated QA packs the caller may see, from the caller's
legal entity, role and year, together with pack validity. A caller entitled to no
pack at all is given a sentinel that matches nothing — deliberately, because an
empty list is read downstream as "all active packs", and the two must not be
confused.

The same entity and role rules are applied by every retrieval step through
`search/app/scope.py`. Visibility is described in [retrieval.md](retrieval.md).

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
