---
id: spec.search-domain-scope
status: active
components: [api, retrieval, db, webchat, mobile, contracts]
sources:
  - search/app/server.py
  - search/app/state.py
  - search/app/scope.py
  - search/app/executor.py
  - search/app/settings.py
  - search/app/nodes/route.py
  - search/app/nodes/qa_common.py
  - search/app/nodes/qa_exact.py
  - search/app/nodes/qa_lexical.py
  - search/app/nodes/qa_vector.py
  - search/app/nodes/document_lexical.py
  - search/app/nodes/rag_keywords.py
  - search/app/nodes/rag_vector.py
  - search/app/nodes/document_siblings.py
  - search/app/nodes/answer.py
  - search/app/nodes/resolve_context.py
  - search/app/conversation.py
  - search/app/log.py
  - search/app/idempotency.py
  - web_chat/web_chat.py
  - web_chat/frontend/src/App.tsx
  - nginx/conf/nginx.conf
  - contracts/schemas/search-request.schema.json
  - contracts/schemas/search-response.schema.json
  - contracts/openapi.yaml
  - contracts/errors.yaml
  - api/app/main.py
  - search/app/domain_scope.py
  - web_chat/frontend/src/DomainPicker.tsx
  - postgres/init/140_qa_pack_school_grades_domains.sql
  - postgres/init/144_qa_scan_surface_parity.sql
  - postgres/init/213_log_domains.sql
  - scripts/domain_inventory.py
  - scripts/domain_migration.py
  - tests/test_domain_scope.py
  - tests/test_domain_scope_api.py
  - docs/integrations/mobile-domain-scope.md
  - docs/nasazeni/2026-09-06_domain_scope.md
last_verified: 2026-09-06
---

# Search domain selection for the mobile app and web chat

> **Active contract for the backend and web chat.** The implementation and its historical
> deployment are evidenced in the sources and the [runbook](../../docs/nasazeni/2026-09-06_domain_scope.md). The status
> heading here is not a claim of a finished mobile integration or of a regression run on the latest SHA.
> A mobile APP exists, but its domain/offline QA integration is not evidenced in the inspected source; see
> [mobile-domain-scope.md](../../docs/integrations/mobile-domain-scope.md).
> The old counts of unlabelled data and the test results in this specification are historical snapshots.

## 1. Recommended outcome

Both mobile and web chat send the same search API an optional set of `domains` codes.
Mobile can set it according to the open section; web chat offers a selection of the
school's domains and an item "Všechny oblasti". The selected domain is always visible in the client.

The first version uses the existing school DOMAIN catalog. It creates no new taxonomy,
no new production dependency and no new switch in settings. The precise restriction rests on
correct labelling of sources, not on guessing the domain from the query text.

Guarantee: answers, generation inputs and suggested questions come only
from sources labelled with at least one selected domain and at the same time available
under the existing rules. A domain adds no permission. A document or answer
with several domains may contain several related topics; the quality and granularity of the
labelling are part of data acceptance. The filter alone does not verify the meaning of every sentence.

After a domain is chosen, search does not widen itself to all domains. When nothing
matches, it returns the existing `no_answer`; the client may offer an explicit action
"Hledat ve všech oblastech". That sends a new request with `domains: null`.
Without the filter, source selection and cascade order stay as they are.

## 2. Verified baseline

- Local and production checkout: `e0316a6`. The content of 14 checked files of the
  search service matched the running container; the BFF differed only in a comment.
- `/v1/ask` today rejects `domains` with HTTP 422 as an unknown field.
- The internal `/v1/domains` works; `/search/v1/domains` through the production nginx
  returns 404. Without an entity the catalog returns GLOBAL.
- `CHAT_ENTITY_PICKER=true`. Both SOŠ and ZŠ Kavčí hory have 11 domains; SOŠ also has
  `CATERING`. Jedovnice and Svobodná škola inherit eight global domains.
- In all four active settings, route, all three QA steps, document_lexical, rag_vector
  and answer are enabled. rag_keywords and document_siblings are disabled, but their
  implementations too must respect the new filter.
- Follow-up query rewriting is enabled. Neither the history nor the record of already
  offered questions distinguishes the topical scope today.

| Active QA pack | Active answers | Today's domains |
|---|---:|---|
| faq-jpz | 110 | Both admissions codes on all answers |
| faq-provoz | 100 | None |
| provoz-sos | 33 | The same seven domains on all answers |
| prijimacky-jed | 32 | None |
| provoz-jed | 33 | None |
| prijimacky-svo | 32 | None |
| provoz-svo | 33 | None |

| Entity | Chunks attached to active document versions | Of which with a domain |
|---|---:|---:|
| ZS_KAVCI_HORY_ZS | 365 | 346 |
| ZS_KAVCI_HORY_SOS | 104 | 0 |
| ZS_KAVCI_HORY | 186 | 29 |
| SVOBODNA_SKOLA_PRAHA | 82 | 19 |

Chunk counts are not result counts for a specific role and date; other filters
may narrow the set. The state is a snapshot; re-measure before preparing the data.

**Re-measured on 6 Sep 2026** with `scripts/domain_inventory.py --remote edu-ai`
(read-only). The numbers above are confirmed; in addition it showed what is not visible from the application:

| finding | number |
|---|---:|
| active QA packs without any labelling | **5** = 230 answers |
| chunks **without** a domain under a **labelled** document | **170** (151 `ZS_KAVCI_HORY`, 19 `ZS_KAVCI_HORY_ZS`) |
| chunks with a **different** set than their document | **94** (`ZS_KAVCI_HORY_ZS`) |
| `qa_variants`/`qa_embedding` vs `qa_entries` | 274 rows, **all 274 only a different array order** → harmless for array overlap |

The last two rows are what §6 anticipated: **a plain UPDATE of the metadata
is not enough**. At the same time it is the counterpart of the first — the order mismatch is cosmetic and
the report therefore lists it separately, otherwise the "drift" row would teach everyone
to ignore it.

Availability through role and validity (what the user actually sees, not what is
labelled) is a separate section of the report: for `PARENT` 717 chunks, of which 394
labelled, and 3087 QA variants, of which 973 labelled.

## 3. Common contract

| `domains` input | Meaning |
|---|---|
| Field missing or `null` | Existing search without topical restriction |
| Non-empty array of known codes | OR between domains, AND with the other filters |
| `[]`, empty string, unknown/inactive code, wrong type | 422 `VALIDATION_ERROR` with `fields` |
| Valid code without matching content | Successful query, possibly `no_answer` |
| Unavailable catalog while validating an explicit filter | Retryable service error; no search without the filter |

The server normalizes codes by the same convention as ingest: trim and uppercase,
removal of duplicates and stable ordering. It does not silently drop an empty element.
Membership is validated against `resolved_taxonomy_rules(entity, 'DOMAIN')` after
the existing verification of entity and caller. Specific codes and their IDs are data.

Example for the ZŠ Kavčí hory catalog; the client takes entities and codes from the catalog:

```json
{
  "question": "Kdy se podává přihláška?",
  "legal_entity": "ZS_KAVCI_HORY_ZS",
  "user_role": "PARENT",
  "domains": ["MULTIYEAR_GYMNASIUM_ADMISSIONS", "SECONDARY_ADMISSIONS"]
}
```

`effective_scope.domains` returns the normalized set of codes actually used,
or `null` for unrestricted search. It is an additive response field. Carry it
also through the BFF and into the final SSE response; do not return internal database IDs.
For answers ended before retrieval, do not present unvalidated input as a
verified filter in use.

`/ask` and `/ask/stream` share the preparation and validation of scope before the cascade starts
and before SSE is opened. `/explain` is wired to the same preparation of entity,
caller, role and domains, but remains a diagnostic deterministic prefix
without generation. Today the shared request model alone does not give it identical behaviour;
describe and test its extension explicitly as a change to the diagnostic contract.

Catalog for mobile: `GET /search/v1/domains?legal_entity=…`, newly in the nginx
allowlist. The existing internal behaviour without an entity stays for compatibility,
but client search always uses the real entity.

## 4. Resolving code versus database ID

Validating a code and looking up the ID for the filter are two different operations:

1. Verify that the selected codes exist in the calling school's catalog.
2. Look up all active DOMAIN IDs of those codes with an owner inside
   the caller's organization or with a global owner. This also covers the
   classification of sources published by a parent organization.
3. At the same time apply to the sources the original scope of entity/visibility/role/validity
   and the active-document-version filter. Expanding the set of IDs releases none of this.

Production example: `SECONDARY_ADMISSIONS` is ID 13 in the ZŠ catalog, ID 42
globally. Five active chunks of the organization carry `{42,24}` and are published for
parents and teachers. A parent from the ZŠ must not lose them when choosing this code.

Implement a common parametrized helper in `scope.py`, for example
`domain_where(alias, column, domain_ids)`. The alias/column is chosen by code, never
by the request. `None` means no restriction; an empty resolved set means
`AND false`, never omission of the filter. With an explicit scope, NULL/empty labels
of a source do not match. Identity and permissions stay in the existing `scope.where()`.

## 5. Places in the code and their obligations

| Place | Change |
|---|---|
| server.py, state.py | Shared validation, normalized codes and internal IDs in state, effective_scope |
| route._scope | Intersection of manifest domains; no domain-outside packs in offer_pack_ids |
| qa_common, qa_exact, qa_lexical, qa_vector | Common QA fragments including the filter on domains of scanned rows; keep the pack allowlist and the existing permissions |
| Categories and related chips in qa_common | The same domains as the answer; no path for topping up chips outside the filter |
| route._corpus_topics | Filter QA questions and document offers directly; runs before pack_ids are assembled |
| Other-grade suggestions | May continue to relax the grade according to today's function, not the domain |
| document_lexical, rag_keywords, rag_vector, document_siblings | Filter on document_chunks.document_domain_id before LIMIT, including the neighbours that are added |
| answer | Processes the already restricted candidates; no restoring of dropped sources or suggestions |
| resolve_context, conversation | History and deduplication of offers in the same domain scope |
| log.py | Store normalized codes in the query log |
| web_chat.py, App.tsx | Catalog, selection, transfer, errors, conversation lifecycle |
| nginx, contracts | Catalog available to mobile, described request/response and error states |

Use the already existing domain columns in the QA tables. The manifest serves as a
preselection; it is not the sole evidence of relevance of an individual answer. The common QA
helper is to prevent divergence of the main answer, top-up chips and direct offers.
Review all ranking statistics tied to the candidate corpus
together with the filtering, so that thresholds and denominators do not change unintentionally after narrowing.
The document offer in route._corpus_topics today joins metadata only via
file_uuid. In this affected path also add a match on processing_uuid, so that
active metadata does not admit chunks from an older version of the same document.

Keep `domain_match_boost` and automatic detection as the existing ordering
within the permitted results. They are not entitled to widen the scope; switching them
off wholesale is not a necessary part of this change.

A failure of a helper step must not turn an explicit filter into unrestricted search.
Also fix the relevant fallback in route, which today can return `pack_ids=[]`
meaning "all packs". On a selection error use the existing sentinel for
no pack. A failure of a retrieval step continues to fall through to the further restricted steps
and finally to `no_answer`. Do not change the cascade order or `on_hit`/run conditions.

## 6. Data preparation without a new classification model

Before the user-facing launch, produce a report per school and source: current domains,
missing/inactive/foreign IDs, breadth of labelling, counts of answers and active chunks.
The report must also include the available organization sources. A missing domain is not
a reason to copy another school's catalog or to label a source with all domains.

The recommended first procedure for QA is to split mixed packs into topically
coherent packs via the existing import/build/export path. Keep questions,
answers, roles, validity, grades and citations. Several domains on one pack are
acceptable only when the labelling matches its answers.
Do not automatically consider all JPZ questions relevant for both admissions
domains; such assignment must be checked by a curator.

Before splitting, verify on a test copy the creation of entry_id, moving between packs,
the behaviour of a repeated import and the transfer of categories/thresholds. A change of identity
or pack can change the selection of matching questions and related chips. Prepare a map
of old and new identifiers if the build does not preserve identity. Retire the old packs
only when the replacements are activated, so that a double active set never remains.

If the splitting runs into a large number of packs or unstable identities, a separate
follow-up proposal may extend the import with curated domains of individual
answers. It cannot be inferred from the existing columns that such an import already works.
This unverified assumption does not belong in the first implementation.

For documents, add the declared domains via the existing ingest and recompute the
labelling of the affected chunks. A manual fix only in document_metadata is not enough.
Large documents are not split for this feature: the school rules may, for example, cover
both attendance and assessment and be available in both domains. Chunks inherit the effective
domains of the document; the relevant passages are selected by the existing retrieval according to the query.
Do not accumulate all domains on a document just to increase coverage.

The existing ingest has this propagation. For new ingestion keep the path through
ingest. For existing content, before a wholesale re-ingest first examine a targeted
migration of metadata and derived labels in a single transaction, including dependent bindings.
If their consistency cannot be ensured without a build, use a verified targeted
re-ingest. No change of texts, chunking or embeddings is required; do not create
a new LLM chunk classifier. Do not re-ingest untouched documents.

Data acceptance is not "every row has an arbitrary domain". A curator must confirm
the assignment, resolve the five unlabelled active QA packs and the sources without domains
within the scope of the launch; any deliberate exclusions must be stated explicitly in the report.
Historical versions and inactive packs do not need to be re-tagged for this feature.

## 7. Clients, history and compatibility

Web chat gets `GET /chat/api/domains?legal_entity=…`. The BFF determines the effective entity
the same way as for message: if the picker is off, it uses its fixed entity.
It cannot load the catalog of one school and then search in another.

When loading and changing the school, the client cancels the old fetch or ignores its late
response. It keeps the stored selection per entity and verifies it against the new catalog.
Until verification completes it does not send a query with the old codes. If a code has disappeared,
it shows the need for a new choice; it does not automatically repeat the query with a wider scope.
When the catalog is unavailable it offers an error and a reload, not a silent filter reset.

Changing the school or the set of domains starts a new conversation; the UI separates the old
history and the old chips. An in-flight request is cancelled or its late
response is not added to the new conversation. Mobile gets the same rules.

The server additionally filters history for resolve_context and conversation.load_seen
by the normalized `domains`. It cannot rely only on the client's new UUID.
Older rows with NULL belong only to unrestricted search. The QA probes that decide
on rewriting a follow-up query are to respect the domain under an explicit filter;
a match in another domain must not prevent a necessary rewrite.

For this purpose the proposal contains one additive migration:
`question_answer_log.domains text[] NULL`, without back-guessing values for
historical queries and without a new index until measurement proves the need.
The logging codes must be read by all paths including the stream. A log failure
does not change the scope; if history is unavailable, the query is processed standalone.

The BFF carries the validated structural field `domains` to search; the content of the catalog
is authoritatively verified by search. An upstream 422 for domains must be returned by the BFF as
a correctable 422 with `fields`, instead of today's generic 500. Adapt also the error
envelope for structural errors of the ChatRequest itself. The client reloads the catalog
and asks for a new choice, never changing the query to "all" by itself.

When updating idempotency, preserve the digest of old unrestricted requests:
the new model adds the default `domains: null`, so a plain model_dump would change the
hash even for original clients. Before hashing, omit exactly the new field if
it is NULL; keep the other existing fields and their defaults. An explicit
non-empty domains enters the hash in canonical form. The same key with a different
scope must return 409, not someone else's response. The TTL/claim/replay rules do not change.
An older replay without effective_scope.domains is compatible with the unrestricted scope.

`selected_answer_key` remains only a logged value, as today's contract says.
A click sends the text of the offered question and the current scope. This change does not introduce
direct loading of an answer by entry_id.

## 8. If the product needs exactly "Přijímačky / Provoz"

These two items are not the same as today's DOMAIN catalog. The first version above
lets users select the real domains; it does not itself introduce two universal shortcuts.
Mobile must not guess a group from the name of a code, and "Provoz" must not mean
automatically the complement of all other domains.

If these shortcuts are a release condition, add before the client stage
separately approved named profiles: profile code, translated names,
owner/entity and an explicit set of domains. The backend publishes their resolved
mapping for a school; both apps use the same mapping and still send
`domains`. Placing profiles in a new table or per-org configuration requires
a decision on data management and a migration; this proposal introduces no such storage.
The number of profiles is not a technical reason for postponement; the product need decides.

## 9. Order of implementation and rollback

| Stage | Concrete output | Condition to continue |
|---|---|---|
| A — confirm the contract and inventory | This draft, a fresh report of sources, a decision on real domains versus named profiles | Agreed scope of UI and data |
| B — backend on a test copy | Log migration, scope helpers, endpoints, catalog through nginx, contracts and tests | Negative scope tests and old unrestricted requests pass |
| C — data on a test copy | Curator-verified packs and documents, exports for deployment, identity map and a rollback procedure | Content review and repeated regression |
| D — web and mobile | The same choices, errors, conversation switching, API examples for the mobile team | Both clients pass the same scenarios |
| E — production | Backup, additive migration, search build, nginx configuration, API verification, prepared data, BFF/UI and then mobile | Verification against the actual deployed API before enabling client selection |

The technical data preparation can proceed in parallel with the backend. The client launch
waits for data acceptance at the target schools; the switch alone does not fill in missing
content. The new backend is first compatible with old clients without domains.
Deploy the log migration before the code that writes to the new column.

On rollback, the client UI can be reverted while keeping the additive backend and the column.
Reverting search to an older version is possible only after scoped requests from
all clients have stopped, including the already released mobile; the older API rejects them with 422.
The filter must not be dropped at the proxy/BFF as an emergency workaround.
Data rollback uses verified exports and activation of the original set, not a coexistence
of old and new packs. Removing the log column is not part of the rollback.

## 10. Acceptance scenarios

1. A missing domains and NULL preserve the existing scope, outcomes and the old
   idempotency digest; unchanged retrieval fixtures give the same candidates.
2. A single domain and OR of several domains work in QA exact/lexical/vector and in documents.
   Empty and foreign values return the same error envelope through the API and the BFF.
3. A valid code with zero content and untagged sources lead to an empty restricted
   set, not to a fallback without the filter. The same on an exception in route/helper.
4. A code with differing local/global IDs keeps the available organization
   documents; another organization, a disallowed role, a private foreign source,
   an invalid document and an inactive version are not made accessible.
5. A mixed pack in a fixture proves the entry filter; a production data check
   proves that "Stravování" does not have the whole unrelated operations content among its candidates.
6. A meta question, related chips, category top-up, other-grade offers and, if
   enabled, document_siblings/rag_keywords preserve the selected domain.
7. A change of school/domain, a slow catalog, a late response and a removed code
   do not cause a query with the old selection or an automatic widening to "all".
8. A follow-up query after a scope change does not use the previous domain even when
   conversation_id is reused by a direct API client; deduplication of offers has the same boundary.
9. /ask and /ask/stream use the same scope and deterministic QA results;
   for generation, the candidates and scope compliance are judged, not the match of the LLM text.
   /explain shows the corresponding deterministic prefix with the correct identity.
10. Idempotency replay with the same scope works; a different scope with the same
    key returns 409. The order/duplicates of domains do not change the meaning of the request.
11. The public catalog path through nginx works for mobile. Mobile and BFF carry
    the filters, effective_scope and correctable errors according to the contract.
12. A large document with several domains is available in each of its domains, without splitting
    the file. The test judges the admissibility of the source and the selection of the relevant passage by
    the query, not a nonexistent separate topical label on each chunk.

Before deployment: `make docs-check`, contract and targeted behavioural tests,
parity check and the regression set `python3 tests/eval_rag.py --repeat 3`.
Separate the measurement of the code change on the same data from the measurement after splitting packs.
Perform synthetic/fault-injection scenarios on a test copy, not in production.
Do not declare the deployment done merely after a single successful query.

## 11. Scope of approval and remaining uncertainties

This document deploys nothing. In an execution session respect the authorization
already granted; do not repeat an approval the owner already gave for a specific change.
If the proposed log migration is not yet approved, present the concrete SQL
and a migration test according to the AGENTS.md rule for schema changes. The change
of the request hash is to preserve the existing replay; any real change of its semantics
requires an owner decision. Any profiles are a further separate
decision on schema/config. Adding domains does not need to change the embedding model,
normalization, indexes or cascade precedence.

The mobile source code is not in this repository; its actual client path,
storage of selections and release must be verified by the mobile team. During implementation
also measure the SQL plans after adding the filters and verify the behaviour of the QA build when splitting packs.
Any need for a new index, a change to the build or a profile is to be presented as
a concrete extension, not an assumption hidden in an estimate.

The estimate of 1.5 days is not substantiated for the whole scope. Split the binding estimate into
backend, curated data and both clients after stage A and after the import is verified;
the data work and the mobile release are today the main unknowns.
