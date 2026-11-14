# QA portal — binding contract v1

Track: contract. Owner: lead. Contract version: **qa-portal/1.8.0** (1.5.0 + §14.8 validity edit, list filters, source check, URL preview, `/knowledge/` address; + §14.9 try a question; + §14.11 debug, §14.12 overview, §14.13 content, §14.14 unchanged content and refresh).
Basis: source HEAD `9fc015c`, integration baseline `8211a85`.

This file is authoritative for the portal implementation. Where it diverges
from `.claude/qa-portal/brief.md` or `category-db.md`, this file prevails:
those drafts were produced without reading the real writer and schema, and this contract is written
against them. Every deviation is justified below and marked **[DEVIATION]**.

No worker may change the shape of the API or the schema. A need for change goes to the lead.

---

## 1. Architecture and boundaries

```
browser (VPN)
   │  /knowledge/     static SPA
   │  /qa-api/v1/     JSON API
   ▼
edu_ai_nginx (listener 80 = VPN side; NEVER listener 81 / search tunnel)
   ▼
edu_ai_qa_admin   ← NEW container, a single port 8082, no `ports:` on the host,
   │                 networks edge + app, WITHOUT PG_* and without the `data` network
   ├──► http://edu_ai_search:8090/internal/qa-admin/*   (data reads, catalogs, uploader)
   └──► http://edu_ai_api:8080/v1/ingest                 (publication via the existing ingest)
```

The portal has **no** database connection. All data access goes through the internal
API of the search service, which continues to pass through `search/app/scope.py`.

The portal state (import previews, operations, temporary exports) lives in the named
volume `qa_admin_state`, outside the frontend's static directory. It is not a second database
of published QA data — only short-lived operational state.

---

## 2. Identity, policy and ownership

### 2.1 Administrator context

The MVP has no login. The backend creates an internal `Principal`:

```python
Principal(kind="vpn_admin", subject="vpn-admin", schools="ALL_ACTIVE")
```

The principal is created **exclusively server-side**. Identity is never read from the JSON body or
from client headers. Policy is the single decision point:

```python
class Policy(Protocol):
    def allowed_schools(self, p: Principal) -> SchoolSelector: ...
    def may_read_pack(self, p: Principal, school: str) -> bool: ...
    def may_publish(self, p: Principal, school: str) -> bool: ...
    def may_replace(self, p: Principal, school: str) -> bool: ...   # import mode replace
    def may_manage_categories(self, p: Principal) -> bool: ...
```

The MVP implementation `VpnAdminPolicy` returns all active schools. A future login
will replace `PrincipalProvider` + `Policy`, not the publication logic.

**Mandatory policy test** (delivered by qa-portal-backend): with a policy restricted to one school,
a request for another school must end in 403 even when the client sends a foreign code
in the path, in the body and in the ZIP manifest. A parameter must not widen the rights.

### 2.2 Technical uploader (school → staff_user mapping)

The existing ingest requires an active `staff_user` exactly for the chosen entity
(`GET /v1/ingest-principal`). `staff_user.user_id` is **globally unique**
(PRIMARY KEY on `user_id`), so the same `user_id` cannot be used for several schools.

Contract: the search service exposes `GET /internal/qa-admin/uploader?legal_entity=…`,
which **looks up the mapping and, if it is missing, creates it** (get-or-create) according to a fixed
convention:

| Field | Value |
|---|---|
| `user_id` | `qa_portal__{legal_entity_code}` |
| `legal_entity_code` | the given school |
| `display_name` | `QA portál (technický účet)` |
| `is_active` | `true` |
| `notes` | `Technický vykonavatel administračních publikací QA portálu. Není fyzická osoba.` |

It is created **exclusively** by the search service (which has DB access), idempotently
(`INSERT … ON CONFLICT DO NOTHING`), and only for an entity that is active.
The portal invents no UUIDs and does not pick a random employee.
**[DEVIATION]** the brief assumed manually prepared records at deployment; the convention
here is deterministic and auditable, requires no seed with fictitious data
and cannot be confused with a real person. It is not a migration — these are application writes
of the same class as `ingestion_log`.

When the entity does not exist or is inactive: `404`. The school is then readable only if
it exists, but publication returns `PUBLISH_UPLOADER_UNAVAILABLE` (see §7).

### 2.3 Preserving ownership — MANDATORY writer fix

**Established state (node `PostgresInsertQA-qa004-QA`):** the UPDATE branch sets
`owner_user_id = meta.user_id`, and every CSV row whose `entry_key` already exists
in the pack goes into `to_update` — regardless of whether the content changed. Every reimport
therefore currently overwrites the owner of all affected items with whoever is uploading at that moment.
The same applies to `qa_variants` (they are overwritten wholesale).

That is incompatible with the requirement "an edit must not change the original owner". The CSV has no
owner column and the ingest accepts a single `user_id` per file, so this
**cannot** be worked around on the portal side.

Binding change (owner: qa-portal-data, file `EDU_AI_QA.json`):

- The query for existing items also loads `owner_user_id`:
  `SELECT entry_key, entry_id, owner_user_id FROM qa_entries WHERE …`.
- In the **UPDATE** branch `owner_user_id` is **not overwritten** — the item's original
  value is written. `visibility`, `audience_roles` and the other scope columns
  behave unchanged (see §2.4 for their protection at the portal boundary).
- In the **INSERT** branch `owner_user_id` is written from the upload metadata (a new item
  is created under whoever creates it) — unchanged.
- `qa_variants` receive the `owner_user_id` of **their item** (not of the uploader),
  so that the denormalized scope matches the row in `qa_entries`.
- If the original owner is no longer an active `staff_user` of the entity, the value is
  preserved anyway: the FK points to `staff_user`, which is never deleted, only deactivated.

This change affects **all** ingest paths, not just the portal. It is a fix:
the existing behaviour silently overwrote provenance on every reimport. It must be
covered by a test (old item + reimport under a different uploader → owner unchanged;
new item → owner = uploader).

### 2.4 Metadata that the portal must not invent

The writer applies the upload metadata to **all** rows: `visibility`,
`audience_roles`, `school_grade_ids`, `school_subject_ids`, `school_level`,
`valid_from`, `valid_to`. The portal therefore **loads them server-side from the current state
of the pack and sends them back unchanged**; it never uses form defaults.

**Metadata divergence within a pack.** Because the ingest can send only one set for the
whole pack, a pack whose items today have different metadata would, by publication, get
uniform metadata and the scope of visibility or grades would silently change.

Contract: the internal API returns per-item metadata and a `metadata_uniform` flag.
If `metadata_uniform == false`, the portal **refuses to publish** with the error
`PACK_METADATA_DIVERGENT` (409) listing the differing items and fields. Reading,
export and preview remain available. **[DEVIATION]** the brief left this option
open; refusal is the only behaviour that cannot silently widen visibility.
Resolving divergent packs is a separate stage outside the MVP scope.

### 2.4a New pack — metadata confirmed by the administrator (v1.1.0)

§2.4 applies unchanged to an **existing** pack: the portal reads its metadata from the pack
state and sends it back unchanged. A new pack, however, has no state, so there was nowhere to load
it from and the first publication ended with `audience_roles: at least one role is
required`. The missing data cannot be filled in with form defaults — the portal would thereby
decide on the reach of the content on the administrator's behalf.

The contract therefore distinguishes by whether the pack exists:

| Pack state | `pack_metadata` in `POST …/preview` | Source of metadata for ingest |
|---|---|---|
| **does not exist** (`pack-state` returns nothing) | **required** | stored preview (`preview.pack_metadata`) |
| **exists** | **forbidden** (422) | pack state, as before (§2.4) |

- Missing for a new pack → `422 VALIDATION_ERROR`, `fields[].field = "pack_metadata"`.
- Sent for an existing pack → `422 VALIDATION_ERROR`, likewise. The refusal is
  deliberate: one set of metadata is applied to **all** rows of the pack, so
  accepting it would silently overwrite the visibility of the whole existing pack.
- Publication of a new pack takes the metadata **only** from the stored preview, never from the body of
  the publication request (§8).

**Shape of `pack_metadata`:**

```json
{"visibility": "entity|organization",
 "audience_roles": ["TEACHER"],
 "school_grade_codes": ["1","2"],
 "valid_from": "2026-09-01", "valid_to": null}
```

- `visibility` — the portal offers **only** `entity` and `organization`. `private` is
  nonsense for a QA pack (only the technical uploader would see it) and the portal rejects it.
  Whether a unit may publish `organization` is still decided by the setting
  `child_may_publish_organization` in the gateway; the refusal is reported as `UPSTREAM_ERROR`.
- `audience_roles` — at least one item, each a code from the `role` code list.
- `school_grade_codes` — codes from the `school_grade` code list, may be an empty array.
- `valid_from` / `valid_to` — ISO date or `null`; `from <= to`.
- `school_subject_ids` and `school_level` are not set by the portal: the current ingest
  boundary does not accept them for QA. A documented limitation, not an omission.

**Validation against code lists.** Codes are verified against `GET /v1/code-lists?legal_entity=`
(`role`, `school_grade`) — the same source used by the gateway. An unknown code →
`422` with `field` `pack_metadata.audience_roles[i]` / `pack_metadata.school_grade_codes[i]`
(the index points to the specific faulty item). An unreadable date is `422`
with `pack_metadata.valid_from` / `…valid_to` — the portal does not let it through to the gateway.
**[DEVIATION]** when the code list is unavailable, the gateway lets the input through unverified; the portal
in that case returns `503 UNAVAILABLE` (`retryable: true`). An administrator tool
has no reason to write unverified metadata, and a retry is cheap here.

For the offer in the dialog there is `GET /schools/{school}/metadata-options`
(codes + labels with the fallback per §4). The visibility labels are a frontend
token (§10), not a database code list.

**Out of scope of N4 (open):** import of CSV/ZIP into a not-yet-existing pack
remains unresolved — there the metadata are confirmed the same way, but the import preview
does not carry them. Today it ends in an ingest error; the fix belongs in a separate slice.

### 2.4c Import into a not-yet-existing pack (v1.2.0)

§2.4a handles a pack created in the dialog. CSV/ZIP import had the same hole: the file
passes validation, but a pack that does not exist yet has no metadata and publication
ends in a gateway error. Import therefore uses the **same validation and the same explicit
confirmation** as the dialog — nothing is inherited from the manifest or from defaults.

**Field `pack_metadata_by_pack`** (multipart, JSON object) in
`POST /schools/{school}/imports/preview`:

```json
{"faq-provoz": {"visibility": "entity", "audience_roles": ["PARENT"],
                "school_grade_codes": ["1"]}}
```

One shape for CSV and ZIP: **the key is always `pack_id`**, so for a ZIP the
metadata are assigned to a specific pack and cannot be accidentally received by another.
For a CSV the key must match the confirmed `pack_id`.

Rules for each pack in the import:

| Pack | Metadata sent | Behaviour |
|---|---|---|
| new | yes | validation against code lists (same function as §2.4a), stored with this pack in the preview |
| new | no | the preview is **200** and shows the diff, but the pack has `metadata_required: true` and `publishable: false`; publication refuses it |
| existing | yes | `422 VALIDATION_ERROR`, field `pack_metadata_by_pack.<pack_id>` — an existing pack keeps its metadata (§2.4) |
| not in the import | yes | `422`, field `pack_metadata_by_pack.<pack_id>` ("no such pack in this import") |

- **[DEVIATION]** from §5.3: missing metadata confirmation is **not** an error in the content
  of the file, so the CSV preview stays `200` and does not return `422`. Otherwise the client
  would have no way to find out for which packs to ask the administrator. It cannot
  be published, though: publication of such a pack ends `failed` with the message
  `PACK_METADATA_REQUIRED` and **ingest is not called for it at all**.
- Faulty codes are reported as `422` with the same indexed field names as §2.4a,
  plus `file`. An unavailable code list → `503 UNAVAILABLE`.
- **A ZIP manifest is not a confirmation.** The `manifest.json` from a portal export may
  be returned by the preview as `suggested_metadata` for a new pack, so that the UI can
  prefill the form, but **only** the value the administrator sends in `pack_metadata_by_pack`
  is binding. An import from a foreign file thus cannot silently
  determine the reach of the content.
- Publication takes the metadata of a new pack **exclusively** from the stored preview;
  the body of the publication request never contains them. An existing pack still reads them
  from the pack state.

---

## 3. Pack version, concurrency and serialization

`qa_pack_manifest.version` is an integer that the writer increments on every
ingest. By itself it is not enough: it does not catch a change made by another tool between
versions, and the portal also needs the identity of the **content** the user saw.

The contract introduces `pack_etag`:

```
pack_etag = "sha256:" + sha256(
    canonical_json({
      "version": <manifest.version>,
      "built_at": <manifest.built_at ISO8601>,
      "entries": [ {entry_key, entry_id, canonical_q, answer_md, category_code,
                    domains(sorted), variants(sorted), sources, is_active}
                   … sorted by entry_key ]
    })
).hexdigest()
```

It is computed by the **search service** in `/internal/qa-admin/pack-state`, so that it has a single
owner and cannot be broken by a different serialization in the portal.

Rules:

- Every read of items returns `pack_etag` and `pack_version`.
- `POST …/preview` accepts `base_etag`; if it does not match the current one → `409
  PACK_VERSION_CONFLICT` (+ the current `pack_etag`).
- `POST …/publish` accepts `preview_id` **and** `expected_etag`. The server verifies
  the etag again just before the write, inside the lock.
- **Serialization:** the backend holds a lock per `(school, pack)` for the entire
  "verify etag → write → verify result". The lock is in `qa_admin_state` (a file
  with exclusive creation + PID/time, with a TTL for an orphaned lock), so it survives
  concurrent requests in the same process too. A mere preliminary GET of the version does not solve the race
  and the portal does not rely on it.
- **Lock ownership:** the lock carries the holder's unforgeable token. Only
  **the one who holds it** may release it: `release()` removes the file only when
  it still contains its own token. Taking over an orphaned lock after the TTL thereby
  **invalidates** the previous holder — its later `release()` has no effect and must not
  delete the new holder's lock.
- **Concurrent takeover:** when several requests compete for an orphaned lock, exactly
  one succeeds; the others get `409 PUBLISH_IN_PROGRESS`, never a 500 and never
  a second simultaneous holder. This also applies to `import/publish`, where several packs are locked.
- **Known limitation (to be documented, not concealed):** the lock is local to a single
  instance of `edu_ai_qa_admin`. The MVP runs as a single instance. Scaling to multiple replicas
  would require a shared lock (e.g. an advisory lock in Postgres via search) — out of
  scope, but it must be in the operations documentation.
- No automatic rebase and no overwriting of someone else's newer change.

---

## 4. Categories (global code list `qa_category`)

### 4.1 Model

Exactly per `category-db.md`: `edu_ai.code_list` with `list_code='qa_category'`,
`legal_entity_code` always `NULL`, labels in `labels` (jsonb `{"cs": "Družina"}`),
a stable `code`. `qa_entries.category_id` is a nullable FK to `code_list(id)`.

Label fallback: requested language → default UI language → stable `code`.
**An unspecified `lang` means the default UI language**, not "any language": when
a translation exists for it, it is the language that asked, and `label_fallback` is
therefore `false`. `true` is reserved for a **genuinely missing** translation
in the requested language. A missing translation **never** drops a category from the offer
and does not change the assignment. The same applies to domains.

The exact DDL is in `contracts/qa-portal/proposed-215_qa_category.sql`.
The migration is not run without the user's approval (AGENTS.md "Ask first").

### 4.2 Two-stage transition from `tags.category` — **[DEVIATION]**

`category-db.md` §4 wants to switch reading to `category_id` in one coordinated
release. That **cannot** be done safely in this delivery:

`search/app/nodes/qa_common.py::category_siblings` today groups related
questions by textual equality of `tags->>'category'`. After switching to `category_id`,
all existing items (where `category_id IS NULL` until an approved
backfill over real production data) would stop offering related questions.
A reviewed mapping `(school, pack, entry, old cat) → code` requires a
production inventory that this delivery does not have available.

Therefore:

| Stage | Content | This delivery |
|---|---|---|
| **1** | Schema, catalog, API, editor, CSV `category_code`, export/import, writing `category_id` from the portal | **YES** |
| **2** | Reviewed backfill of `category_id` + switching `category_siblings` and the export to `category_id` as the single source of truth | **NO** — separate approval over real data |

In stage 1 therefore:

- `category_siblings` stays **unchanged** (reads `tags.category`). The runtime does not
  change, related questions do not disappear, the `--repeat` baseline is comparable.
- On writing, the portal keeps both consistent: `category_id` is authoritative for the API
  and the new CSV column; `tags.category` receives the label of the assigned category
  in the pack's language (or empty for `null`). No second authoritative
  assignment arises — `tags.category` is derived text that the runtime still uses
  for grouping and stage 2 demotes it to a purely legacy value.
- Migration 215 **does no backfill** and **has no taxonomy seed**. Categories
  are created explicitly by the administrator via the API. The example codes from `category-db.md`
  (AFTER_SCHOOL_CARE, SCHOOL_PROPERTY, SCHOOL_SUPPLIES) are not an approved seed.

### 4.3 Write rules

- The API accepts `category_code: string | null`. The internal `category_id` is resolved
  server-side and is never exposed in the API or CSV.
- An unknown code or a **newly assigned** inactive category → `422` before the write.
- An inactive code **already assigned to the same item** may survive a round trip unchanged.
- Creating categories: `POST /qa-api/v1/categories` → internal
  `POST /internal/qa-admin/categories`. Code, `labels`, `sort_order`, `notes`.
  `code`/`list_code`/`id` are immutable after creation, renaming goes through `labels`.

---

## 5. CSV and ZIP

### 5.1 Columns of a single pack

The column order stays. Format: UTF-8 **BOM**, CRLF, RFC 4180 quoting.
**One** column `category_code` is added at the end:

```
entry_key,canonical_q,answer_md,cat,variants,sources,is_active,domains,category_code
```

#### Delimiter — write with comma, read both (v1.4.0)

- **Writing.** Every CSV the system newly creates uses the delimiter `,`.
  This applies to the pack export from the portal, packs in the export ZIP, the CSV that the portal
  passes to ingest on publication, and `/v1/qa-export`. One format on all
  production outputs — the delimiter is not changed on one side only.
- **Reading.** Pack import and the shared QA ingest accept both `,` and `;`. An old
  semicolon-delimited pack therefore remains a valid input without conversion.
- **Detection in the portal is deterministic, not heuristic.** The parser tries the
  header in turn as `,` and as `;` and picks the delimiter with which the header
  matches a known set of columns (the new nine-column or the legacy eight-column one).
  When neither matches, the existing error `unexpected header` is reported
  — never guessing by character frequency.
- **Quoting is handled by the `csv` module, not by manual assembly.** A cell with `,`, `;`, `"`
  or a line end (multiline answer, variants, sources) is enclosed in quotes
  and an inner quote is doubled. BOM and CRLF stay because of Excel.
- `domains` stays a JSON array in a single cell. Because a JSON array contains
  commas, this cell is always quoted in comma CSV — the round trip does not change.

- `cat` stays as legacy readable text (the category label), it does not decide.
- `category_code` present and non-empty → determines the category.
- `category_code` present and empty → **explicitly no category**.
- The `category_code` column **missing entirely** (old format) → ingest leaves
  `category_id` unchanged on UPDATE and `NULL` on INSERT. Translating the old `cat` to a
  code is done **exclusively by the import preview in the portal** against a reviewed mapping
  or by asking the user; the shared ingest never guesses and never creates categories.
- A conflict between `cat` and `category_code` is shown in the preview; `category_code` wins.
- **Codes** are carried, never database IDs.

`domains` keeps today's meaning at the import boundary: an empty cell inherits the
upload metadata, `[]` explicitly empties, a non-empty set replaces.
**The editor always sends domains explicitly as an array** (even empty) — inheritance is only
a property of the CSV boundary, not of the API.

### 5.2 ZIP

`packs.zip`:
```
manifest.json
packs/<pack_id>.csv
```
`manifest.json` carries `legal_entity_code`, `exported_at`, `portal_version`,
`contract_version` and for each pack `pack_id`, `lang`, `version`, `pack_etag`,
`entry_count`, `metadata` (grades/subject/level/visibility/audience_roles/validity).

It **never** contains passwords, tokens or `user_id`. The `user_id` at import is determined by the
server-side mapping of the technical uploader (§2.2).

Import of a ZIP with a **foreign** `legal_entity_code` in the manifest → `422
IMPORT_ENTITY_MISMATCH`. In the MVP packs are not transferred between schools.

CSV export is labelled in the UI and documentation as **not a complete backup** (it does not contain
all the pack metadata).

### 5.3 Bulk import — no partial write within a pack

- Default mode `merge`. Missing rows delete nothing.
- A pack that **does not yet exist** in the import needs confirmed metadata
  (§2.4c). Without them the preview is readable, but publication of that pack is refused.
- `replace` only for an administrator, with an explicit list of deactivated items
  and a separate confirmation (`confirm_replace: true`).
- **The whole input is validated before any write**: structure, mandatory
  question/answer, duplicate `entry_key` within a pack, the entity's domain catalog,
  `category_code`, permissible metadata. A single faulty domain or category code
  → no QA change in that pack.
- For a ZIP, **a single transaction across all packs is not guaranteed**. The report gives
  the state per pack (`ok` / `failed` / `skipped`) and faulty packs can be safely
  retried separately. Success is not claimed before the pack is committed.
- Errors give `file`, `row` (row number in the file), `field` and an understandable fix.

**Clarification in v1.0.0 (implementation C4a, the statuses were already in openapi.yaml):**

- One CSV = exactly one pack, so a faulty row means that nothing from this import
  can be published. `POST /imports/preview` therefore returns **422** with `fields`
  (`file`, `row`, `field`, `message`), not 200 with `ok: false`. The client thus
  cannot mistake the response for a usable preview.
- A ZIP stays **200** with per-pack `ok`/`errors` — a faulty pack can be retried
  separately, healthy packs are not blocked by it.
- `GET /exports/{export}/download` has no school in the path and the stored export metadata
  do not carry an owner. With today's `VpnAdminPolicy` (one administrator for
  all active schools) this restricts nothing; **any future per-school
  policy must first add the owner to `export_service`**, otherwise
  the check on download would be only apparent.

---

## 6. Operations and publication state

`POST …/publish` and `POST …/imports/{id}/publish` return `202` with an `operation_id`
(a UUID minted by the portal **before** calling ingest) and a state.

```
validating → accepted → processing → completed | failed
```

- The state is stored in `qa_admin_state` (persistent volume), keyed by
  `operation_id`, together with `school`, `pack`, `preview_id`, `base_etag`,
  `processing_uuid` (once ingest returns it) and a summary of the result.
- `GET /qa-api/v1/operations/{id}` returns **only this** stored correlation.
  It is **forbidden** to derive the state of one's own publication from `GET /v1/ingest-status`
  by `since_seconds` — that is a global search of the latest runs, not a matching.
  `ingest-status` may be used exclusively by querying a **known** `processing_uuid`.
- The existing `POST /v1/ingest` is synchronous. The portal calls it in a separate
  thread/executor so that it does not block the UI and API handling, and it updates the state as it goes.
  No percentage progress is promised.
- **Bulk import: the operation state must match the packs.** `completed` only when
  **no** pack ended `failed`. As soon as at least one fails, the operation is
  `failed` with the code `IMPORT_PACKS_FAILED` and a message with counts; the detail stays
  in `packs[]`. The earlier behaviour reported `completed` even when all
  packs failed — the administrator saw success and the data did not change.
- **Never fill in missing metadata of an existing pack.** If the pack state
  lacks `visibility` or `audience_roles` (a pack without active items), the portal
  **refuses** publication with `422` and the field `pack_metadata` and asks for confirmation as
  for a new pack. A default would silently determine the reach of the content (§2.4).
- `completed` is set **only after verifying** that ingest ended in success **and**
  a subsequent read of the pack returns the expected new `pack_etag`. Closing the HTTP request
  or an animation is not proof of success.
- **After a service restart:** operations in state `accepted`/`processing` are marked
  `unknown` and **first look up** (by `processing_uuid`, otherwise by comparing
  `pack_etag`). They are never blindly started again. On a timeout no
  further import is created.
- Standard logs do not contain the texts of questions and answers — only identifiers
  (`operation_id`, `school`, `pack`, counts).

---

## 7. Errors

The same envelope as `contracts/errors.yaml`:
`{"error": {"code": …, "message": …, "retryable": bool}}`, for `422` additionally
`fields: [{field, message}]`. The client branches on `code`, never on text.

| HTTP | code | When |
|---|---|---|
| 400 | `BAD_REQUEST` | Malformed path/parameter |
| 403 | `FORBIDDEN` | Policy did not permit the school or operation |
| 404 | `NOT_FOUND` | Unknown/inactive school, pack, preview, operation, export |
| 409 | `PACK_VERSION_CONFLICT` | `base_etag`/`expected_etag` ≠ current state |
| 409 | `PACK_METADATA_DIVERGENT` | Items of the pack have different metadata (§2.4) |
| 502 | `IMPORT_PACKS_FAILED` | Bulk import: at least one pack ended `failed` (detail in `packs[]`) |
| 409 | `PACK_ID_TAKEN` | `POST /packs` for an already existing `pack_id` (canonical code, not `PACK_EXISTS`) |
| 409 | `PUBLISH_IN_PROGRESS` | Another publication is running for (school, pack) |
| 409 | `DELETE_CONFIRMATION_MISMATCH` | The retyped pack identifier does not match (§13) |
| 413 | `PAYLOAD_TOO_LARGE` | Import or document upload over the limit (nginx and application) |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | Import is not CSV/ZIP; a document upload has an unsupported extension (§14.4) |
| 429 | `INGEST_QUEUE_FULL` | The school's document queue is full (§14.4) |
| 422 | `VALIDATION_ERROR` | Content validation (`fields` field) |
| 422 | `IMPORT_ENTITY_MISMATCH` | The ZIP manifest targets a different school |
| 422 | `UNKNOWN_CATEGORY` / `INACTIVE_CATEGORY` | Category code (§4.3) |
| 422 | `UNKNOWN_DOMAIN` | Domain code outside the entity's catalog |
| 500 | `INTERNAL_ERROR` | Unexpected error |
| 502 | `UPSTREAM_ERROR` | search/ingest unreachable or failed |
| 503 | `PUBLISH_UPLOADER_UNAVAILABLE` | The school's technical uploader is missing/cannot be verified (retryable: false) |
| 503 | `UNAVAILABLE` | Dependency temporarily unavailable (retryable: true) |

`retryable` corresponds to whether an identical retry can help.

---

## 8. Public portal API (`/qa-api/v1`)

Authoritative shapes in `contracts/qa-portal/openapi.yaml`. Overview:

| Method and path | Purpose |
|---|---|
| `GET /context` | Mode, contract version, available actions |
| `GET /schools` | Active schools according to policy |
| `GET /schools/{school}/domains` | The school's effective domain catalog |
| `GET /schools/{school}/packs` | Packs, versions, `pack_etag`, `entry_count` |
| `GET /schools/{school}/metadata-options` | Code lists for the metadata of a new pack (§2.4a) |
| `POST /schools/{school}/packs` | Verification of a new `pack_id` + optional metadata check (§2.4a); materialized only by publication |
| `GET /schools/{school}/packs/{pack}/entries` | Paginated list of items + `pack_etag` |
| `GET /schools/{school}/packs/{pack}/entries/{entry_key}` | Detail of a single item |
| `GET /categories` | Global `qa_category` catalog with labels |
| `POST /categories` | Creation of a global category |
| `POST /schools/{school}/packs/{pack}/preview` | Validation + diff against `base_etag` |
| `POST /schools/{school}/packs/{pack}/publish` | Publication of a reviewed preview |
| `POST /schools/{school}/imports/preview` | CSV/ZIP: mapping, errors, diffs |
| `POST /schools/{school}/imports/{preview}/publish` | Publication of a reviewed import |
| `GET /operations/{operation}` | State of a specific operation |
| `POST /schools/{school}/exports` | Creation of an export (CSV/ZIP) |
| `GET /exports/{export}/download` | Download of a finished export |

Rules: paginated lists (`limit` ≤ 200, `cursor`), the API never returns HTML,
publication uses **only** the content stored in the preview, never a body supplied at publication.

### CSRF / origin

UI and API are on the same origin, CORS is not opened. Every **write** request
must be "non-simple": `Content-Type: application/json` (or `multipart/form-data`
for import) **and** the header `X-QA-Portal: 1`. The server verifies `Origin`/`Referer`
against an allowed list and rejects a missing header with `403`. It applies even without a login.

---

## 9. Internal API of the search service (`/internal/qa-admin`)

A separate router, **outside** the nginx allowlist (`$search_path_allowed`), it must not be
published by any gateway route. All queries continue to go through `scope.py`;
the administrator need is resolved **here explicitly**, not by bypassing the filter
in the portal. An administrator may manage schools, but when reading a specific pack the
owning entity must match.

| Endpoint | Returns |
|---|---|
| `GET /internal/qa-admin/pack-state?legal_entity&pack_id` | `pack_version`, `pack_etag`, `built_at`, pack metadata, `metadata_uniform`, `divergent_fields` |
| `GET /internal/qa-admin/entries?legal_entity&pack_id&limit&cursor` | Items with everything: `entry_key`, `entry_id`, `canonical_q`, `answer_md`, `variants[]`, `sources[]`, `domains[]` (codes), `category_code`, `category_label`, `category_is_active`, `cat_legacy`, `owner_user_id`, `visibility`, `audience_roles`, `school_grade_ids`, `school_subject_ids`, `valid_from`, `valid_to`, `is_active` |
| `GET /internal/qa-admin/uploader?legal_entity` | `{user_id, display_name, created}` — get-or-create per §2.2 |
| `GET /internal/qa-admin/categories?lang` | The whole `qa_category` catalog including inactive ones |
| `POST /internal/qa-admin/categories` | Creation of a category (code, labels, sort_order, notes) |

Shapes in `contracts/qa-portal/internal-qa-admin.yaml`.

---

## 10. Frontend

- Own build (Vite + React), base `/knowledge/` (§14.8), modelled on `web_chat/frontend`.
- Light cream look: `#fdf6ec` background, `#1a1410` text, `#aea2f5` accent,
  Nunito, a subtle green accent for the administration context. Colors and texts as
  **frontend tokens** (`src/tokens.css`, `src/strings.cs.ts`), not hard-coded.
- Dark mode is optional and **must not** by itself override the default light look according to
  the system setting.
- Desktop: list on the left, editor on the right. Mobile: list → detail step by step.
- Markdown preview safely (`react-markdown` + `rehype-sanitize`), no executable
  HTML. Raw HTML is **not allowed** (no `rehype-raw`): a raw
  tag is not rendered.
- **The preview must not swallow content silently.** When a piece of an answer is
  not rendered for safety reasons, the preview must **visibly say so** (a short note by the preview),
  so that the administrator does not get an empty area without explanation and think that
  text was lost. This also applies to an answer loaded from the API, not only to one being typed.
- Work-in-progress changes in browser memory, prominently signalled. Switching the question
  keeps the edits; switching school/pack or closing the page is protected by a confirmation
  against silent loss. A response from an older school load **must not** overwrite a newer
  selection (controlled by request generations/AbortController).
- Keyboard accessibility, visible focus.

### 10.1 Modal dialog — real scrolling of the content (v1.4.0)

Applies to the shared `Dialog` shell, therefore also to **Nový balíček**.

- The dialog has three bands: **header**, **scrollable content**, **footer with
  confirmation buttons**. The header and footer always stay visible;
  only the middle band scrolls.
- Scrolling must actually work, not merely be set in CSS. The dialog's grid
  therefore defines rows explicitly (`auto minmax(0, 1fr) auto`), so that the middle
  track can be shorter than its content; `min-height: 0` on the item alone
  is not enough and the content is merely clipped.
- The content must be scrollable by **wheel, touch and keyboard**. The scroll
  band is therefore its own focusable region (`tabindex="0"`, named),
  but it **does not take the initial focus** — that still belongs to the first control of
  the form.
- On a short desktop window and on mobile, the **last item of the longest list (grades)**
  and the **confirmation button** must be reachable. The proof is a browser test with a real window size,
  not a unit test in jsdom — that does not
  measure dimensions and cannot prove reachability.
- No redesign: layout, colors and texts do not change.
- Until integration, it is built against **frozen JSON fixtures** derived from this
  contract (`qa_portal/frontend/src/fixtures/`), not against the live backend.

---

## 13. Permanent deletion (question and pack)

Besides deactivation (`is_active=false`), which remains the default path, the portal can
**permanently remove content from the database**. It is irreversible and has no undo.

### 13.1 What is deleted and what is not

| Deleted | Remains |
|---|---|
| `qa_entries` of the selected item (for a pack all its items) | `code_list` and all shared code lists |
| `qa_variants` — via FK `ON DELETE CASCADE`, so also inactive ones | `question_answer_log` including `matched_entry_id` (the FK does not exist, the row remains with a reference to an already nonexistent item — the operational audit is not rewritten) |
| `qa_embedding` by `entry_id`, **all generations**, active or not | `embedding_registry` (the global registry of generations, not an item's history) |
| for a whole pack additionally **all** `qa_pack_manifest` rows for the given (school, pack) pair | documents, chunks and anything outside QA |

Inactive rows are deleted too: "permanently" means including history, otherwise
they would remain matchable via partial indexes after reactivation.

### 13.2 Rules

- **Transaction.** The whole deletion is a single `BEGIN`/`COMMIT`. An error in the middle must not
  leave an item without variants or a pack without a manifest.
- **School check.** Every statement has `legal_entity_code` in its `WHERE`. A pack or
  item of another school must not be deleted or displayed — the same rule as for reading.
- **Write protection.** `X-QA-Portal` and the origin check (§8) apply, as for
  publication.
- **Lock.** Deletion holds the same `PackLock` for (school, pack) as publication,
  for its whole duration. A concurrent publication gets `409 PUBLISH_IN_PROGRESS` and vice versa.
- **Confirmation.** Before deletion the client must display the school, the name and the scope
  (number of items). For a **whole pack** the administrator must **retype its
  identifier**; the server rejects a mismatch with `409 DELETE_CONFIRMATION_MISMATCH`.
- **Invalidating previews.** After deletion the server discards all stored previews for
  the given (school, pack) pair. Publication of an older preview thus cannot resurrect a deleted
  pack or question. If one arrived anyway, it ends on an etag mismatch
  (§3), never by silent re-creation.
- **No archiving.** Nothing is copied to another table. The backup is the database backup.

### 13.3 API

| Method | Path | Meaning |
|---|---|---|
| `DELETE` | `/qa-api/v1/schools/{school}/packs/{pack}/entries/{entry_key}` | one question |
| `DELETE` | `/qa-api/v1/schools/{school}/packs/{pack}` | whole pack; body `{"confirm_pack_id": "<pack>"}` |

Both return `200` with the counts of deleted rows
(`{"deleted": {"entries": n, "variants": n, "embeddings": n, "manifests": n}}`),
`404 NOT_FOUND` for a foreign or nonexistent school/pack/question.
Internally the portal calls `DELETE /internal/qa-admin/entries` and
`DELETE /internal/qa-admin/packs` of the search service; only that touches the database.

## 14. Documents (v1.5.0)

A second page of the portal manages the school's ingested **documents** (not QA).
Design rationale: `specs/active/spec.qa-portal-documents.md`. This section is the
binding shape; nobody changes it without the lead.

### 14.1 Terms

- **Version** = one `file_uuid` (its `document_metadata` row plus the
  `document_chunks` / `document_elements` / `document_tables` rows with that
  `file_uuid`).
- **Document** = the versions of ONE school (`legal_entity_code`) sharing a
  **source key**: `'url:' || source_url` when `source_url` is not null, else
  `'file:' || original_file_name || '|' || coalesce(user_name, '')`. This is the
  key the ingest flow uses to retire older versions. A row with neither
  `source_url` nor `original_file_name` falls back to `'uuid:' || file_uuid`,
  so it is a document of its own instead of merging with every such row.
- **Shown version** of a document = its active version if it has one, else its
  newest version (`ldts_gmt DESC, file_uuid DESC`).
- A document is **active** when it has an active version (`is_active = true`).
- Any `file_uuid` of a document identifies the whole document in every path below.

### 14.2 Internal search API (`/internal/qa-admin`, search only)

Same rules as §13.2: one transaction per request, `legal_entity_code` in EVERY
`WHERE`, a foreign or unknown document is `404 NOT_FOUND`, an `file_uuid` that is
not a UUID is `400 BAD_REQUEST`. Error envelope as §7.

| Method | Path | Response `200` |
|---|---|---|
| `GET` | `/documents?legal_entity=&status=active\|inactive\|all&q=&limit=&cursor=` | `{"documents": [DocumentSummary], "next_cursor": str\|null}` |
| `GET` | `/documents/{file_uuid}?legal_entity=` | `{"document": DocumentSummary, "versions": [DocumentVersion]}` |
| `POST` | `/documents/{file_uuid}/deactivate?legal_entity=` | `{"document": DocumentSummary}` |
| `POST` | `/documents/{file_uuid}/activate?legal_entity=` | `{"document": DocumentSummary}` |
| `DELETE` | `/documents?legal_entity=&file_uuid=` | `{"deleted": {"versions": n, "chunks": n, "elements": n, "tables": n}, "file_uuids": [str]}` |

- `status` default `active`; `inactive` = documents with no active version.
  `q` = case-insensitive substring of `source_url` or `original_file_name`.
  `limit` 1–200, default 50. `cursor` is opaque (an offset is fine); order is
  shown version `ldts_gmt DESC, file_uuid DESC`.
- **deactivate** sets `is_active = false` on the document's active version(s).
  Idempotent (already inactive → `200`, unchanged).
- **activate** sets `is_active = true` on the NEWEST version and `false` on all
  others of the document. Idempotent. Never re-activates an older version.
- **DELETE** removes every version of the document from the four tables. Not
  touched: `ingestion_log`, `question_answer_log`, code lists.

```
DocumentSummary = {
  file_uuid: str,                 # the shown version
  title: str,                     # source_url, else original_file_name, else file_uuid
  source_kind: "web" | "file",    # "web" iff source_url is not null
  source_url: str | null,
  original_file_name: str | null,
  document_type: str | null,      # document_type_user, else document_type_llm
  document_language: str | null,  # document_language_user, else document_language_llm
  school_subject: str | null,     # school_subject_user
  school_grade_codes: [str],      # code_list codes of school_grade_user ids (empty if none)
  domain_codes: [str],            # taxonomy_rules.type_code of document_domain_user, else _llm
  audience_roles: [str],
  visibility: "private" | "entity" | "organization",
  valid_from: "YYYY-MM-DD" | null,
  valid_to: "YYYY-MM-DD" | null,
  user_name: str | null,
  ingested_at: ISO-8601 str,      # ldts_gmt of the shown version
  is_active: bool,                # document has an active version
  version_count: int,
  chunk_count: int                # chunks of the shown version
}
DocumentVersion = { file_uuid, processing_uuid, ingested_at, is_active, chunk_count }
```

### 14.3 Ingest gateway (`edu_ai_api`)

```
DELETE /v1/documents/{file_uuid}?legal_entity=<code>
200 {"deleted": {...as 14.2}, "file_uuids": [str], "files_removed": n, "warnings": [str]}
400 file_uuid not a UUID · 404 foreign/unknown · 503 search unreachable
```

The gateway calls `DELETE /internal/qa-admin/documents` first. Only after a
`200` it removes, for each returned `file_uuid`, the files in `UPLOAD_DIR`
whose name starts with `"<canonical uuid>_"`. Nothing from the request is used
as a path. A failed unlink adds a warning and still returns `200`; so does a
`200` from search that cannot be read (the rows are gone): the files of the
requested `file_uuid` are removed and a warning says so. Logs carry
`legal_entity`, `file_uuid` and counts only. Error bodies keep the gateway's
existing FastAPI `{"detail": …}` shape.

### 14.4 Portal API (`/qa-api/v1`)

Every write passes `csrf_guard` (§8). Every route resolves the school through
the policy (§14.6) BEFORE anything else.

| Method | Path | Policy | Response |
|---|---|---|---|
| `GET` | `/schools/{school}/documents?status=&q=&limit=&cursor=` | read | `200` as 14.2 list |
| `GET` | `/schools/{school}/documents/{file_uuid}` | read | `200` as 14.2 detail |
| `GET` | `/schools/{school}/document-options?lang=` | read | `200 DocumentOptions` |
| `POST` | `/schools/{school}/documents` (multipart) | ingest | `202 {"job": DocumentJob}` |
| `GET` | `/schools/{school}/document-jobs?batch_id=` | read | `200 {"jobs": [DocumentJob]}` |
| `POST` | `/schools/{school}/documents/{file_uuid}/deactivate` | deactivate | `200 {"document": DocumentSummary}` |
| `POST` | `/schools/{school}/documents/{file_uuid}/activate` | deactivate | `200 {"document": DocumentSummary}` |
| `DELETE` | `/schools/{school}/documents/{file_uuid}`, body `{"confirm_file_uuid": str}` | delete_permanently | `200` as 14.3 |

```
DocumentOptions = {
  visibility: ["entity", "organization"],
  audience_roles: [CodeOption],     # code_list 'role'
  school_grades: [CodeOption],      # code_list 'school_grade'
  school_subjects: [CodeOption],    # code_list 'school_subject'
  languages: [CodeOption],          # code_list 'language'
  document_types: [CodeOption],     # search /v1/document-types (type_code + labels)
  domains: [Domain],                # same shape as GET /schools/{school}/domains
  validity_periods: [{id, name, valid_from, valid_to, is_default}],
  file_extensions: [".pdf", ".docx", ".pptx", ".xlsx"],     # no .txt: the gateway has no flow for it
  max_file_bytes: 52428800,
  max_batch_items: 100,
  max_child_links: 10
}
CodeOption = {code, label, label_fallback?}   # label resolution as §4.1
```

**Upload item** (`POST /schools/{school}/documents`, `multipart/form-data`):

| Field | Rule |
|---|---|
| `file` **xor** `url` | exactly one. `file`: extension in `file_extensions` (`.csv` → `415 UNSUPPORTED_MEDIA_TYPE`), non-empty, ≤ `max_file_bytes` (`413 PAYLOAD_TOO_LARGE`). `url`: absolute `http`/`https`. |
| `save_level_1_child` | `"true"`/`"false"`, default false; only with `url` |
| `max_child_links` | int 1–10, default 5; only with `url` |
| `batch_id` | required, a UUID chosen by the client |
| `metadata` | required, JSON string of `DocumentMetadata` |

```
DocumentMetadata = {
  visibility: "entity" | "organization",       # required
  audience_roles: [str],                        # required, >= 1
  document_type: str | null,                    # null = the LLM classifies
  document_language: str | null,
  school_subject: str | null,
  school_grade_codes: [str],
  domain_codes: [str],
  valid_from: "YYYY-MM-DD" | null,
  valid_to: "YYYY-MM-DD" | null                 # from <= to
}
```

Every code is checked against `DocumentOptions` of that school; a violation is
`422 VALIDATION_ERROR` with `fields`. More than `max_batch_items` jobs with one
`batch_id` → `422 VALIDATION_ERROR` (field `batch_id`). More than 300 jobs of the
school in `queued`/`processing` → `429 INGEST_QUEUE_FULL` (retryable: true).
`legal_entity` = the school, `user_id` = the technical uploader (§2.2); the
client can send neither. A missing uploader → `503 PUBLISH_UPLOADER_UNAVAILABLE`.

```
DocumentJob = {
  job_id: str, batch_id: str, school: str,
  source_kind: "file" | "url", source: str,      # file name or URL
  status: "queued" | "processing" | "completed" | "failed" | "unknown",
  created_at, started_at | null, finished_at | null,
  processing_uuid: str | null,
  result: {status, total_files, ok_count, error_count} | null,   # gateway's own summary
  stored_chunks: int | null,                      # from ingest-status, see 14.5
  error: {code, message} | null
}
```

`GET /document-jobs` returns the school's jobs newest first (max 200); with
`batch_id` only that batch, oldest first.

### 14.5 Ingest queue

- `POST` stores the job (`jobs/<job_id>.json`) and the file (`jobs/files/<job_id>`)
  in the state volume and returns `202` at once.
- ONE worker thread started in `lifespan` processes jobs FIFO by `created_at`,
  concurrency 1 (`QA_PORTAL_INGEST_CONCURRENCY`, only `1` is supported). It calls
  the synchronous `POST /v1/ingest` with the job's fields; the temporary file is
  removed when the job ends. `completed` = gateway `2xx` with status
  `completed`/`partial`; `failed` = gateway `4xx` (its detail in `error`) or
  status `failed`; gateway unreachable/`5xx` = `failed` with `UPSTREAM_ERROR`.
  No automatic retry.
- `stored_chunks`: when listing, for a `completed` job with `stored_chunks` null
  or 0 finished less than 10 minutes ago, the portal asks `ingest-status` by the
  job's KNOWN `processing_uuid` (never `since_seconds`, §6), sums `chunks`
  over its files and stores a positive value. Lookup failures leave it unchanged.
- Restart: `queued` stays queued; `processing` becomes `unknown` (the gateway
  returns `processing_uuid` only at the end, so nothing can be looked up).
- Finished jobs older than 7 days are pruned at startup and once an hour.

### 14.6 Policy (ready for stage 2)

`Policy` gains per-school methods; `VpnAdminPolicy` answers `True`:

```python
def may_read(self, p, school) -> bool: ...
def may_ingest(self, p, school) -> bool: ...
def may_deactivate(self, p, school) -> bool: ...
def may_delete_permanently(self, p, school) -> bool: ...
```

- `GET /schools` adds to every school `permissions: {read, publish, ingest,
  deactivate, delete_permanently}` from the policy; the UI hides what is `false`.
- `GET /operations/{id}` and `GET /exports/{id}/download` answer `404` when the
  policy does not allow reading the record's school.
- `GET /context` `may_publish`/`may_replace` come from the policy (true if the
  principal may do it for at least one school), not a literal.

### 14.7 Errors added to §7

| HTTP | code | When |
|---|---|---|
| 409 | `DELETE_CONFIRMATION_MISMATCH` | `confirm_file_uuid` ≠ the path `file_uuid` (also used by §13) |
| 429 | `INGEST_QUEUE_FULL` | The school already has 300 queued/processing jobs |

### 14.8 Validity, filters, previews and the `/knowledge/` address (v1.6.0)

**Validity state.** Every `DocumentSummary` gains
`validity_state: "current" | "expired" | "future"`, computed against `CURRENT_DATE`
in search: `future` when `valid_from > today`, `expired` when `valid_to < today`,
else `current`. Search hides `expired` and `future` documents even when active.

**List filters** (search `GET /documents` and portal `GET /schools/{school}/documents`,
all optional, combined with AND):

| Param | Meaning |
|---|---|
| `type` | shown version's `document_type` equals this code |
| `role` | shown version's `audience_roles` contains this code |
| `validity` | `current` / `expired` / `future` / `all` (default `all`) |
| `source` | exact match on `source_url` OR `original_file_name` (duplicate check) |

**Edit validity** — the only metadata that can change without a new ingest:

```
search  PATCH /internal/qa-admin/documents/{file_uuid}/validity?legal_entity=
portal  PATCH /schools/{school}/documents/{file_uuid}/validity        (policy: deactivate, csrf)
body    {"valid_from": "YYYY-MM-DD" | null, "valid_to": "YYYY-MM-DD" | null}
200     {"document": DocumentSummary}
422     VALIDATION_ERROR (bad date, from > to)  · 404 foreign/unknown · 400 bad uuid
```

It sets both columns on EVERY version of the document in `document_metadata`,
`document_chunks`, `document_elements` and `document_tables` (one transaction,
`legal_entity_code` in every WHERE). Requires UPDATE on the three child tables for
`edu_ai_search` (migration 227, `postgres/role_contract/matrix.json`).

**Source check** before an upload:

```
portal  POST /schools/{school}/documents/check-sources               (policy: ingest, csrf)
body    {"sources": [{"kind": "file" | "url", "value": str}]}        # <= max_batch_items
200     {"results": [{"kind", "value", "existing": DocumentSummary | null, "supersedes": bool}]}
```

`existing` = the school's document whose `source_url` (kind url, after the same
trimming the client sends) or `original_file_name` (kind file) equals `value`.
`supersedes` = a new upload will retire it: always for `url`; for `file` only when the
existing document's `user_name` equals the technical uploader's `display_name`
(the ingest flow keys files by name AND uploader).

**URL preview** — which pages a web ingest would fetch, without Langflow:

```
portal  POST /schools/{school}/documents/preview-url                 (policy: ingest, csrf)
body    {"url": str, "save_level_1_child": bool, "max_child_links": 1..10, "metadata": DocumentMetadata}
200     {"files": [{"source_url": str | null, "source_type": str, "page_role": "parent" | "child"}]}
422     validation as §14.4 · 400 the gateway refused (its message)
```

The portal calls the gateway's `POST /v1/ingest` with `dry_run=true`. The gateway
removes every file it saved for a dry run before answering (a preview leaves nothing
in `/data/uploads`).

**Error mapping.** A search answer `500` (e.g. a database permission error) becomes
portal `502 UPSTREAM_ERROR` with `retryable: false`; network errors and `502/503/504`
stay `503 UNAVAILABLE` (`retryable: true`).

**Address.** The SPA is served at `/knowledge/` with real paths `/knowledge/qa` and
`/knowledge/documents` (`/knowledge/` opens QA), on listener 80 only; the tunnel
listener answers `404`. The former `/qa/` address is removed, with no redirect:
nginx answers it `404` like any unknown path. The API stays `/qa-api/v1`.
A legacy `#/documents` hash opens `/knowledge/documents`.

### 14.9 Try a question (v1.7.0)

An administrator asks the school's assistant from the portal, to check what the
QA packs and documents answer — the same cascade the chat uses.

```
portal  POST /schools/{school}/ask-preview                           (policy: publish, csrf)
body    {"question": str (1..2000 chars, not blank),
         "user_role": "STUDENT" | "TEACHER" | "PARENT",              # code_list 'role' of the school
         "user_school_grade": str | null,                            # a school_grade code of the school
         "retrieval_only": bool}                                     # true = no LLM call, free
200     {"answer", "outcome", "answer_source", "delivered_by", "sources", "suggestions",
         "matched_entry_id", "match_score", "duration_ms", "retrieval_only"}
422     VALIDATION_ERROR (blank/too long question, unknown role or grade)
```

The portal calls search `POST /v1/ask` with `legal_entity` = the school,
`user_id` = the school's technical account `qa_portal__<school>` (§2.2; search
refuses an unknown user_id with 403) and a fresh `query_id`, so the query log tells
a portal test from a real user by that prefix. A missing technical account →
`503 PUBLISH_UPLOADER_UNAVAILABLE`. Fields of the search response are passed through
unchanged (contracts/schemas/search-response.schema.json); nothing is stored by
the portal. A full answer costs one LLM call; `retrieval_only` shows what would be
found without it. Search `4xx` → `400 BAD_REQUEST` with its message; `500` → `502
UPSTREAM_ERROR`; unreachable/`502-504` → `503 UNAVAILABLE` (as §14.8).

### 14.10 File format and download (v1.7.0)

- `DocumentSummary.file_format` and `DocumentVersion.file_format`: the stored file's
  extension in lower case (`pdf`, `docx`, `pptx`, `xlsx`, …), `web` for a saved web
  page (`*.html`), null when unknown. Search list filter `format` =
  `pdf | docx | pptx | xlsx | web` (else 422); the portal passes it through.
- `DocumentVersion.file_name` (search internal only): the stored name
  `<file_uuid>_<name>`. The portal does not expose it.
- Download one version's stored file (original upload, or the saved HTML of a page):

```
gateway GET /v1/documents/{file_uuid}/file?legal_entity=     # file_uuid = the VERSION
portal  GET /schools/{school}/documents/{file_uuid}/download   (policy: read; GET, no csrf)
200     the bytes, application/octet-stream, Content-Disposition: attachment,
        X-Content-Type-Options: nosniff  (a saved page is never served inline)
404     foreign/unknown version, or the file is not stored (e.g. data moved without uploads)
400     bad uuid · 503 search/gateway unavailable
```

The gateway asks search for the document detail, takes the version's `file_name`,
accepts it only as a plain name starting with `<file_uuid>_`, and serves it from
`UPLOAD_DIR`. The portal streams the gateway response unchanged.

### 14.11 Debug information for "try a question" (v1.8.0)

`POST /schools/{school}/ask-preview` body gains `"debug": bool` (default `true`).
With `debug` the portal sends `debug: true` to search `/v1/ask` and, in parallel,
the same identity to search `POST /v1/explain` (deterministic, no LLM). The response
gains, passed through unchanged:

| Field | From | Content |
|---|---|---|
| `effective_scope` | ask | `legal_entity, role, school_grade, language, domains, identified` |
| `strategies`, `key_words_used`, `language`, `answer_parts` | ask | as the search response schema |
| `trace` | ask (`debug`) | per step: `step, status (ran/skipped/disabled/error/unknown), ms, pool, pool_added, added_chunk_ids, matched_entry_id, resolved, outcome, decision, match_score, stopped_here, note, detail` |
| `explain` | explain | `query_norm_tokens, query_lemma_tokens, query_stem_tokens, query_language, query_in_corpus, query_absent_concepts, pack_ids, offer_pack_ids, resolved, trace_detail` |

`explain` is `null` when the explain call fails (the answer is still returned); the
portal never stores or logs any of it. Search returns no ranked list of QA candidates:
the panel shows the winner (`matched_entry_id`, score), the n-gram chip
(`trace[qa_lexical].detail.ngram_chip`) and `suggestions`.

```
search  GET /internal/qa-admin/entries/by-id?legal_entity=&entry_id=
portal  GET /schools/{school}/entries/by-id?entry_id=                (policy: read)
200     {"pack_id": str, "entry_key": str, "canonical_q": str, "is_active": bool}
404     foreign/unknown · 400 entry_id not a UUID
```

### 14.12 School overview and "expiring soon" (v1.8.0)

```
search  GET /internal/qa-admin/documents/stats?legal_entity=
portal  GET /schools/{school}/documents/stats                        (policy: read)
200     {"active": n, "inactive": n, "expired": n, "expiring_30": n, "future": n,
         "qa_entries_active": n}
```

Counts are per DOCUMENT (shown version, §14.1). `expiring_30` = active, current, with
`valid_to` between today and today + 30 days. The list filter `validity` gains
`expiring` with the same meaning.

### 14.13 Document content (chunks) (v1.8.0)

```
search  GET /internal/qa-admin/documents/{file_uuid}/chunks?legal_entity=&limit=&cursor=
portal  GET /schools/{school}/documents/{file_uuid}/chunks?limit=&cursor=   (policy: read)
200     {"chunks": [{"chunk_index": int, "heading_path": str|null,
                      "section_heading": str|null, "text": str, "char_count": int}],
         "total": int, "next_cursor": str|null}
```

`file_uuid` is the VERSION; order `chunk_index`; `limit` 1–100 (default 20); the cursor
is an offset. Scoped by `legal_entity_code` like every document route.

### 14.14 Unchanged content and refresh from the web (v1.8.0)

- Migration 228 adds `document_metadata.content_sha256 text` (nullable). Approved by
  the owner on 2026-11-14. Summaries and versions expose `content_sha256`.
- `PATCH /internal/qa-admin/documents/{file_uuid}/content-hash?legal_entity=` body
  `{"content_sha256": "<64 lowercase hex>"}` sets it on that ONE version (422 on a bad
  value, 404 foreign). Search role already has UPDATE on `document_metadata` (226).
- The hash is the SHA-256 of the uploaded file's bytes, or for a web page of its
  cleaned text (`clean_text` of the page's visible text), so dates in markup do not
  count. The gateway reports it per file in `/v1/ingest` results (`content_sha256`),
  also in a dry run.
- **Recording:** the portal stores the hash through the PATCH when a job's
  `stored_chunks` first becomes positive (the metadata row exists only then). Ingests
  outside the portal leave it NULL; a NULL hash never matches.
- **Skipping:** a FILE job, or a URL job without level-1 links, whose hash equals the
  `content_sha256` of the current ACTIVE version with at least one stored chunk of the same document (same source key),
  and whose declared metadata match that version (visibility, roles, validity, grades,
  subject; type, language and domains only when the job sets them), ends
  as `status: "skipped"` with `result: {"reason": "unchanged", "file_uuid": <current>}`
  and no ingest call. For a URL the portal first runs the gateway dry run to obtain
  the hash. Crawls with level-1 links are never skipped.
- **Refresh:** `POST /schools/{school}/documents/{file_uuid}/refresh` (ingest, csrf) on
  a web document enqueues a URL job with the shown version's metadata and no level-1
  links → `202 {"job": DocumentJob}`; a file document → 422.
- `DocumentJob.status` gains `skipped`.

---

## 11. Out of scope

AI generation of answers, an approval workflow, XLSX, merging duplicates, broad
taxonomy editing, permanent server-side drafts, item version history, transfer of
packs between schools, category backfill (stage 2), switching `category_siblings`
to `category_id` (stage 2), a shared lock for multiple replicas.

---

## 12. Mandatory checks before completion

`make docs-check`; frontend build and unit tests; policy, scope and domain tests;
CSV/ZIP round trip including diacritics, Markdown, quotes, semicolons, sources,
domains and `category_code`; an incomplete import writes nothing; `merge` does not deactivate
omitted rows; a stale etag does not let a write through; preservation of `entry_key`/`entry_id`,
owner and metadata; integrity of categories and translations; `tests/eval_rag.py --repeat 3`
before and after (including related questions); a normalization parity check if normalization
would be touched (otherwise normalization is **not changed** because of the portal);
browser flow per §10.

## Version and schema coverage note (documentation audit 2026-10-12)

The document revision, runtime `qa-portal/1.3.0` context/export identifier,
OpenAPI snapshot revision and frontend package/type revision are separate labels.
They should not be assumed to describe identical coverage. The committed OpenAPI
files do not yet enumerate every implemented delete route; consult the current
router models/tests before generating a client. This audit records the gap and
does not change wire identifiers or implement new API behaviour.
