---
id: docs.data-model-and-setup
status: current
components: [data-model, setup, ingest, langflow]
sources:
  - postgres/init_clean/01_schema.sql
  - postgres/init_clean/02_seed_config.sql
  - postgres/init/213_log_domains.sql
  - scripts/align_existing_db.sh
  - scripts/domain_inventory.py
  - scripts/domain_migration.py
  - search/app/scope.py
  - search/app/domain_scope.py
  - api/app/main.py
  - docker-compose.yml
  - .env.example
last_verified: 2026-09-06
---

# Data model and setup — what every table is for, and the order to fill it

This is the companion to [SETTINGS_REFERENCE.md](SETTINGS_REFERENCE.md). That
one explains *how retrieval behaves*; this one explains *what the data means*,
**in what order a new school has to be set up**, and how the three separate
configuration mechanisms (environment variables, Langflow global variables and
the database) relate to each other.

Bringing the stack up — cloning, `.env`, TLS, importing the flows, minting keys
— is [ONBOARDING.md](../ONBOARDING.md). This document starts where that one
ends: the containers run, and the database now has to be told what a school is.

---

## 1. The four layers

Every table belongs to exactly one of four layers, and the layers have to be
filled in this order, because each depends on the one above it.

| Layer | Question it answers | Filled by |
|---|---|---|
| **Identity** | Whose data is this? | during setup, once |
| **Registries** (číselníky) | What vocabulary does this school use? | during setup, then maintained |
| **Content** | What does the school actually say? | ingestion, continuously |
| **Operations** | What was asked, and what was answered? | the running system |

Nothing in the content layer can be created before the registries exist —
ingestion resolves grades, subjects, document types and domains against them and
refuses codes it does not know.

---

## 2. Identity layer

### `legal_entity` — the school or company

The legal person whose documents, QA pairs and users these are. Every data table
references it. The table is **hierarchical**: `parent_code` lets one legal person
be split into units (a primary school and a secondary school under one
organisation), which is what makes "grade 2" resolvable — the same number means
different years in different units.

| Column | Business meaning |
|---|---|
| `legal_entity_code` | Stable technical code. Appears in every API call; changing it later is a migration across the whole database. |
| `parent_code` | The organisation this unit belongs to. `NULL` = a top-level legal person. |
| `display_name` | What a person sees. |
| `ico`, `red_izo`, `units` | Registry identifiers; documentation, not logic. |
| `domains` | The school's web domains. Used to decide whether an ingested URL belongs to this school. |
| `kind`, `level`, `grade_levels` | Whether the unit is primary or secondary and which grades it spans. Drives grade disambiguation at search time. |

**Cross-tenant isolation is not configurable.** Two legal persons never see each
other's content, whatever the visibility settings say.

### `staff_user` — a named person who ingests or administers

The name is **required** and lands on the documents that person ingests, so a
document always has an accountable owner.

### `app_user` — one mobile installation

Known by a technical id only. There is deliberately no name: the questions asked
are a behavioural record, and attaching a name would turn it into a personal one.

---

## 3. Registry layer (číselníky)

These are the school's vocabulary. They are **per legal entity**, and they
resolve by *nearest owner takes the whole set* — a unit either has its own list
or inherits the parent's entire list. There is no merging.

### `code_list` — languages, subjects, grades

The single registry. Adding a language **starts here**: insert with
`is_active = false`, fill the other layers (labels, prompts, messages, engines),
activate last. `is_ui_default` is presentation only; `is_processing_fallback` is
what the pipeline uses when language detection cannot tell.

Grades are numeric. `9999` means "all grades" — a document that applies to
everyone, not a document whose grade is unknown. The two are different states
and must not be conflated.

### `taxonomy_rules` — document types and domains

A číselník of document types (rules, information page, study material…) **plus
the instruction given to the classification LLM**. This is the central control of
classification: adding a new document type is one row, not a code change.

`sort_order` is the **authority ladder** — binding rules (10) outrank a news page
(40) when both match a question. See SETTINGS_REFERENCE §3.10.

### `taxonomy_binding` — domain × school level → grade set

The semantic bridge: "secondary admissions, seen from a primary school, concerns
grade 9". Read **live** at ingest to identify `document_metadata.binding_id` and
to whitelist per-chunk grades. The grade set is never materialised, so editing a
row takes effect **without re-ingesting anything**.

### `grade_term`, `subject_term` — how people actually speak

Colloquial and standard words for grades and subjects, per language ("devítka",
"páťáci", "matika"). Without these the search sees a word it cannot map to a code.

### `qa_synonym` — abbreviations and local names

**This is the step most often forgotten, and it silently breaks retrieval.**
A school's documents are full of abbreviations (ŠVP, IVP, ŠD, ŽK) and local
names for places and events. If they are not grouped here, the passage
containing them is *lexically unreachable* — no amount of tuning helps, because
the query and the document never share a token.

Setting up a school therefore includes a mandatory pass through its documents
for abbreviations. Two traps: anything containing the word "škola" tends to
over-match, and ŠVP (school education programme) collides with SVP (special
educational needs).

### `qa_stopword` — words that carry no meaning

Its own registry, deliberately not in code and not in the settings, because
which words are noise differs by language and by school.

### `qa_blocklist` — topics that must not be answered from documents

Each row names a category. What *happens* on a hit is configuration
(`pipeline[route]`), and the wording is a `system_message` row — for
self-harm the answer is a crisis line, never a refusal.

### `system_message`, `ui_label` — everything a person reads

Wording per language. A language is offered in a page's switcher only when it
has **every** key of that scope, which is the mechanism that prevents a
half-translated interface.

### `prompt_registry` — every LLM prompt, versioned

All prompts in the whole solution, fetched per request by a technical key
(UI > DB > code, no cache), so changing one takes effect without a restart or a
redeploy.

### `embedding_registry` — which vector model wrote the vectors

One row per generation. `document_chunks.embedding` is the **only** vector store
in the system.

### `settings` — retrieval behaviour

Documented in full in [SETTINGS_REFERENCE.md](SETTINGS_REFERENCE.md). Note that
it is **per organisation**: a new flag must be written to every active row.

### `chunk_exclusion_rule`, `answer_exclusion_rule`, `validity_period`

Boilerplate that must not become a chunk (headers, footers, signature blocks);
meta-sentences that must not become an answer ("this document does not address
…"); and the period for which a document is valid.

---

## 4. Content layer

Written by ingestion; not edited by hand.

| Table | What it holds |
|---|---|
| `document_metadata` | One row per ingested document: type, owner, visibility, declared grades, validity, binding. |
| `document_elements` | The document's structure — sections, lists, tables — before chunking. |
| `document_chunks` | The retrievable unit. Carries the normalized text, lemmas and stems, the heading path, grades, subjects, domains, keywords and the embedding. |
| `document_tables` | Tables extracted as data, so a table row can answer a question. |
| `qa_entries`, `qa_variants`, `qa_embedding` | Curated question–answer pairs, their phrasing variants and vectors. Answered before documents are searched. |
| `qa_pack_manifest` | Provenance of an uploaded QA pack: who uploaded it, for which entity, with which scope. |
| `ingestion_log` | What happened during each ingest, step by step. |

Two things worth knowing:

* **`document_chunks.bm25_text` is a generated column.** It is the concatenation
  of the normalized text, lemmas, stems and the heading lemmas. You cannot insert
  into it, and you do not need to reindex after changing it — it is derived.
* **A chunk records which `settings` row produced it** (`setting_id`). That is
  what makes "this answer came from chunks built under configuration v2"
  answerable a year later.

---

## 5. Operations layer

| Table | What it holds |
|---|---|
| `question_answer_log` | Append-only record of every query: the question, the route taken, the candidates, the answer, and (since migration 213) the thematic AREAS it was restricted to. Written by the search service. |
| `qa_feedback` | A rating attached to a logged answer. |
| `qa_conversation_v` | A view assembling a session's turns into a conversation. |
| `idempotency_key` | Replay cache for `POST /v1/ask`, so a retried request does not produce a second answer. 24-hour retention. |

The query log is append-only. Idempotency state may be updated during a query, and the separate internal QA administration router writes curated data; this statement does not imply the entire service is read-only.

### `question_answer_log.domains` (migration 213)

`text[] NULL`. Which thematic areas the turn searched in, as **canonical DOMAIN
codes** — not `taxonomy_rules` ids. An id differs per school for one and the
same code (in production `SECONDARY_ADMISSIONS` is one id in the ZŠ catalog and
another globally), so an id column could not be compared across turns and a
re-tagged catalog would silently change what a historical row meant.

`NULL`, never `'{}'`, and no default: NULL **is** the meaning of "no thematic
restriction", and it is what every pre-existing row and every client without the
field writes. A `DEFAULT '{}'` would make "unrestricted" indistinguishable from
"explicitly empty" — which the API rejects with a 422 and can therefore never
reach the table.

**No backfill**, deliberately: the area a 2026-08 question was asked in is not
recoverable from its text, and guessing it would poison the comparison the
column exists for. It is also a column the server **reads back** — see
[ai/caching.md](ai/caching.md) — so a guessed value would change which earlier
turns a follow-up sees.

The migration is transactional, repeatable, and **checks the column's type**:
`ADD COLUMN IF NOT EXISTS` alone proves only that *something* called `domains`
is there, and a hand-patched database with a `jsonb` one would fail every INSERT
at runtime while the migration reported success.

---

## 5a. Thematic area labels — where they live and how to change them

The area filter reads the **scanned** row, never a declaration through a join,
because the vector scans order by distance and take a `LIMIT` before any join.
So every label exists twice, and the two must agree:

| Layer | Declared on | Scanned on |
|---|---|---|
| curated QA | `qa_pack_manifest.domain_ids`, `qa_entries.domain_ids` | `qa_variants.domain_ids`, `qa_embedding.domain_ids` (migrations 140, 144) |
| documents | `document_metadata.document_domain_user` / `_llm` | `document_chunks.document_domain_id` |

**The effective value of a document is user-declared if non-empty, else the
classifier's** — the precedence the ingest applies and propagates to the chunks.
Editing `document_metadata` alone therefore changes nothing that retrieval sees:
the chunks keep the old labels, and the document becomes reachable in the wrong
areas or in none. Measured on production 2026-09-06: **170 chunks carried no
label under a labelled document, and 94 carried a different set** — the exact
signature of a metadata-only edit.

Two tools, and neither of them decides anything:

- `scripts/domain_inventory.py` — **read-only**, safe against production
  (`--remote edu-ai`). Per school: the catalog, active packs with untagged and
  *differing* entry counts, documents and chunks, and a drift section that
  separates the harmless case (same set, different array order — irrelevant to
  array overlap) from the two that block switching the picker on: a **missing**
  label, and a **different set**. It prints codes, counts and identifiers only,
  never a question or a document's text, so its output can go into a deployment
  note.
- `scripts/domain_migration.py` — applies a **curator's written decisions**.
  Dry run by default; `--apply --go` writes one transaction per set; `--verify`
  reads back, including the derived copies; `--rollback <snapshot> --go`
  restores the recorded values row by row. The plan carries **codes, not ids**,
  and keys QA entries by **`entry_key`**, because the QA writer resolves
  identity through `entry_key` and the same CSV rebuilt into another pack does
  not keep its UUID.

Four properties of it are worth stating exactly, because each is a way the tool
could have destroyed data and earlier versions of it did not have them:

  **One selector defines the set.** `snapshot_sql(item)` enumerates the rows a
  set will write — by pack, file or entity — and it is used three times: to take
  the snapshot, as the guard's "actual" side inside the transaction, and (via
  its keys) to restrict every UPDATE. It therefore includes the **scan
  surfaces**, `qa_variants` and `qa_embedding`, which were previously updated by
  JOIN while being absent from the snapshot altogether: a variant added to an
  existing answer was written blind and had no rollback entry.

  **The precondition is re-asserted INSIDE the write transaction**, after a row
  lock (`FOR UPDATE`). A check in a separate statement is advisory: a curator
  saving in the gap between it and the UPDATE would have their decision
  overwritten silently. It compares the WHOLE current target set against the
  snapshot, so it catches a value that changed, a row that disappeared **and a
  row that appeared** — the last of which was structurally undetectable while
  the comparison restricted its own actual side to the keys it already had.

  **Every write is keyed to the snapshot.** The predicates by pack and file are
  kept for clarity, but each statement also carries
  `AND <key> IN (…the snapshot's keys…)`. Selecting by `pack_id` alone touched
  whatever was in the pack at write time, which is not the set the operator
  reviewed. This is also what covers the case no guard can see — a row inserted
  by a transaction that has not committed, and is therefore invisible to any
  SELECT: the guard refuses what it can see, and the key restriction makes the
  rest unreachable.

  **The snapshot reaches disk BEFORE the first row is touched** (atomically, via
  a temp file and `os.replace`), and is rewritten after each set. Written after
  the loop, a process killed half way — a dropped SSH connection is enough —
  left a changed database with no record of the prior values, at precisely the
  moment the record is needed.

  **A rollback restores each recorded row by its own key**, verifies inside its
  own transaction under the same lock, and REFUSES (exit 2) when a row has
  changed since the apply, naming which. It used to resync the scan surfaces
  with an unrestricted `UPDATE … FROM qa_entries` — ~3 000 variants and ~3 000
  embeddings on production, to undo two packs — and to run its drift check
  *before* the transaction, so a curator committing in between was overwritten
  after the tool had printed "no drift". `--force` skips the verification;
  never the lock.

The guarantee is **per snapshot**, and worth saying plainly: a row absent from
snapshot S is neither written nor restored by S. A row that is committed and
visible when a snapshot is taken is legitimately part of the set that snapshot
describes — writing it is correct precisely *because* it is recorded and can
therefore be undone.

`--rollback` is not `qa_deactivate_packs.py --activate`: that one re-activates
*every* variant of a pack, which is a different thing from putting a curated set
back, and the difference has already cost one.

`tests/test_domain_migration.py` drives the script through `subprocess` as an
operator would and asserts on the database afterwards: a concurrent session
committing during the apply transaction and during the **restore** transaction,
a row (and a variant) inserted between the snapshot and the write, a process
killed mid-apply, and a rollback that must leave a foreign set — and a variant
deliberately out of step with its entry — untouched. The concurrency groups win
their races deterministically by holding an **uncommitted** row lock from a
second psql session and handshaking on `pg_locks`, not by sleeping.

What the migration tool does **not** do: re-chunk, re-embed, re-classify, or
recompute `taxonomy_binding` / `document_binding_id` and the audience and grade
values derived from a binding. If new areas change a document's semantic
binding, that is a targeted **reingest**, not a label edit — and `--verify`
reports an unchanged binding so the case cannot pass unnoticed.

**Untagged content is not a defect a tool can fix.** Which area a source belongs
to is a curatorial decision, and an untagged source is invisible under *any*
explicit filter — it is not "in every area". A school may knowingly keep a
document reachable only under "all areas"; that is a decision to record in the
report, not a default to rely on.

---

## 6. Setting up a new school — the order

Each step depends on the ones above it; a foreign key or the ingest validation
will stop you if you skip one.

**1. Create the legal person.** `legal_entity` — the organisation first, then its
units with `parent_code` pointing at it. Set `kind`, `level` and `grade_levels`
correctly now: grade disambiguation depends on them, and changing them later
means re-ingesting.

**2. Declare its web domains.** `legal_entity.domains`. Ingesting a URL that
belongs to no known domain is how content ends up under the wrong school.

**3. Create the people.** `staff_user` — at least one, because a document
requires an owner.

**4. Fill the číselníky.** `code_list` for languages, subjects and grades.
Nothing can be ingested before this: ingestion resolves codes and rejects
unknown ones.

**5. Set the document types and domains.** `taxonomy_rules`, including the
`sort_order` authority ladder — decide now which document type wins when two of
them answer the same question.

**6. Bind domains to grades.** `taxonomy_binding` — the domain × level → grade
set rules. These can be edited later without re-ingesting.

**7. Add the vocabulary.** `grade_term` and `subject_term` for how people in this
school actually speak.

**8. Go through the documents for abbreviations and set up `qa_synonym`.**
Do not skip this. Without it, passages containing the school's own abbreviations
cannot be found at all.

**9. Create the settings row** for the organisation, or leave it inheriting the
platform-wide one. See SETTINGS_REFERENCE §1.

**10. Ingest the documents.** Declare grades on the form
(`school_subject_grades`) — a whole-school document is declared on the
organisation, and the grade then cascades to the chunks. A document that
declares nothing gets nothing; the cascade cannot infer what was never stated.

**11. Upload the QA packs**, if the school has curated pairs.

**12. Verify.** Run a handful of real questions and check the route each took —
a QA hit, a document hit, or silence. Silence on a question the corpus genuinely
does not answer is **correct behaviour**, not a defect.

---

## 6a. Worked example A — an organisation that is one school

The simple case: one legal person, no units. A primary school with grades 1–9.

**Step 1 — the legal person.** `parent_code` is `NULL`, `kind` is
`legal_person`, and because there is only one school inside it, `level` is set
directly on the row.

```sql
INSERT INTO edu_ai.legal_entity
  (legal_entity_code, parent_code, display_name, ico, domains,
   kind, level, grade_levels, notes)
VALUES
  ('ZAKLADNI_SKOLA_BOROVA', NULL, 'Základní škola Borová', '1234',
   '{skola-borova.example,www.skola-borova.example}',
   'legal_person', 'ZS',
   '{"ZS": ["1","2","3","4","5","6","7","8","9"]}'::jsonb,
   'Příspěvková organizace. Školní 1234, 123 45 Borová.');
```

`grade_levels` says which years exist under which level. With a single level
there is no ambiguity: "2. ročník" can only mean one thing.

**Step 3 — a person who ingests.** At least one, because a document needs an
owner.

```sql
INSERT INTO edu_ai.staff_user (user_id, legal_entity_code, display_name, email)
VALUES (gen_random_uuid()::text, 'ZAKLADNI_SKOLA_BOROVA',
        'Jana Nováková (ředitelka)', 'reditelka@skola-borova.example');
```

**Step 4 — the číselníky.** Language and grades are owned by this entity;
subjects usually are too.

```sql
-- language: one active row is enough for a single-language school
INSERT INTO edu_ai.code_list
  (list_code, code, sort_order, legal_entity_code, labels,
   is_ui_default, is_processing_fallback)
VALUES ('language', 'cs', 1, 'ZAKLADNI_SKOLA_BOROVA',
        '{"cs":"čeština","en":"Czech"}'::jsonb, true, true);

-- grades 1..9 plus 9999 = "applies to all grades"
INSERT INTO edu_ai.code_list
  (list_code, code, sort_order, legal_entity_code, labels)
SELECT 'school_grade', g::text, g, 'ZAKLADNI_SKOLA_BOROVA',
       jsonb_build_object('cs', g || '. ročník', 'en', 'Grade ' || g)
FROM generate_series(1, 9) AS g;

INSERT INTO edu_ai.code_list
  (list_code, code, sort_order, legal_entity_code, labels)
VALUES ('school_grade', '9999', 9999, 'ZAKLADNI_SKOLA_BOROVA',
        '{"cs":"všechny ročníky","en":"all grades"}'::jsonb);
```

That is ten rows: nine years and `9999`. `9999` means **"applies to
everyone"** — a school-wide document. It is *not* "grade unknown"; those are
two different states and conflating them is how a rule for ninth-graders ends up
answering a second-grader.

**Steps 8 and 9.** Go through the school's documents for abbreviations and put
each group into `qa_synonym`, then either create a settings row for this entity
or let it inherit the platform-wide one. A single school with nothing unusual
about it can safely inherit.

After that, ingest. Total: **one entity row, one person, ten grade rows, one
language row**, plus the abbreviations found in the documents.

---

## 6b. Worked example B — an organisation with several schools inside it

The harder case, and the one the whole hierarchy exists for: **one legal person
operating both a primary and a secondary school**. The fictitious
`SKOLA_NOVA_VES` example below illustrates this shape. The current
[clean-install organization seed](../postgres/seed/clean_organizations.json)
keeps this demo organization and its units inactive; its active `ZS_KAVCI_HORY`
organization uses the same parent/unit structure.

The problem it solves: inside such an organisation, **"2. ročník" means two
different years** — the second year of the primary school, and the second year
of the secondary school. Without units, that question is unanswerable.

**Step 1a — the organisation.** `level` is `NULL`, because the organisation
itself is not a school of one level. `grade_levels` carries **both** ladders, and
`units` records the registry identifiers of the individual units.

```sql
INSERT INTO edu_ai.legal_entity
  (legal_entity_code, parent_code, display_name, red_izo, domains,
   kind, level, grade_levels, units, notes)
VALUES
  ('SKOLA_NOVA_VES', NULL, 'Škola Nová Ves – celá organizace', '1234',
   '{skola-nova-ves.example,www.skola-nova-ves.example}',
   'legal_person', NULL,
   '{"ZS": ["1","2","3","4","5","6","7","8","9"],
     "SS": ["10","11","12","13"]}'::jsonb,
   '{"ZS":"1234","SOS":"1234","MS":"1234","SJ":"1234","SD":"1234"}'::jsonb,
   'Jedna právnická osoba s několika zapsanými jednotkami.');
```

**Step 1b — the units.** `kind = 'unit'`, `parent_code` points at the
organisation, and **`level` is what distinguishes them**. `grade_levels` stays
`NULL` on a unit — the ladder is declared once, on the organisation.

```sql
INSERT INTO edu_ai.legal_entity
  (legal_entity_code, parent_code, display_name, kind, level, notes)
VALUES
  ('SKOLA_NOVA_VES_ZS',  'SKOLA_NOVA_VES', 'Škola Nová Ves — základní škola',
   'unit', 'ZS',  'IZO 1234'),
  ('SKOLA_NOVA_VES_SOS', 'SKOLA_NOVA_VES', 'Škola Nová Ves — střední odborná škola',
   'unit', 'SS',  'IZO 1234');
```

**Step 4 — číselníky, and this is where the two cases really differ.**

Code lists resolve by **nearest owner takes the whole set**. A unit either has
its own list or inherits the parent's entire list; the two are never merged. So
the grades are declared **on the units**, and the organisation holds only the
one code that is true across all of them:

```sql
-- primary unit: years 1..9
INSERT INTO edu_ai.code_list
  (list_code, code, sort_order, legal_entity_code, labels)
SELECT 'school_grade', g::text, g, 'SKOLA_NOVA_VES_ZS',
       jsonb_build_object('cs', g || '. ročník', 'en', 'Grade ' || g)
FROM generate_series(1, 9) AS g;

-- secondary unit: years 10..13, LABELLED as the first..fourth year of SŠ
INSERT INTO edu_ai.code_list
  (list_code, code, sort_order, legal_entity_code, labels)
VALUES
  ('school_grade', '10', 10, 'SKOLA_NOVA_VES_SOS',
   '{"cs":"1. ročník (SŠ)","en":"Grade 1"}'::jsonb),
  ('school_grade', '11', 11, 'SKOLA_NOVA_VES_SOS',
   '{"cs":"2. ročník (SŠ)","en":"Grade 2"}'::jsonb),
  ('school_grade', '12', 12, 'SKOLA_NOVA_VES_SOS',
   '{"cs":"3. ročník (SŠ)","en":"Grade 3"}'::jsonb),
  ('school_grade', '13', 13, 'SKOLA_NOVA_VES_SOS',
   '{"cs":"4. ročník (SŠ)","en":"Grade 4"}'::jsonb);

-- the organisation holds ONLY "all grades"
INSERT INTO edu_ai.code_list
  (list_code, code, sort_order, legal_entity_code, labels)
VALUES ('school_grade', '9999', 9999, 'SKOLA_NOVA_VES',
        '{"cs":"všechny ročníky","en":"all grades"}'::jsonb);
```

**Read the codes and the labels separately.** The code is a position on one
continuous ladder — 1–9 primary, 10–13 secondary — so that ordering, filtering
and the grade tier work without knowing which unit a chunk came from. The label
is what a person sees, and there the secondary years are numbered from one
again. A secondary school's "2. ročník" is therefore **code 11**, and that is
what makes it distinguishable from the primary school's second year.

Language and subject lists, on the other hand, are usually the same across the
whole organisation, so they are declared once on the parent and the units
inherit them:

```sql
INSERT INTO edu_ai.code_list
  (list_code, code, sort_order, legal_entity_code, labels,
   is_ui_default, is_processing_fallback)
VALUES ('language', 'cs', 1, 'SKOLA_NOVA_VES',
        '{"cs":"čeština","en":"Czech"}'::jsonb, true, true);
```

**Step 6 — bind domains to grades.** With two levels in one organisation, the
bindings finally earn their keep: the same topic concerns different years
depending on which unit is asking.

```sql
INSERT INTO edu_ai.taxonomy_binding (domain_code, school_level, grades, notes)
VALUES
  ('SECONDARY_ADMISSIONS',          'ZS', '{9}',   'Přijímačky na SŠ se týkají deváťáků.'),
  ('MULTIYEAR_GYMNASIUM_ADMISSIONS','ZS', '{5,7}', 'Víceletá gymnázia: pátý a sedmý ročník.'),
  ('GRADUATION_EXAM',               'SS', '{13}',  'Maturita se týká posledního ročníku SŠ.'),
  ('UNIVERSITY_ADMISSIONS',         'SS', '{13}',  'Přihlášky na VŠ tamtéž.');
```

These are read live at ingest, so editing a row later takes effect **without
re-ingesting anything**.

**Step 10 — who owns which document.** This is the decision that bites later.
A school-wide document (a code of conduct covering both units) is ingested
**on the organisation** with `school_subject_grades = 9999`; a document that
concerns only the secondary school is ingested **on that unit**.

Getting it wrong is recoverable but not free: moving a document to a different
owner means changing the owning entity, and the foreign key on the document's
owner has to move with it.

### Visibility inside such an organisation

Sharing uses explicit visibility modes. `settings.config.visibility` contains one publishing switch and one unused legacy field, documented in SETTINGS_REFERENCE §2:

* `child_may_publish_organization` (enforced at ingest) lets a unit mark a
  document `organization`, which then reaches the parent **and the sibling
  unit** — the secondary school sees what the primary school published
  organisation-wide.
* `entity_visible_to_parent` is a stored legacy field and is not read by search.
  Current production and the user's confirmed model (2026-10-12) permit `entity`
  only for the owning entity. A parent, child or sibling must use content explicitly
  marked `organization`; see architecture decision D-19.

Neither ever crosses to another legal person. Between `SKOLA_NOVA_VES` and
`ZAKLADNI_SKOLA_BOROVA` nothing is shared under any configuration.

### The two shapes side by side

| | One school | Organisation with units |
|---|---|---|
| `legal_entity` rows | 1 | 1 organisation + 1 per unit |
| `level` | on the entity | `NULL` on the organisation, set on each unit |
| `grade_levels` | on the entity | on the organisation only |
| `school_grade` codes | on the entity (1–9 + 9999) | on the **units**; only `9999` on the organisation |
| `language`, `school_subject` | on the entity | on the organisation, inherited by units |
| `taxonomy_binding` | usually one level | one row per (domain, level) |
| Who owns a document | always the entity | the organisation if school-wide, otherwise the unit |

---

## 7. Langflow: flows, IDs and nodes

### Flows are addressed by alias, not by UUID

Every flow JSON in `langflow_flows/ingestion/` carries a stable
**`endpoint_name`**. Langflow assigns its own UUID on import, and that UUID
differs on every instance — so nothing outside Langflow may depend on it.
Callers address flows by the alias:

| Flow file | `endpoint_name` | Environment variable |
|---|---|---|
| `EDU_AI_LOAD_PDF.json` | `edu-ai-load-pdf` | `LANGFLOW_FLOW_ID_PDF` |
| `EDU_AI_LOAD_OFFICE.json` | `edu-ai-load-office` | `LANGFLOW_FLOW_ID_OFFICE` |
| `EDU_AI_LOAD_WEB_PAGE.json` | `edu-ai-load-web` | `LANGFLOW_FLOW_ID_WEB` |
| `EDU_AI_QA.json` | `edu-ai-load-qa` | `LANGFLOW_FLOW_ID_QA` |
| `EDU_AI_LOAD_MARKDOWN.json` | `edu-ai-load-markdown` | `LANGFLOW_FLOW_ID` |
| `EDU_AI_EXPORT_QA_PACK.json` | `edu-ai-export-qa-pack` | — |

These aliases are **stable constants set in `docker-compose.yml`**, not
per-instance secrets, which is why they are deliberately *not* in `.env`.
`scripts/import_flows.py` is idempotent: a flow whose `endpoint_name` already
exists is PATCHed in place rather than duplicated.

Only the **markdown** flow writes to the database. PDF, Office and Web run
first and only convert their input; they then hand the result to the markdown
flow through a loopback call.

### Node IDs must be unique per flow

Each node carries an id like `APIPipelineInput-tfiG6-MD`. The suffix — `MD`,
`PDF`, `WEB`, `OFF`, `QA` — is not cosmetic.

Langflow caches component builds by bare node id. When several flows shared the
same id (`APIPipelineInput-tfiG6` in all of them), one flow's cached build was
served to another, and child PDFs were silently never ingested. Making the ids
unique per flow fixed it. **If you copy a node between flows, re-suffix its id.**

The gateway must know which node to feed:

| Variable | Value in the reference install | What it names |
|---|---|---|
| `LANGFLOW_PIPELINE_INPUT_NODE_MARKDOWN` | `APIPipelineInput-tfiG6-MD` | the node the payload is injected into |
| `LANGFLOW_PIPELINE_INPUT_NODE_PDF` | `APIPipelineInput-tfiG6-PDF` | same, PDF flow |
| `LANGFLOW_PIPELINE_INPUT_NODE_WEB` | `APIPipelineInput-tfiG6-WEB` | same, web flow |
| `LANGFLOW_PIPELINE_INPUT_NODE_OFFICE` | `APIPipelineInput-tfiG6-OFF` | same, Office flow |
| `LANGFLOW_PIPELINE_INPUT_NODE_QA` | `APIPipelineInput-tfiG6-QA` | same, QA flow |
| `LANGFLOW_MARKDOWN_LOGGER_NODES` | `IngestionLogger-AQC5h-MD` | the logger node whose progress is reported back |

**Where to read a node id:** open the flow JSON and look at `data.nodes[].id`,
or in the Langflow UI use the node's context menu. After editing a flow in the
UI, export it back to `langflow_flows/` — component code lives **only** inside
the flow JSON (`data.node.template.code.value`), nowhere else in the repository.

**After PATCHing a flow, restart Langflow.** Otherwise a stale build stays
cached and the next run fails with an unrelated-looking `KeyError: None`.

---

## 8. Three configuration mechanisms, and which to use

This is the part that costs the most time when it goes wrong, because the
failure never looks like a configuration problem.

| Mechanism | Lives in | Use it for |
|---|---|---|
| **Environment variable** | `.env` → container env | Connectivity and infrastructure: hosts, ports, database name and user, URLs. |
| **Langflow global variable** | Langflow's own database, encrypted | Secrets that flow components consume: `OPENAI_API_KEY`, `POSTGRES_PASSWORD`. |
| **Database (`settings` and the registries)** | Postgres | Everything about behaviour and content. |

### Rule 1 — only secrets may cross into Langflow

`LANGFLOW_VARIABLES_TO_GET_FROM_ENVIRONMENT` is set to exactly
`OPENAI_API_KEY,POSTGRES_PASSWORD` and must stay that way.

The env → global-variable bridge creates every variable it touches as type
**Credential**, and a Credential-typed variable is **rejected by non-secret
`StrInput` fields**:

    Cannot use a Credential-typed global variable in 'pg_host'.

Adding `POSTGRES_DB_SERVER` (or any other plain connectivity key) to that list
breaks **155 fields across all six flows at once**. Non-secret connectivity must
stay an ordinary environment variable, which the flows read through
`load_from_db` with a fallback to `os.getenv`.

Removing a name from the list later does **not** delete the variable from
Langflow's database — the row has to be dropped by hand.

### Rule 2 — never set `LANGFLOW_FALLBACK_TO_ENV_VAR=false`

The default is `true`, and it is exactly what makes the fallback in rule 1 work.
Turned off, each of those 155 fields fails with `<NAME> variable not found.`
Secrets keep working (they are in the database), so the failure looks like a
Postgres or network problem rather than a flipped flag.

### Rule 3 — a variable edited in the Langflow UI stops following `.env`

Once someone edits a global variable in the UI, its `updated_at` passes
`created_at`, Langflow marks it user-modified, and `.env` **never overwrites it
again**. If you change a key in `.env`, restart, and nothing happens: delete the
variable in the UI and restart again.

### Rule 4 — `LANGFLOW_SECRET_KEY` is set once, before the first start

It encrypts the global variables. Changing it later makes the existing ones
undecryptable; they have to be deleted and recreated. It must be a Fernet key —
exactly 32 random bytes in url-safe base64 (44 characters, trailing `=`). An
ordinary password of similar length is **not** a valid Fernet key.

```bash
python3 -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

### Rule 5 — hidden fields do not resolve

A field with `show=False` does not resolve its global variable. It needs
`override_skip=True` as well. A UI input must also be materialised in the node
template, not only in the component code — otherwise it exists for the code and
not for the person configuring it.

### Rule 6 — values in `.env` are literal

No `${OTHER_VARIABLE}` references. Compose would interpolate them, but
`scripts/envfile.py:read_env()` would not — and the tooling would then silently
disagree with the containers about what is configured.

### Where a given knob belongs

* Connectivity, ports, hostnames → **environment variable**.
* An API key a flow component uses → **Langflow global variable**, sourced from
  env via rule 1.
* Which model a flow component calls → **the component's own input in the
  Langflow UI**. Not the settings row; the settings row governs behaviour, not
  which model a flow uses.
* Thresholds, cascade order, chunking, reranking → **`settings.config`**.
* Wording, stop words, synonyms, blocked topics, document types, grades →
  **their own registry table**, never code and never the settings.

---

*Updated by Martin Šafránek on 2026-08-28.*
