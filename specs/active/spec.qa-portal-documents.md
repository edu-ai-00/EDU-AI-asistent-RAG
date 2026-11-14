---
id: spec.qa-portal-documents
status: active
components: [api, ingestion, db, contracts]
sources:
  - api/app/main.py
  - search/app/qa_admin.py
  - search/app/server.py
  - search/app/nodes/rag_vector.py
  - search/app/nodes/rag_keywords.py
  - qa_portal/backend/app/policy.py
  - qa_portal/backend/app/api/deps.py
  - qa_portal/backend/app/api/security.py
  - qa_portal/backend/app/api/context.py
  - qa_portal/backend/app/api/operations.py
  - qa_portal/backend/app/adapters/ingest.py
  - qa_portal/backend/app/services/document_queue.py
  - qa_portal/backend/app/services/document_service.py
  - qa_portal/backend/app/services/publish_service.py
  - qa_portal/backend/app/state/store.py
  - qa_portal/frontend/src/App.tsx
  - qa_portal/frontend/src/components/PackMetadataFields.tsx
  - contracts/qa-portal/CONTRACT.md
  - nginx/conf/nginx.conf
  - docs/CLEAN_INSTALL.md
  - tools/upload_test.html
  - langflow_flows/ingestion/EDU_AI_LOAD_MARKDOWN.json
  - postgres/init_clean/01_schema.sql
  - postgres/migrations/226_search_document_admin.sql
  - postgres/migrations/227_search_document_validity.sql
  - postgres/migrations/228_document_content_sha256.sql
last_verified: 2026-11-14
---

# QA portal — document management and school access

Written 2026-11-13 against `main` at `bc33e1b`. It extends the QA portal
(CONTRACT.md v1, qa-portal/1.4.0) with a second page, **Documents**, and adds a
delete endpoint to the ingest gateway. Stage 2 describes sign-in for school staff.

**Status (2026-11-14).** Stage 1 is implemented as contract `qa-portal/1.8.0`;
CONTRACT.md §14 is authoritative for every route and shape, this page keeps the
design rationale. During implementation the scope grew, each step approved:
validity editing, list filters, a school overview, the document content (chunks),
stored-file downloads, "try a question" with a debug view, and skipping unchanged
content. That needed migrations 226–228 (the search role may update and delete
document rows; `document_metadata.content_sha256`). The portal lives at
`/knowledge/` only; the former `/qa/` address is gone. Stage 2 is not started.

## 0. Accepted decisions

| # | Decision |
|---|---|
| D1 | The delete endpoint lives in the ingest gateway: `DELETE /v1/documents/{file_uuid}`. It deletes the rows (through search) and the files in `/data/uploads`. |
| D2 | "Delete" removes the **document with all its versions** (same source, same school). |
| D3 | The portal ingests under the technical account `qa_portal__{school}` (CONTRACT §2.2). Visibility is `entity` or `organization` only. |
| D4 | Besides permanent deletion there is **deactivation and reactivation**. |
| D5 | Bulk upload (several files or several URLs at once) is in scope. |
| D6 | The portal is prepared for school staff (see their own school only) and a central administrator (sees every school) — stage 2. |

## 1. Scope

**In scope, stage 1:** the school's document list; uploading one or many files;
ingesting one or many web pages, optionally with their level-1 links; document
metadata; deactivation, reactivation and permanent deletion; the gateway delete
endpoint.

**In scope, stage 2:** sign-in, the roles "school staff" and "central administrator".

**Out of scope:** editing metadata other than the validity dates (the metadata
is copied onto every chunk, so a change is a new ingest; validity is the one
exception, CONTRACT §14.8); private documents; the `taxonomy_binding` picker of
`upload_test.html`; uploading QA CSV through this page (packs have their own
path); archiving deleted content (the database backup is the backup, as in CONTRACT §13.2).

## 2. Terms

- **Version** = one `file_uuid`: a `document_metadata` row plus the
  `document_chunks`, `document_elements` and `document_tables` rows with the same
  `file_uuid`. The four tables have no foreign keys between them.
- **Document** = the versions of one school that share a source. The key is
  exactly the one the ingest flow uses to retire older versions
  (`PostgresInsert` in `EDU_AI_LOAD_MARKDOWN.json`):
  - `source_url` + `legal_entity_code` when `source_url` is set;
  - otherwise `original_file_name` + `user_name` + `legal_entity_code`.
- **Active version** = `is_active = true`. Re-ingesting a source activates the
  new version and deactivates the older ones. Every retrieval step
  (`rag_vector`, `rag_keywords`, `document_lexical`, `document_siblings`) joins
  on `m.is_active = true`, so a deactivated document leaves search at once.
- **Run** = one `processing_uuid`. A web page ingested with its level-1 links
  produces the parent and its child pages in one run. Each child is a
  **separate document** (it has its own `source_url`) and is managed on its own.

## 3. Architecture

```
browser ──► nginx ──► edu_ai_qa_admin (portal, no database)
                        ├─ reads, (de)activation ─► edu_ai_search /internal/qa-admin/documents*
                        ├─ ingest (one item) ─────► edu_ai_api    POST   /v1/ingest   (unchanged)
                        └─ delete ────────────────► edu_ai_api    DELETE /v1/documents/{id}
                                                       ├─► edu_ai_search DELETE /internal/qa-admin/documents
                                                       └─► unlink /data/uploads/{file_uuid}_*
```

The boundaries stay: the portal has neither database credentials nor the
`uploads` volume; only search touches the database; only the gateway (and
Langflow) touch `/data/uploads`.

## 4. Search — internal API (`search/app/qa_admin.py`)

Same router, same helpers (`_cursor`, `_error`), same rules as QA deletion
(CONTRACT §13.2): one transaction per request, `legal_entity_code` in **every**
`WHERE`, a foreign document is `404`.

| Method | Path | Meaning |
|---|---|---|
| `GET` | `/internal/qa-admin/documents?legal_entity=&q=&status=active\|inactive\|all&cursor=&limit=` | Document list, one row per document showing its newest version. Fields: `file_uuid`, `title` (`source_url`, else `original_file_name`), `source_kind` (`web`/`file`), type, language, subject, grades, domains, roles, visibility, `valid_from/to`, `user_name`, `ldts_gmt`, `is_active`, `version_count`, `chunk_count`. `q` matches title/URL (ILIKE). |
| `GET` | `/internal/qa-admin/documents/{file_uuid}?legal_entity=` | One document with the list of its versions (for the confirmation dialog). |
| `POST` | `/internal/qa-admin/documents/{file_uuid}/deactivate?legal_entity=` | Deactivates the document's **active version**. Idempotent. |
| `POST` | `/internal/qa-admin/documents/{file_uuid}/activate?legal_entity=` | Activates the document's **newest** version, never an older one (it was superseded). Idempotent. |
| `DELETE` | `/internal/qa-admin/documents?legal_entity=&file_uuid=` | Permanently deletes **every version**: `document_chunks`, `document_tables`, `document_elements`, `document_metadata`. Returns `{"deleted": {"versions", "chunks", "elements", "tables"}, "file_uuids": [...]}`. |

Not deleted: `ingestion_log`, `question_answer_log`, code lists — the audit
stays, as in §13.1.

Deactivation changes only `document_metadata.is_active`. Chunks have no flag of
their own; retrieval filters them through the join (§2).

## 5. Ingest gateway — `DELETE /v1/documents/{file_uuid}` (`api/app/main.py`)

```
DELETE /v1/documents/{file_uuid}?legal_entity=<code>
200 {"deleted": {...}, "files_removed": n}
400 invalid UUID · 404 foreign or unknown · 503 search unavailable
```

- `file_uuid` is parsed with `uuid.UUID`; nothing from the request reaches a path.
- The database goes first (search, one transaction). Only after it succeeds are
  the files `UPLOAD_DIR/{uuid}_*` removed for **every** returned `file_uuid`
  (the glob is anchored on the canonical UUID; whether Langflow writes derived
  files elsewhere is checked during implementation). A failed unlink returns
  `200` with a warning in the body and the log: the database is the source of
  truth, an orphaned file is harmless, orphaned chunks are not.
- Same exposure as `/v1/ingest`: port 80 is VPN-only and has **no sign-in**
  (Basic auth was removed on 2026-09-02). Any WireGuard peer can therefore delete,
  just as it can ingest today. School staff (stage 2) never reach `/api/`; the
  portal calls the gateway inside the Docker network.
- Log: `legal_entity`, `file_uuid`, counts. Never a title or content.

## 6. Portal — API (`/qa-api/v1`)

Binding shapes: CONTRACT.md §14.4; this section is the overview. A new router `api/documents.py`. Every path with a school goes through the
policy (§9); every write through `csrf_guard`.

| Method | Path | Policy |
|---|---|---|
| `GET` | `/schools/{school}/documents?q=&status=&limit=&cursor=` | `may_read(school)` |
| `GET` | `/schools/{school}/documents/{file_uuid}` | `may_read(school)` |
| `GET` | `/schools/{school}/document-options?lang=` | `may_read(school)` |
| `POST` | `/schools/{school}/documents` (multipart, **one** item) → `202 {"job": DocumentJob}` | `may_ingest(school)` |
| `GET` | `/schools/{school}/document-jobs?batch_id=` | `may_read(school)` |
| `POST` | `/schools/{school}/documents/{file_uuid}/deactivate` · `/activate` | `may_deactivate(school)` |
| `DELETE` | `/schools/{school}/documents/{file_uuid}`, body `{"confirm_file_uuid": "<file_uuid>"}` | `may_delete_permanently(school)` |

`document-options` extends the existing `metadata-options` (roles, grades) with
document types, languages, subjects, the school's domains and the
organisation's validity periods — all from existing search `/v1/*` endpoints.

### 6.1 One upload item

Multipart: `file` **or** `url` (+ `save_level_1_child`, `max_child_links` ≤ 10),
`batch_id` (a client UUID, grouping only), and the metadata:

| Field | Required | Note |
|---|---|---|
| `visibility` | yes | `entity` / `organization` (D3) |
| `audience_roles` | yes, ≥ 1 | |
| `document_type`, `document_language` | no | empty = the LLM decides |
| `school_subject`, `school_grade_codes` | no | empty stays empty (the LLM does not set them) |
| `domain_codes` | no | overrides the LLM classification |
| `valid_from`, `valid_to` | no | an organisation period or custom dates |

`legal_entity` is the school from the path and `user_id` the technical uploader
from search (`/internal/qa-admin/uploader`); the client cannot send either. The
gateway validates against the code lists; its `400` becomes the item's error.

The portal refuses `.csv` (QA packs have their own path). Allowed extensions =
`.pdf`, `.docx`, `.pptx`, `.xlsx`: the gateway also lists `.txt` but has no
extraction flow for it, so such an upload always fails (VM 507, 2026-11-14).
At most 50 MB per file (the nginx limit).

## 7. Ingest queue (single and bulk upload)

Bulk upload has **no** endpoint of its own: the client posts every item
separately with the same `batch_id`. Single and bulk upload share one path, and
the 50 MB limit applies per file, not per batch.

- The portal stores the item in its state volume (`jobs/{job_id}.json` plus the
  file under `jobs/files/`), answers `202` and queues it.
- One worker thread, started in `lifespan`, takes items FIFO and calls the
  synchronous `/v1/ingest` — **concurrency 1** (`QA_PORTAL_INGEST_CONCURRENCY=1`)
  so a batch overwhelms neither Langflow nor the LLM budget. A finished item
  keeps its `processing_uuid` and the gateway result, and its temporary file is
  removed.
- Item states: `queued → processing → completed | failed | unknown`. After
  completion the UI reads the chunk count from `ingest-status` by the **known**
  `processing_uuid` (the markdown flow is asynchronous and chunks are the only
  reliable "stored" signal — never `since_seconds`, CONTRACT §6).
- Restart: `queued` continues (nothing was sent yet); `processing` becomes
  `unknown` by the same rule as `reconcile_operations_on_startup`.
- Limits: 100 items per batch, 300 queued items per school (`429` above it).
  Finished records are pruned after 7 days.
- The UI refuses two items of one batch with the same source (file name or URL):
  the second would supersede the first immediately.

## 8. Frontend

- **Navigation:** a header switch "QA packs | Documents" with real paths
  `/knowledge/qa` and `/knowledge/documents` (CONTRACT §14.8, no new dependency).
  Both pages share the school selector.
- **`DocumentsPage`:** a table (title/URL, type, language, visibility, roles,
  validity, uploaded, chunks, versions, state), search, an Active / Deactivated /
  All filter, row actions Deactivate / Activate / Delete.
- **`DocumentUploadDialog`** (`Dialog.tsx`) with two tabs:
  - *Files* — pick or drop **one or more** files;
  - *Web pages* — one or more URLs (one per line), "also ingest level-1 links"
    and their count (≤ 10).

  Metadata is shared by the whole batch: `PackMetadataFields` (visibility, roles,
  grades) plus a new `DocumentMetadataFields` (type, language, subject, domains,
  validity).
- **Batch progress:** a table of items with their state from `document-jobs`,
  polled every 5 s while anything is `queued` or `processing`. Closing the dialog
  does not stop the batch; items the browser had not sent yet are listed.
- **Delete:** `DeleteDialog` shows the school, title, number of versions and
  chunks, and states that the action is irreversible (deactivation is the
  reversible alternative).
- The mock client (`mockClient.ts`) and tests cover every new screen.

## 9. Policy, ready for sign-in (stage 1)

`Policy` gets per-school methods:

```python
def may_read(self, p, school) -> bool: ...
def may_ingest(self, p, school) -> bool: ...
def may_deactivate(self, p, school) -> bool: ...
def may_delete_permanently(self, p, school) -> bool: ...
```

`VpnAdminPolicy` answers `True` everywhere (today's behaviour). Also in stage 1:

- every stored record (preview, operation, export, job) carries `school` and
  `principal_subject`; `GET /operations/{id}` and exports check the policy for
  the record's school (today they do not — harmless with one administrator, not
  with many schools);
- `GET /context` returns the real permissions from the policy (today
  `may_publish` is hard-coded `True`) and the UI hides what is not allowed.

Stage 2 is then reduced to swapping `get_principal` and `get_policy`.

## 10. Stage 2 — sign-in for school staff

### 10.1 Principle

No passwords are stored and no sign-in is written here. **Cloudflare Access**
verifies identity (the tunnel and Access are already in use, see
`docs/CLEAN_INSTALL.md`): a one-time e-mail code, or the school's Microsoft 365 /
Google, with MFA per identity provider. The portal receives a signed JWT and
takes only the e-mail from it.

### 10.2 Path to the portal

- A new portal hostname → the Cloudflare tunnel → a **new nginx listener**
  (e.g. 82) that serves **only** `/knowledge/` and `/qa-api/v1/` — the same pattern as
  listener 81, which serves only `/search/`. Listeners 80 (VPN) and 81 (search)
  do not change.
- An Access application with a policy "allowed e-mails / school domains".
  Whoever fails it never reaches nginx.
- With `QA_PORTAL_AUTH=cf_access`, `/knowledge/` is removed from listener 80: **one**
  path and one identity rule, the central administrator included.

### 10.3 Verification in the portal

On every request `get_principal`:

1. Reads `Cf-Access-Jwt-Assertion` (the header, not the cookie). Missing → `401`.
2. Verifies the signature against the team JWKS (cached, refreshed on an unknown
   `kid`), `aud` = the application tag, `iss`, `exp`. Failure → `401`.
3. The e-mail (lowercased):
   - listed in `QA_PORTAL_CENTRAL_ADMINS` → `Principal(kind="central_admin", schools=ALL)`;
   - otherwise `GET /internal/qa-admin/portal-principal?email=` → the active
     `staff_user` rows of active entities → `Principal(kind="school_staff",
     subject=user_id, schools={…})`. No row → `403`.

   Not cached (a deactivated user loses access at once), like `ingest-principal`.

Identity never comes from the body, the query or a client header. Cloudflare
signs the JWT and the portal verifies it itself — no "trust the proxy header".

### 10.4 Permissions

| Action | School staff | Central administrator |
|---|---|---|
| read QA and documents | own schools | all |
| publish QA, import, export | own schools | all |
| upload documents, (de)activate | own schools | all |
| permanently delete a pack or document | **no** | yes |
| global category catalog | **no** | yes |

A foreign school is always `403`, even when the client sends it in the path,
the body or a ZIP manifest (the mandatory test of CONTRACT §2.1 extends to the
new routes).

### 10.5 Provenance

School staff ingest under **their own** `staff_user.user_id`, so the real author
is on the document and on QA entries. The central administrator keeps the
school's technical account. The CSRF/origin guard stays and matters more with
the Cloudflare cookie. Logs carry `user_id` and the action, never an e-mail or
content.

### 10.6 Approvals stage 2 needs

- A **new production dependency** `PyJWT[crypto]` (JWT verification) —
  AGENTS.md "ask first".
- Cloudflare Access and DNS configuration (outside the repository).
- **No schema change**: `staff_user.email` exists; central administrators live
  in configuration.

## 11. Tests

- **search:** list (school scope, status filter, version grouping),
  deactivate/activate (newest version only, idempotent), delete (every version,
  a foreign school is 404 with nothing deleted, rollback on a failure midway).
- **gateway:** invalid UUID, 404, unlink only with the canonical prefix, a
  database failure deletes no file.
- **portal backend:** the policy on every new route (a restricted policy → 403
  for a foreign school), CSRF on writes, the queue (FIFO, concurrency 1, restart
  of `queued`/`processing`, limits 100/300), `.csv` refused, delete confirmation.
- **frontend:** the page, the dialog for one and many files/URLs, batch
  progress, (de)activation, deletion, dialog accessibility.
- **live (dev stack):** upload → chunks > 0 → `/v1/ask` cites the document →
  deactivate → the document is gone from answers → activate → delete → zero rows
  in the four tables and the file gone from `uploads`.
- **stage 2:** valid / invalid / expired JWT, wrong `aud`, unknown e-mail,
  deactivated staff user, the permission matrix of §10.4.

## 12. Implementation order

1. Search: the internal document endpoints and their tests. *Done.*
2. Gateway: `DELETE /v1/documents/{file_uuid}` and its tests. *Done.*
3. Portal backend: policy methods, adapters, router, queue, tests. *Done.*
4. Frontend: navigation, page, dialogs, mock, tests. *Done.*
5. Contracts: `CONTRACT.md` (§14 Documents), `openapi.yaml`,
   `internal-qa-admin.yaml`; the live test on the dev stack; `make docs-check`.
   *Done; live tests on VM 507 with production-like data.*
6. Stage 2 on its own, after the dependency and Cloudflare are approved.
