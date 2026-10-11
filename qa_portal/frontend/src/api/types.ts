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

export type School = {
  code: string;
  name: string;
  kind?: string;
  level?: string | null;
  parent_code?: string | null;
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
