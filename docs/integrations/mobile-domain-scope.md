# Search domain selection — contract for the mobile app

Prepared 6 Sep 2026. Describes an **API that is finished and tested** against the
real deployed interface; the mobile client is **not yet implemented** against it,
and this document is a brief for the mobile team, not a description of a released
feature. The mobile source code is not in this repository. The existing Flutter
app is at [EDU-AI-asistent-APP](https://github.com/edu-ai-00/EDU-AI-asistent-APP);
a read-only check on 11 Oct 2026 verified main `e653aea54d5e408bd3667f4ab087e0920814cc76`.
The wording "not yet implemented" here refers to this integration, not to the
existence of the app as a whole. The [QA offline audit](offline-qa-rules.md) describes
the verified online chat, local cache and the missing QA-pack engine.

Design and decisions: [`specs/draft/spec.search-domain-scope.md`](../../specs/draft/spec.search-domain-scope.md).
Server behaviour: [`docs/ai/routing.md`](../ai/routing.md) §4–5.
Error envelopes: [`contracts/errors.yaml`](../../contracts/errors.yaml).

---

## 1. What it does

The user selects one or more **domains** (attendance, catering, admissions…)
and search then draws **only from sources labelled with at least one of them**. The
mobile app can also set the domain according to the open section of the app, without
the user choosing manually — it is an ordinary request parameter.

Three things it **does not do**, because each of them has already been expected by someone:

* **It is not a permission.** The filter can only remove sources. School, role,
  visibility and validity apply behind it unchanged, so by choosing a domain nobody
  gets access to anything they would not see without it.
* **It does not classify text.** The label is on the *document* and on the *answer*,
  not on an individual paragraph. A long document (the school rules) can have several
  domains, **is not split**, and is reachable in each of them as a whole; which
  paragraph answers is decided by the query, as before. Practical consequence: an
  attendance query entered in the "Stravování" domain can still find the attendance
  passage of such a document — that document really is also about catering.
* **It does not widen by itself.** When there is nothing in the selected domains, the
  existing `no_answer` is returned. Search is never automatically repeated without the filter.

---

## 2. Domain catalog

```
GET https://<host>/search/v1/domains?legal_entity=ZS_KAVCI_HORY_ZS
```

```json
{
  "legal_entity": "ZS_KAVCI_HORY_ZS",
  "domains": [
    {"code": "SECONDARY_ADMISSIONS",
     "labels": {"cs": "Přijímačky na střední školu", "en": "Secondary-school admissions",
                "de": "Aufnahme an die Mittelschule", "es": "Admisión a la escuela secundaria"}},
    {"code": "ATTENDANCE",
     "labels": {"cs": "Docházka a absence", "en": "Attendance & absence"}}
  ]
}
```

**Take codes and names from here.** The app must not contain its own list of codes or
its own translations: the catalog is **per school** (in production 8 global ones, 11 for
both Kavčí hory units) and a private copy would drift away from the database. A new
language is just another key in `labels`, not a new app version; handle a missing
language with the fallback `labels[language] → labels["cs"] → code`.

`legal_entity` in the response is the school for which the catalog was **actually**
resolved. Store the selection **under this value**, not under the one you asked about.
A catalog loaded for one school and used in a search for another gives codes that the
server rejects with 422.

The endpoint has been in the nginx allowlist since 6 Sep 2026; until then it returned 404. It is
reference data like `/search/v1/code-lists` — it can be cached and contains no user
content. Without `legal_entity` it returns the global set, which is only
compatibility for other callers; **client search always sends the real
school**.

An unavailable catalog is `503 UNAVAILABLE` with `retryable: true`. Show the error and
offer a retry — **not** an empty list: an empty list reads in the UI as "the school
has no domains" and the filter silently disappears.

---

## 3. Query

```
POST https://<host>/search/v1/ask
POST https://<host>/search/v1/ask/stream        (SSE, same semantics)
```

```json
{
  "question": "Kdy se podává přihláška?",
  "legal_entity": "ZS_KAVCI_HORY_ZS",
  "user_role": "PARENT",
  "user_id": "…",
  "conversation_id": "11111111-1111-1111-1111-111111111111",
  "query_id": "app-42",
  "domains": ["MULTIYEAR_GYMNASIUM_ADMISSIONS", "SECONDARY_ADMISSIONS"]
}
```

| `domains` | Meaning |
|---|---|
| field missing, or `null` | no topical restriction — **exactly the existing behaviour** |
| non-empty array of known codes | OR between domains, AND with all existing filters |

**Omit the field when there is no filter** — do not send `null`. The body is then
byte-for-byte the same as today, which is what keeps the `Idempotency-Key`
compatibility (see §6).

The server **normalizes** the codes: trim, uppercase, removal of duplicates, stable
ordering. `[" attendance ", "ATTENDANCE", "CATERING"]` is the same as
`["ATTENDANCE", "CATERING"]`, so you do not have to deal with ordering or duplicates.

### Response

One field is added in `effective_scope`; the others do not change.

```json
{
  "answer": "…",
  "outcome": "answer",
  "answer_source": "qa_exact",
  "effective_scope": {
    "legal_entity": "ZS_KAVCI_HORY_ZS",
    "role": "PARENT",
    "school_grade": null,
    "language": null,
    "domains": ["MULTIYEAR_GYMNASIUM_ADMISSIONS", "SECONDARY_ADMISSIONS"],
    "identified": true
  }
}
```

`effective_scope.domains` is the **canonical set the server actually used**, or `null`
for unrestricted search. It is always a validated value — even for
`outcome: "role_required"`, where domain validation deliberately ran before the
role check. **Compare it with the normalized sent set**:
strip surrounding whitespace, convert codes to uppercase, remove duplicates
and sort them. For example `[" attendance ", "ATTENDANCE"]` correctly comes back
as `["ATTENDANCE"]`; that is not a scope change or a catalog problem.
Compare a missing field and `null` as the same unrestricted choice.

An unknown or inactive code is rejected by the server with a **422** response; it is not
silently removed in a successful response. If the actual set differs even after
normalization, treat it as a mismatch between request and response to be investigated, not
automatically as a catalog change. Do not loosen the filter yourself because of it.

Internal database IDs of domains are never sent and you do not need them.

---

## 4. Errors

| Status | Code | When | What the client should do |
|---|---|---|---|
| 422 | `VALIDATION_ERROR` | `[]`; empty or whitespace-only element; **unknown/inactive code for this school**; wrong type; one bad code among correct ones | reload the catalog and let the user choose; `fields[].field` is `domains` or `domains[1]` |
| 503 | `UNAVAILABLE` | the catalog could not be read while validating an **explicit** filter | retry with the **same** `domains` |
| 409 | `IDEMPOTENCY_CONFLICT` | same `Idempotency-Key`, different scope | new key — it is a different query |
| 200 | `outcome: "no_answer"` | valid code in which there is nothing | offer the explicit action "Hledat ve všech oblastech" (§5) |

**One bad code fails the whole query.** The server does not silently shrink the set — an
answer to a query about three domains served from two would be an answer to a different question.

```json
{"error": {
  "code": "VALIDATION_ERROR",
  "message": "domains: unknown or inactive domain code(s) 'PRIJIMACKY'",
  "retryable": false,
  "fields": [{"field": "domains[0]",
              "message": "unknown or inactive domain code 'PRIJIMACKY' for this school; see GET /v1/domains"}]
}}
```

**Never repeat the query without `domains`.** Not after a 422, not after a 503. A wider
answer is a different answer, and passing it off as the one requested is exactly the
mistake this filter exists to prevent. Widening is the user's decision.

**With SSE (`/ask/stream`) the validation error arrives as a plain HTTP 422 before
the first event** — the stream does not open at all. Only after the stream has opened is
a failure an `event: error`.

---

## 5. Lifecycle in the app

Five rules; each corresponds to one specific bug that the web version
had to handle (`web_chat/frontend/src/DomainPicker.tsx` is the reference
implementation).

1. **A late catalog does not overwrite a newer one.** When the user switches school
   twice quickly, the first response may arrive last. Keep track of the request
   generation — cancellation alone is not enough, a cancelled request can still complete.
2. **Verify the stored selection, never trust it.** Codes are per school and the curator
   can retire them. Intersect with the new catalog, and **tell the user** what
   dropped out — do not discard it silently, they chose it. Store under the effective entity.
3. **Until you know the catalog, do not send a scoped query.** Neither with old codes (422),
   nor without a filter (it would silently search everywhere).
4. **Changing the school or domain = a new conversation.** New `conversation_id`,
   separated history, the in-flight query cancelled. The server also filters history
   by domain itself — it does not rely on the client's new UUID, because the
   client sends it — but the old chips on screen belong to the old scope and would
   offer queries into a domain the user has just left.
5. **"Všechny oblasti" is a real, always visible choice**, not the absence of a choice, and
   it is the default. Deselecting the last domain returns to it; **never send an
   empty array** (it is a 422). Changing the selection **sends no query by itself** —
   it only determines where the next one will belong.

When you offer "Hledat ve všech oblastech" after a `no_answer`, the click sends a
**new** request: `domains: null`, a new `conversation_id` and a new
`Idempotency-Key`, if you use one.

---

## 6. Idempotency and history

**The `Idempotency-Key` of old requests keeps working.** Before hashing, the server
omits `domains` when it is `null`, so the digest of an unrestricted request is the same
as before this change and a repeat of a query sent by an old app version is
correctly replayed. Verified by a test that creates a claim hashed with the *old* code
(`tests/test_domain_scope_api.py`).

An explicit filter enters the digest in canonical form: `["B","a"]` and
`["A","b"]` are the same query and replay each other, **the same key with a different
scope is 409**. TTL, retention and stream behaviour do not change.

**The server filters history by domain.** A follow-up query sees only earlier
turns of the same conversation entered **in the same scope**; turns without a filter belong only to
unrestricted search. This also holds when you recycle a `conversation_id`.

---

## 7. What does not exist yet and what needs agreeing

* **The mobile app exists.** Its domain and offline QA integration is not evidenced in the inspected APP snapshot; this is not a claim that the mobile app does not exist. The API is finished and verified through the real
  proxy hop; the UI, storing the selection and the release are up to the mobile team.
* **Data.** The filter is only as good as the labelled sources. As of 6 Sep 2026
  production has **five active QA packs (230 answers) without any labelling** —
  they are unreachable under an explicit filter — and `ZS_KAVCI_HORY_SOS` has 104
  active chunks, **none** of which has a domain. Report:
  `python3 scripts/domain_inventory.py --remote edu-ai`. **Do not enable domain
  selection at a school whose content is not labelled** — the user will get `no_answer` to
  questions the system is able to answer.
* **"Přijímačky / Provoz"** as two shortcuts are not the same thing as the DOMAIN catalog.
  If the product needs them, they are separately approved *named profiles*
  (code, translated names, an explicit set of domains) — mobile **must not** guess them from
  the code name, and "Provoz" must not mean the complement of everything else. See spec §8.
