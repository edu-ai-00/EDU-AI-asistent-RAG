---
id: docs.ai.caching
status: current
components: [cache, api]
sources:
  - search/app/settings.py
  - search/app/idempotency.py
  - search/app/server.py
  - search/app/log.py
  - search/app/conversation.py
  - search/app/nodes/resolve_context.py
  - postgres/init/213_log_domains.sql
last_verified: 2026-09-06
---

# Caching

**Track:** human (AI-relevant). What is and is not cached. Control types as in
[routing.md](routing.md). "Normalized query" means the parity token forms
([../../contracts/parity/](../../contracts/parity/)).

## What exists today

### Configuration cache (`enforced`)

`search/app/settings.py` caches the resolved active configuration — pipeline,
engines, normalization and the injected stop list — per legal entity, to avoid a
database round-trip on every query.

- **Keying:** by the caller's legal entity, because configuration resolves per
  organisation (see [../SETTINGS_REFERENCE.md](../SETTINGS_REFERENCE.md) §1).
- **Staleness:** bounded by a short TTL, currently **5 seconds**. Editing the
  settings row therefore takes effect within seconds and needs no restart.
  `refresh()` and `get_active_setting(force=True)` bypass it outright.
- **Why a TTL at all:** the cache used to live for the whole process lifetime and
  nothing ever called `refresh()`, so a setting changed in the database kept
  being ignored until the container was restarted — measured on the
  ingest-visibility switch, which was flipped and had no effect. A short TTL
  keeps the round-trip amortised while making an edit actually take.
- **Failure:** if the table has no active row, a built-in fallback configuration
  is cached instead, so a configuration problem degrades search rather than
  taking the service down.

### Idempotency replay cache (`enforced`)

A request carrying an `Idempotency-Key` is claimed in `idempotency_key` before
the cascade runs, and its response is stored against that key.

- **Keying:** the account plus the client's key.
- **Behaviour:** the same key with the same body replays the stored response
  immediately; the same key with a **different** question is a conflict, not a
  silent overwrite; no key at all means a normal, recomputed request.
- **In-flight:** a claim with no response yet means "running"; a claim older than
  five minutes is taken over, so a crashed request does not block its key
  forever.
- **Retention:** 24 hours, swept by this service.

This is a *replay* cache keyed by an explicit client token — not a semantic
cache. Two clients asking the same question get two full computations.

**The digest and the `domains` field (2026-09-06).** The digest is the SHA-256
of the canonical `model_dump()` of the request, so **adding a field with a
default changes the digest of every request that does not send it** — including
one a client sent minutes ago and is now retrying, whose stored claim was hashed
by the previous code from a dict with no such key. That retry would come back
`IDEMPOTENCY_CONFLICT`: a client that did nothing wrong told its answer belongs
to somebody else.

So `server._hashable_body` drops `domains` before hashing **when it is null**,
reproducing the old dict exactly. One field, one rule, and it is a compatibility
rule rather than a policy:

- it applies **only** to `domains`, because it is the field added *after* claims
  could already exist. Every other default (`retrieval_only`, `debug`, …) was in
  the digest when those claims were written, so omitting one now would break the
  very compatibility this exists to keep. It is not a general "skip the
  defaults";
- an **explicit** filter goes into the digest in **canonical** form, taken from
  the resolver and not from the request. `["B","a"]` and `["A","b"]` are the same
  set and must replay each other; the same key with a genuinely different scope
  must be a 409, and it is;
- the areas are validated **before** the claim is created, so a 422 or 503
  leaves no claim for the client's retry to collide with;
- an older stored replay simply has no `effective_scope.domains`, which reads as
  the unrestricted scope it was.

TTL, account keying, claim takeover and the stream (which has no replay
mechanism) are all unchanged. Covered by `tests/test_domain_scope_api.py`, which
writes a claim hashed the way the *old* code hashed it and asserts the replay —
two requests against the new code would prove only self-consistency.

### Per-word normalization memoization (`enforced`)

Stemming and lemmatization of a single word are memoized inside the
`eduai_normalize` package. They are pure functions of the input word, so no
invalidation is needed.

## What does not exist

### Normalized-query response cache (`intended` — gap)

There is **no** semantic response cache. Every ask runs the full cascade,
including the embedding, rerank and generation calls. Identical questions from
different callers, or the same caller without an idempotency key, are recomputed.

The intended design — **not implemented, do not describe it as present**:

- **Key:** the normalized query (parity token forms, so trivially different
  phrasings collapse), plus the pack and entity scope, plus the configuration
  version. All three are needed: the same words asked by a different entity, or
  under a different configuration, are a different question.
- **Value:** the response.
- **TTL:** to be decided, in seconds, and it MUST be bounded so that an edit to a
  curated answer surfaces. Invalidation on re-ingest is an open decision.

Until it exists, treat every request as a cold computation.

## Conversation and turn tracking

Conversation and session identifiers are accepted and logged, and they are what
`resolve_context` would read to rewrite an elliptical follow-up into a standalone
question. That step is **enabled in the reference configuration** (`context.enabled: true` in all active seed rows; code default is also on; it needs a `conversation_id` with prior turns and costs one small LLM call when a rewrite is needed); where a deployment turns it off, each
request is answered independently, with no prior-turn context.

Nothing is cached across turns in either case: the follow-up mechanism rewrites
the *question*, it does not reuse a previous *answer*.

### History is scoped by area (`enforced`)

Both readers of earlier turns — `resolve_context._load_turns` and
`conversation.load_seen` — filter on `question_answer_log.domains` with
`IS NOT DISTINCT FROM`, i.e. NULL-safe **equality** on the canonical array. Not
overlap and not containment: a turn asked in one area must not suppress a chip
in another, and plain `=` would drop every historical row even for an
unrestricted request, because `NULL = NULL` is unknown. A row with `NULL`
belongs to the unrestricted scope **alone**.

The canonical form is what makes the comparison meaningful: the codes are stored
trimmed, upper-cased, deduplicated and **sorted**, so two spellings of one
selection are one value.

The client is expected to start a new conversation when the area changes, and
the web UI does. The server does not rely on it: `conversation_id` arrives from
the client and a direct API caller may reuse it. A failed history lookup costs
the history and never the filter.

### The `domains` column (migration 213)

`question_answer_log.domains text[] NULL`, additive, no default and no backfill.
It stores **canonical codes, not taxonomy ids** — an id differs per school for
one and the same code, so an id column could not be compared across turns and a
re-tagged catalog would silently change what a historical row meant.

`NULL` rather than `'{}'`: NULL *is* the meaning of "no thematic restriction",
and it is what every pre-existing row and every client without the field
writes. A `DEFAULT '{}'` would make "unrestricted" indistinguishable from
"explicitly empty" — which the API rejects as a 422 and can therefore never
reach the table — and would rewrite the meaning of every historical row. No
backfill for the same reason: the area a past question was asked in is not
recoverable from its text, and guessing it would poison the very comparison the
column exists for.

Written from **one** normalized representation: `log.py` reads
`resp.effective_scope.domains`, the same value the search ran with and the same
value the client was told, so the three cannot disagree. No index yet — the
read-back queries filter `conversation_id` first, an indexed equality on a uuid,
which leaves a handful of rows for the array predicate.

## Logging is not caching

Query-log writes are append-only, fire-and-forget background tasks that run after
the response has been sent, and are never read back into the request path. They
are analytics, not a cache — and because the query path performs no updates, a
logged answer can never be rewritten after the fact.
