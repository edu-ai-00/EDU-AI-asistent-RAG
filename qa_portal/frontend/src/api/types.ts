// TS mirror of contracts/qa-portal/openapi.yaml (qa-portal/1.0.0). Shapes
// only — this worker may not change the contract, so keep this file a
// faithful copy of the schemas, not an interpretation of them.

export type Capabilities = {
  may_publish?: boolean;
  may_replace?: boolean;
  may_manage_categories?: boolean;
};

export type Context = {
  mode: "vpn_admin";
  contract_version: string;
  portal_version?: string;
  capabilities: Capabilities;
  ui_default_lang?: string;
};

/** CONTRACT.md §14.6: per-school permissions from the policy. */
export type SchoolPermissions = {
  read: boolean;
  publish: boolean;
  ingest: boolean;
  deactivate: boolean;
  delete_permanently: boolean;
};

export type School = {
  code: string;
  name: string;
  kind?: string;
  level?: string | null;
  parent_code?: string | null;
  /** Absent on an older backend: the UI then treats everything as allowed. */
  permissions?: SchoolPermissions;
};

export type Domain = {
  code: string;
  label: string;
  label_fallback?: boolean;
  sort_order?: number;
};

export type Category = {
  code: string;
  label: string;
  label_fallback?: boolean;
  labels?: Record<string, string>;
  is_active: boolean;
  sort_order?: number;
  notes?: string | null;
};

export type Pack = {
  pack_id: string;
  version: number;
  pack_etag: string;
  lang?: string;
  built_at?: string | null;
  entry_count: number;
  metadata_uniform?: boolean;
  publishable?: boolean;
};

export type CodeOption = { code: string; label: string; label_fallback?: boolean };

/** Confirmed metadata for a pack that does not exist server-side yet
 *  (CONTRACT.md §2.4a). Carried in POST .../preview as `pack_metadata`;
 *  the portal never invents these from a form default. */
export type PackMetadata = {
  visibility: "entity" | "organization";
  audience_roles: string[];
  school_grade_codes: string[];
  valid_from?: string | null;
  valid_to?: string | null;
};

/** GET /schools/{school}/metadata-options response (CONTRACT.md §2.4a).
 *  visibility is a fixed frontend token pair, not a code_list. */
export type MetadataOptions = {
  visibility: string[];
  audience_roles: CodeOption[];
  school_grades: CodeOption[];
};

export type Source = { title: string; url: string };

export type Entry = {
  entry_key: string;
  entry_id?: string;
  canonical_q: string;
  answer_md: string;
  variants?: string[];
  sources?: Source[];
  domains: string[];
  category_code?: string | null;
  category_label?: string | null;
  category_is_active?: boolean | null;
  cat_legacy?: string | null;
  is_active: boolean;
  owner_user_id?: string | null;
  visibility?: string;
  audience_roles?: string[] | null;
  school_grade_ids?: number[] | null;
  school_subject_ids?: number[] | null;
  valid_from?: string | null;
  valid_to?: string | null;
};

/** The writable subset (openapi EntryWrite). domains/category_code are
 *  ALWAYS present and explicit — never omitted (CONTRACT.md §5.1/§10). */
export type EntryWrite = {
  entry_key: string | null;
  canonical_q: string;
  answer_md: string;
  variants: string[];
  sources: Source[];
  domains: string[];
  category_code: string | null;
  is_active: boolean;
};

export type EntryPage = {
  entries: Entry[];
  next_cursor?: string | null;
  total?: number;
  pack_etag: string;
  pack_version: number;
  metadata_uniform?: boolean;
};

export type DiffField = { field: string; old: unknown; new: unknown };

export type Diff = {
  added: Array<{ entry_key: string; canonical_q: string }>;
  changed: Array<{ entry_key: string; fields: DiffField[] }>;
  unchanged: string[];
  deactivated: Array<{ entry_key: string; canonical_q: string }>;
};

export type Preview = {
  preview_id: string;
  base_etag: string;
  diff: Diff;
  warnings?: Array<{ code: string; message: string }>;
  expires_at: string;
};

export type ImportError = {
  file?: string;
  row?: number;
  field?: string;
  code?: string;
  message?: string;
};

export type ImportPackResult = {
  pack_id: string;
  file: string;
  base_etag: string;
  is_new_pack?: boolean;
  /** Data rows read from this file. Tells "empty file" apart from "file that
   *  changes nothing" — both otherwise render as an all-zero diff. Optional
   *  because a preview stored before this field existed will not carry it. */
  row_count?: number;
  diff: Diff;
  ok: boolean;
  errors?: ImportError[];
  legacy_cat_unresolved?: Array<{ entry_key: string; cat: string }>;
  /** CONTRACT.md §2.4c: a new pack (is_new_pack) with no confirmed metadata
   *  yet. The preview itself is still 200 so the diff stays visible, but
   *  publish will reject this pack until the admin confirms metadata and
   *  re-runs the preview with pack_metadata_by_pack. */
  metadata_required?: boolean;
  /** ok && !metadata_required — mirrors the backend's own derived flag so
   *  the client never re-derives publishability with slightly different logic. */
  publishable?: boolean;
  /** Echoed from a portal-exported ZIP manifest for a new pack — a
   *  suggestion to prefill the confirmation form, never itself a
   *  confirmation (CONTRACT.md §2.4c). */
  suggested_metadata?: PackMetadata;
};

export type ImportPreview = {
  preview_id: string;
  source_kind: "csv" | "zip";
  mode?: "merge" | "replace";
  packs: ImportPackResult[];
  expires_at: string;
};

export type OperationStatus = "validating" | "accepted" | "processing" | "completed" | "failed" | "unknown";

export type ApiErrorShape = {
  code: string;
  message: string;
  retryable: boolean;
  fields?: Array<{ field: string; message: string; file?: string; row?: number }>;
};

export type ErrorEnvelope = { error: ApiErrorShape };

export type Operation = {
  operation_id: string;
  status: OperationStatus;
  school: string;
  pack_id?: string | null;
  preview_id?: string | null;
  processing_uuid?: string | null;
  started_at?: string;
  finished_at?: string | null;
  result_etag?: string | null;
  packs?: Array<{ pack_id: string; status: "pending" | "ok" | "failed" | "skipped"; message?: string }>;
  summary?: { added?: number; updated?: number; deactivated?: number; untouched?: number };
  /** Flat, NOT the HTTP envelope — see openapi.yaml Operation.error. */
  error?: ApiErrorShape;
};

/** CONTRACT.md §13.3: returned by both permanent-delete endpoints. Counts
 *  are informational only — the UI never re-derives them, it shows what the
 *  server actually removed. */
export type DeleteResult = {
  deleted: {
    entries: number;
    variants: number;
    embeddings: number;
    manifests: number;
  };
};

export type ExportResult = {
  export_id: string;
  format: "csv" | "zip";
  download_url: string;
  filename?: string;
  bytes?: number;
  expires_at: string;
  note?: string;
};

// ---- Documents (CONTRACT.md §14) ----

export type DocumentStatusFilter = "active" | "inactive" | "all";

/** §14.8: computed by search against CURRENT_DATE. */
export type ValidityState = "current" | "expired" | "future";
/** §14.12: `expiring` = active, current, valid_to within the next 30 days. */
export type DocumentValidityFilter = ValidityState | "expiring" | "all";
/** §14.10 list filter `format`. */
export type DocumentFormatFilter = "pdf" | "docx" | "pptx" | "xlsx" | "web";

/** §14.8 list filters, all optional and combined with AND. */
export type DocumentListParams = {
  status?: DocumentStatusFilter;
  q?: string;
  type?: string;
  role?: string;
  validity?: DocumentValidityFilter;
  format?: DocumentFormatFilter;
  source?: string;
  limit?: number;
  cursor?: string;
};

/** §14.2: one document = the versions of one school sharing a source key. */
export type DocumentSummary = {
  file_uuid: string;
  title: string;
  source_kind: "web" | "file";
  source_url: string | null;
  original_file_name: string | null;
  document_type: string | null;
  document_language: string | null;
  school_subject: string | null;
  school_grade_codes: string[];
  domain_codes: string[];
  audience_roles: string[];
  visibility: "private" | "entity" | "organization";
  valid_from: string | null;
  valid_to: string | null;
  user_name: string | null;
  ingested_at: string;
  is_active: boolean;
  /** Absent on an older backend: treated as "current". */
  validity_state?: ValidityState;
  version_count: number;
  chunk_count: number;
  /** §14.10: extension in lower case, "web" for a saved page, null/absent when unknown. */
  file_format?: string | null;
  /** §14.14: SHA-256 of the content (hex); null/absent when the ingest did not come from the portal. */
  content_sha256?: string | null;
};

export type DocumentVersion = {
  file_uuid: string;
  processing_uuid: string | null;
  ingested_at: string;
  is_active: boolean;
  chunk_count: number;
  file_format?: string | null;
  /** §14.14 */
  content_sha256?: string | null;
};

export type DocumentPage = { documents: DocumentSummary[]; next_cursor: string | null };

export type DocumentDetail = { document: DocumentSummary; versions: DocumentVersion[] };

/** §14.3: permanent delete of every version, as returned by the portal. */
export type DocumentDeleteResult = {
  deleted: { versions: number; chunks: number; elements: number; tables: number };
  file_uuids: string[];
  files_removed: number;
  warnings: string[];
};

export type ValidityPeriod = {
  id: string | number;
  name: string;
  valid_from: string | null;
  valid_to: string | null;
  is_default: boolean;
};

/** §14.4 GET /schools/{school}/document-options. */
export type DocumentOptions = {
  visibility: string[];
  audience_roles: CodeOption[];
  school_grades: CodeOption[];
  school_subjects: CodeOption[];
  languages: CodeOption[];
  document_types: CodeOption[];
  domains: Domain[];
  validity_periods: ValidityPeriod[];
  file_extensions: string[];
  max_file_bytes: number;
  max_batch_items: number;
  max_child_links: number;
};

/** §14.4: sent as the JSON string field `metadata` of one upload item. */
export type DocumentMetadata = {
  visibility: "entity" | "organization";
  audience_roles: string[];
  document_type: string | null;
  document_language: string | null;
  school_subject: string | null;
  school_grade_codes: string[];
  domain_codes: string[];
  valid_from: string | null;
  valid_to: string | null;
};

/** One upload item: exactly one of `file` / `url`. */
export type DocumentUploadItem = {
  file?: File;
  url?: string;
  save_level_1_child?: boolean;
  max_child_links?: number;
  batch_id: string;
  metadata: DocumentMetadata;
};

/** §14.14: `skipped` = the content did not change, nothing was ingested. */
export type DocumentJobStatus = "queued" | "processing" | "completed" | "failed" | "skipped" | "unknown";

export type DocumentJob = {
  job_id: string;
  batch_id: string;
  school: string;
  source_kind: "file" | "url";
  source: string;
  status: DocumentJobStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  processing_uuid: string | null;
  result: {
    status?: string;
    total_files?: number;
    ok_count?: number;
    error_count?: number;
    /** §14.14: "unchanged" on a skipped job, with the file_uuid of the current version. */
    reason?: string;
    file_uuid?: string;
  } | null;
  stored_chunks: number | null;
  error: { code: string; message: string } | null;
};

/** §14.8 PATCH .../validity body. */
export type DocumentValidityBody = { valid_from: string | null; valid_to: string | null };

/** §14.8 check-sources. `value` is the trimmed URL or the file name. */
export type SourceCheckItem = { kind: "file" | "url"; value: string };
export type SourceCheckResult = {
  kind: "file" | "url";
  value: string;
  existing: DocumentSummary | null;
  supersedes: boolean;
};

/** §14.8 preview-url. */
export type UrlPreviewBody = {
  url: string;
  save_level_1_child: boolean;
  max_child_links: number;
  metadata: DocumentMetadata;
};
export type UrlPreviewFile = {
  source_url: string | null;
  source_type: string;
  page_role: "parent" | "child";
};

/** CONTRACT.md §14.12 GET .../documents/stats: counts per DOCUMENT (shown version). */
export type DocumentStats = {
  active: number;
  inactive: number;
  expired: number;
  expiring_30: number;
  future: number;
  qa_entries_active: number;
};

/** CONTRACT.md §14.13 GET .../documents/{file_uuid}/chunks (file_uuid = the VERSION). */
export type DocumentChunk = {
  chunk_index: number;
  heading_path: string | null;
  section_heading: string | null;
  text: string;
  char_count: number;
};
export type DocumentChunkPage = { chunks: DocumentChunk[]; total: number; next_cursor: string | null };

/** CONTRACT.md §14.11 GET .../entries/by-id. */
export type EntryLocation = { pack_id: string; entry_key: string; canonical_q: string; is_active: boolean };

/** CONTRACT.md §14.9: try a question. `user_school_grade` null = not sent to search.
 *  `debug` (§14.11, default true on the server) adds trace and explain to the answer. */
export type AskPreviewBody = {
  question: string;
  user_role: string;
  user_school_grade: string | null;
  retrieval_only: boolean;
  debug?: boolean;
};

/** rag-source.schema.json: rank and title always, the rest optional. */
export type AskSource = {
  rank?: number;
  title?: string;
  url?: string | null;
  file_uuid?: string;
  original_file_name?: string | null;
  source_url?: string | null;
};

export type AskSuggestion = {
  kind?: string;
  question: string;
  entry_id?: string | null;
  score?: number | null;
};

/** §14.11 effective_scope: what the search actually ran with. */
export type AskEffectiveScope = {
  legal_entity?: string | null;
  role?: string | null;
  school_grade?: string | null;
  language?: string | null;
  domains?: string[] | null;
  identified?: boolean;
};

/** §14.11 trace row. Only a debugging surface: every field but `step` may be absent. */
export type AskTraceStep = {
  step: string;
  status?: string;
  ms?: number | null;
  pool?: number | null;
  pool_added?: number | null;
  matched_entry_id?: string | null;
  resolved?: boolean | null;
  outcome?: string | null;
  decision?: string | null;
  match_score?: number | null;
  stopped_here?: boolean | null;
  note?: string | null;
  detail?: Record<string, unknown> | null;
};

/** §14.11 explain (POST /v1/explain); null on the result when that call failed. */
export type AskExplain = {
  query_norm_tokens?: string[];
  query_lemma_tokens?: string[];
  query_stem_tokens?: string[];
  query_language?: string | null;
  query_in_corpus?: boolean | null;
  query_absent_concepts?: string[];
  pack_ids?: string[];
  offer_pack_ids?: string[];
};

export type AskAnswerPart = {
  index: number;
  lead_in?: string | null;
  text: string;
  markdown?: string;
  source_rank?: number | null;
  title?: string | null;
  url?: string | null;
  file_uuid?: string | null;
};

/** The search /v1/ask fields passed through; a key search did not send is null. */
export type AskPreviewResult = {
  answer: string | null;
  outcome: string | null;
  answer_source: string | null;
  delivered_by: string | null;
  sources: AskSource[];
  suggestions: AskSuggestion[];
  matched_entry_id: string | null;
  match_score: number | null;
  duration_ms: number | null;
  retrieval_only: boolean;
  /** §14.11, present with `debug`; an older backend sends none of these. */
  effective_scope?: AskEffectiveScope | null;
  strategies?: string[];
  key_words_used?: string[];
  language?: string | null;
  answer_parts?: AskAnswerPart[];
  trace?: AskTraceStep[] | null;
  explain?: AskExplain | null;
};

/** Thrown by both adapters on a non-2xx response, so callers never branch
 *  on transport (fetch vs mock) — only on `.code` (CONTRACT.md §7). */
export class ApiError extends Error {
  status: number;
  code: string;
  retryable: boolean;
  fields?: ApiErrorShape["fields"];

  constructor(status: number, shape: ApiErrorShape) {
    super(shape.message);
    this.name = "ApiError";
    this.status = status;
    this.code = shape.code;
    this.retryable = shape.retryable;
    this.fields = shape.fields;
  }
}
