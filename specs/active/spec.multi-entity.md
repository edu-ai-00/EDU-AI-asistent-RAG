---
id: spec.multi-entity
status: deprecated
components: [db, api, ingestion, retrieval, config]
sources:
  - postgres/init/
  - api/app/main.py
  - langflow_flows/ingestion/
  - search/app/settings.py
  - search/app/languages.py
last_verified: 2026-08-01
---

# One database, several legal entities

> **Implementation status (documentation audit, SHA c8fe0543): the text is a proposal from 2026-08-01, not a description of today's system.**
> `postgres/init_clean/01_schema.sql` contains **no** `PARTITION BY` partitioning, `CREATE POLICY` or row-level security, and no
> `_TEMPLATE` entity. Today entity isolation is provided only by application code (`search/app/scope.py`, `private`/`entity`/`organization` visibility),
> so one cannot rely on the database catching a forgotten `WHERE`. Settings live in the `edu_ai.settings` table with `legal_entity_code`
> (not `search_setting`), `public` visibility was removed (migration 91) and the `scope=all` request parameter does not exist.
> This historical proposal is superseded by the actual production model, which the owner confirmed on 2026-10-12. Binding visibility is described by D-19 in docs/architecture/decisions.md; this page is not a guide to introducing RLS, partitioning or a broader scope.

## Problem

One company operates the platform and operates it for its subsidiaries as well —
a bank with an insurance arm and a pension fund; a school authority with its
schools. Today every table is single-tenant by omission: there is no attribute
saying whose a document is, so a second entity's material would simply be found
by the first entity's queries.

The product owner's requirement, as settled in the design conversation of 2026-08-01:

* one level of hierarchy (parent and subsidiaries), and the operator may itself
  be a subsidiary — so the hierarchy is descriptive, not a permission structure
* **documents and QA must never mix in search**
* one global parameter may lift that and search across all entities
* most settings are the same for everyone; an entity adds rather than replaces
* a code list is per entity, because one entity wants two active languages and
  another wants four

## Naming

`legal_entity_code text` — a short stable code (`KB`, `KB_POJISTOVNA`), not a
UUID, because it travels through the ingest API, the flow parameters, the logs
and the diagnostics, and a human reads it in all four places. The consequence is
that a code cannot be renamed without a data migration, so everything mutable
lives in `display_name`.

## The registry

```sql
CREATE TABLE edu_ai.legal_entity (
    legal_entity_code  text PRIMARY KEY,
    parent_code        text REFERENCES edu_ai.legal_entity,
    display_name       text NOT NULL,
    ico                text,
    domains            text[],          -- maps a web-chat origin to an entity
    is_active          boolean NOT NULL DEFAULT true,
    created_at         timestamptz NOT NULL DEFAULT now(),
    notes              text
);
```

`parent_code` records who belongs under whom, for reporting and administration.
**Search never reads it.** Because it carries no permission, one level is a
convention and needs no constraint — a depth check would defend an invariant that
does not exist.

There is no `default_language` column: the entity's languages live in its own
`code_list` rows, which is where every consumer already looks.

## Four semantics, one per kind of table

The design conversation produced four distinct behaviours. Getting the wrong one
is not a style question — for a code list, union and per-key override both fail
to express "this entity has fewer languages than the platform".

| behaviour | tables |
|---|---|
| **one row for everyone** | `search_setting` |
| **entity only, equality** | documents, QA, `code_list`, `document_type_taxonomy`, logs |
| **global + entity adds, union** | `qa_synonym`, `qa_stopword`, `qa_blocklist`, `chunk_exclusion_rule` |
| **global + entity overrides per key** | `ui_label`, `system_message`, `prompt_registry` |

### One row for everyone

`search_setting` gets no `legal_entity_code` at all and `search_setting_one_active`
stays as it is. The cross-entity visibility switch lives in its `config`, since
that row is now genuinely global.

### Entity only

`WHERE legal_entity_code = %s`. Equality, so a single btree index with the code
first serves every query and the planner reads exactly one range.

Tables: `edu_ai_document_metadata`, `_chunks`, `_elements`, `_tables`,
`_chunk_keywords`, `qa_entries`, `qa_variants`, `qa_embedding`,
`qa_pack_manifest`, `code_list`, `document_type_taxonomy`, `ingestion_log`,
`qa_query_log`, `qa_feedback` — all `NOT NULL REFERENCES legal_entity`.

`code_list` and `document_type_taxonomy` are in this group and not in the
config groups: an entity holds its OWN SET of records, there are no global rows
to fall back to. That is what lets an entity have two languages while another has
four, and a different default document type.

### Global + entity adds

Read is a union: `WHERE legal_entity_code IS NULL OR legal_entity_code = %s`,
no `LIMIT`. An entity adds three stopwords; it does not replace the 564 global
ones. The blocklist is a safety register, so replacing it globally would be a
regression in safety, not a customisation.

"May not change the global rows" is enforced in the database, not in a document —
write policies bound to the entity, with the operator's role holding `BYPASSRLS`
for the global rows.

### Global + entity overrides per key

`ORDER BY legal_entity_code NULLS LAST LIMIT 1` over the key
(`label_key, lang` / `message_key, lang` / `prompt_key`).

This is the only group where the NULL trap applies: a plain unique index does not
consider two NULLs equal, so `UNIQUE (legal_entity_code, label_key, lang) WHERE
is_active` would happily allow **two global rows** for one key and `LIMIT 1`
would return an arbitrary one. Postgres here is 18 (`NULLS NOT DISTINCT` needs 15+), so:

```sql
CREATE UNIQUE INDEX ui_label_active ON edu_ai.ui_label
    (legal_entity_code, label_key, lang) NULLS NOT DISTINCT WHERE is_active;
```

## Physical separation of documents and QA

The requirement is a table named after the entity. Declarative partitioning gives
exactly that without the maintenance cost of hand-rolled tables:

```sql
CREATE TABLE edu_ai.edu_ai_document_chunks (…) PARTITION BY LIST (legal_entity_code);
CREATE TABLE edu_ai.edu_ai_document_chunks_kb_pojistovna
    PARTITION OF edu_ai.edu_ai_document_chunks FOR VALUES IN ('KB_POJISTOVNA');
```

One schema and one migration — migration 61 added eight columns; with hand-rolled
per-entity tables that becomes eight columns times N, and one entity missed is a
silent ingest failure. Cross-entity search, when the global parameter allows it,
is an ordinary query on the parent instead of a hand-assembled `UNION ALL` over N
names. Single-entity search prunes to one partition, which is the performance the
requirement is reaching for.

What it costs:

* the partition key must be in every unique index —
  `edu_ai_document_chunks_pkey (id)` becomes `(legal_entity_code, id)`,
  `uq_chunk_keywords (file_item_uuid, chunk_index)` becomes
  `(legal_entity_code, file_item_uuid, chunk_index)`, `qa_entries_pkey` becomes
  composite and so does the `qa_variants → qa_entries` foreign key
* a new entity means DDL (`CREATE TABLE … PARTITION OF`), so entity provisioning
  must run it. No `DEFAULT` partition: it would silently swallow a typo in an
  entity code instead of failing
* cross-entity vector search becomes N index scans and a merge — the price of the
  feature, worth knowing before it is switched on at scale

## Resolving which entities a query may see

The search API **always** receives an entity; a missing one is a 400. The scope is
that entity's own material plus everything marked `public` (migration 78) — nothing
else, and cross-entity search is a request decision permitted by a global one (see
below).

An earlier version of this section said the cross-entity set should be "the
entities that have the query's language enabled", resolved from each entity's
`code_list('language')`. **That was wrong and is withdrawn.** It used
CONFIGURATION as a proxy for DATA. Retrieval already filters on the language
recorded on the chunk itself —

```sql
AND coalesce(t.langdetect_result, m.document_language_user,
             m.document_language_llm) = %s
```

— in `document_lexical` and `rag_vector` alike. So a public Czech document owned by
one entity is found by a Czech query from another without anyone consulting the
owner's language list, and it would have been absurd to exclude it because the
OWNER happens not to have that language switched on in its UI.

What an entity's `code_list('language')` actually governs is narrow and stays that
way: which languages a form offers, and which ones an AMBIGUOUS query is reduced
into when detection cannot tell (`prepare()`, `rag_keywords`). It never decides
what is in the index.

The one genuine limit that follows: a document in a language no searching entity
has active is lexically unreachable, because the query is never reduced into that
language. That is the correct behaviour — an entity that does not offer Ukrainian
should not be answering in it — and the vector path still reaches such a document.

**Cross-entity is a request decision, permitted by a global one.** The global
parameter in `settings.config` is the PERMISSION; the request states
`scope = entity | all` and asking for `all` while the permission is off is a 400. A
deployment-wide flag that silently widens every query is the same class of mistake
as defaulting a missing entity to "everything".

Because a cross-entity answer legitimately mixes documents from different entities,
the entity has to be visible in the answer: `answer_parts[]` carries
`legal_entity_code` and the source title says whose document it is.

## Enforcement

Two layers, because one is demonstrably not enough. `private_public_flag` has
existed on `edu_ai_document_metadata` since the beginning and **retrieval ignores
it** — a predicate that has to be repeated in four SQL statements
(`document_lexical`, `rag_keywords`, `rag_vector`, `document_siblings`) is a
predicate that gets forgotten.

1. **One place in code.** `config.CHUNKS_TABLE` as a bare string goes away; a
   helper returns the table together with the mandatory entity predicate.
2. **Row-level security**, on the resolved entity:

```sql
CREATE POLICY tenant_read ON edu_ai.document_chunks FOR SELECT
  USING (visibility = 'public'
         OR legal_entity_code = ANY (
              string_to_array(current_setting('edu_ai.visible_entities', true), ',')));
```

A list rather than an equality, because cross-entity search exists — the
application sets the GUC to the ALREADY RESOLVED set, one element in the ordinary
case. One policy covers both scopes; a policy that called the resolver itself
would run per row.

Partition pruning still applies to `= ANY` with a parameter (runtime pruning), but
this is worth confirming with `EXPLAIN` on real volumes before it is relied on —
an unpruned scan across N entities is the difference between the feature being
usable and not.

RLS is what survives a forgotten `WHERE`, and the same mechanism finally answers
`private_public_flag`.

## Provisioning a new entity

Because there are no global code-list rows, a new entity without its own set has
no languages and neither ingestion nor search will run. A reserved
`_TEMPLATE` entity holds the platform's default sets and is never searched:

```sql
INSERT INTO edu_ai.code_list (legal_entity_code, …)
SELECT %s, … FROM edu_ai.code_list WHERE legal_entity_code = '_TEMPLATE';
```

This keeps "each entity has its own set" while making entity creation one
statement, and it gives a platform-wide addition (a new supported language) a
defined place to start from. Without it the code list gets copied by hand from
whichever entity was created last.

## The hard part: nineteen components read config themselves

`code_list` is the language registry and it is read on both sides:

* search — `languages.py`, `prepare.py`, `messages.py`, `server.py`,
  `rag_keywords.py`, `api/app/main.py`
* flows — `LanguageSegmenter-MD`, `ChunkLemmatizer-1-MD`, `ChunkLemmatizer-2-MD`,
  `BatchRunComponent-r6dJ7-MD`, `ParseDocumentMetadataJSON-1UQ97-MD`,
  `QAJsonReader-qa001-QA`

and another thirteen flow components read `prompt_registry`, `search_setting`,
`chunk_exclusion_rule` or `document_type_taxonomy` — nineteen in total, each with
its own `psycopg2` connection and its own `WHERE is_active`.

Every one of them must learn the entity. A component that does not gets it wrong
**silently**: chunks indexed in a language the entity does not have, or reduced
with the wrong dictionary, discovered later as bad search results.

So the entity must not be threaded through nineteen copies of the same lookup.
One component per flow resolves the config and the code list at the start of the
run and passes them on in the DataFrame; the rest stop touching the database.
Larger change, but it is the only version where nothing can be forgotten.

## Ingestion

`/v1/ingest` takes `legal_entity` as a **required** form field, validated against
the registry the way `private_public_flag` is validated today. The gateway passes
it in the tweaks to the input node; all six flows carry it on the row and every
insert writes it — `EDU_AI_LOAD_MARKDOWN`, `_PDF`, `_WEB_PAGE`, `_OFFICE`, `_QA`,
and `EDU_AI_EXPORT_QA_PACK` scopes its export.

Two existing behaviours become entity-scoped and will be wrong until they are:

* **duplicate detection.** There is no unique index on `source_url`; dedup runs in
  the application over `is_active`. Unscoped, the second entity ingesting the same
  URL deactivates the first entity's document.
* **`qa_pack_manifest.pack_id`** is globally unique while active
  (`qa_pack_manifest_one_active_per_pack`) and the value comes from the user
  (`user_defined_pack_id`). Two entities cannot both have an active pack named
  `faq`. The index becomes `(legal_entity_code, pack_id)`.

## Order of work

1. registry `legal_entity` + `_TEMPLATE`
2. cross-entity visibility parameter into `search_setting.config`
3. nullable `legal_entity_code` everywhere, backfill to one seed entity
4. `NOT NULL` + FK + reindex the unique constraints listed above
5. convert documents/QA to partitioned tables (a data move — trivial at today's
   465 chunks, not later)
6. `code_list` / `document_type_taxonomy` resolver switched to equality
7. union tables + RLS write policies
8. one config-loading component per flow; components stop reading the database
9. `/v1/ingest` requires the entity; flows write it
10. RLS last — only once the application actually sets the GUC, or it locks the
    operator out of their own data

Steps 1–3 are safe while running. Step 4 requires that nothing writes without an
entity any more.

## Open

**Is partitioning an acceptable reading of "a table named after the entity"?**
Everything else is settled; step 5 depends on this answer.

**Genuinely shared corpora.** The Cermat and MŠMT documents in the current corpus
belong to no entity — public information relevant to all of them. Strict equality
means ingesting them per entity (N copies of ~250 chunks and N times the
embedding cost) or an explicit `visible_to_all` flag that only the operator may
set, which is an exception to "must not mix" and has to be locked to a role.
Undecided; it determines the shape of the predicate in all four gatherers.
