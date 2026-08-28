---
id: docs.ai.caching
status: current
components: [cache, api]
sources:
  - search/app/settings.py
  - search/app/idempotency.py
  - search/app/server.py
  - search/app/log.py
last_verified: 2026-08-28
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
question. **That step is disabled in the reference configuration**, so today each
request is answered independently, with no prior-turn context (`intended`).

Nothing is cached across turns in either case: the follow-up mechanism rewrites
the *question*, it does not reuse a previous *answer*.

## Logging is not caching

Query-log writes are append-only, fire-and-forget background tasks that run after
the response has been sent, and are never read back into the request path. They
are analytics, not a cache — and because the query path performs no updates, a
logged answer can never be rewritten after the fact.
