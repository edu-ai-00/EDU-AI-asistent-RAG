import type {
  AskPreviewBody,
  AskPreviewResult,
  Category,
  Context,
  DeleteResult,
  DocumentDeleteResult,
  DocumentChunkPage,
  DocumentDetail,
  DocumentJob,
  DocumentOptions,
  DocumentListParams,
  DocumentPage,
  DocumentStats,
  DocumentSummary,
  DocumentUploadItem,
  DocumentValidityBody,
  Domain,
  Entry,
  EntryLocation,
  EntryPage,
  EntryWrite,
  ExportResult,
  ImportPreview,
  MetadataOptions,
  Operation,
  Pack,
  PackMetadata,
  Preview,
  School,
  SourceCheckItem,
  SourceCheckResult,
  UrlPreviewBody,
  UrlPreviewFile,
} from "./types";

/** Everything the UI needs from the backend, shaped exactly like
 *  contracts/qa-portal/openapi.yaml. Both the live adapter (liveClient.ts)
 *  and the fixture adapter (mockClient.ts) implement this — components and
 *  hooks depend only on this interface, never on which one is active. */
export interface ApiClient {
  getContext(signal?: AbortSignal): Promise<Context>;
  listSchools(signal?: AbortSignal): Promise<School[]>;
  listSchoolDomains(school: string, signal?: AbortSignal): Promise<Domain[]>;
  listCategories(
    params?: { lang?: string; include_inactive?: boolean },
    signal?: AbortSignal,
  ): Promise<Category[]>;
  createCategory(body: {
    code: string;
    labels: Record<string, string>;
    sort_order?: number;
    notes?: string;
  }): Promise<Category>;
  listPacks(school: string, signal?: AbortSignal): Promise<Pack[]>;
  createPack(
    school: string,
    body: { pack_id: string; lang: string; metadata?: PackMetadata },
  ): Promise<{ pack_id: string; available: boolean }>;
  /** CONTRACT.md §2.4a: code lists to offer in the new-pack dialog. */
  getMetadataOptions(school: string, lang?: string, signal?: AbortSignal): Promise<MetadataOptions>;
  listEntries(
    school: string,
    pack: string,
    params?: { limit?: number; cursor?: string; q?: string },
    signal?: AbortSignal,
  ): Promise<EntryPage>;
  getEntry(
    school: string,
    pack: string,
    entryKey: string,
    signal?: AbortSignal,
  ): Promise<{ entry: Entry; pack_etag: string; pack_version: number }>;
  previewPackChanges(
    school: string,
    pack: string,
    body: { base_etag: string; entries: EntryWrite[]; pack_metadata?: PackMetadata },
  ): Promise<Preview>;
  publishPackChanges(
    school: string,
    pack: string,
    body: { preview_id: string; expected_etag: string },
  ): Promise<Operation>;
  previewImport(
    school: string,
    file: File,
    opts: {
      pack_id?: string;
      mode?: "merge" | "replace";
      /** CONTRACT.md §2.4c: confirmed metadata for packs in this import that
       *  do not exist yet, keyed by pack_id so a ZIP's packs cannot swap
       *  metadata. Sent as a multipart field whose value is JSON.stringify(...). */
      pack_metadata_by_pack?: Record<string, PackMetadata>;
    },
  ): Promise<ImportPreview>;
  publishImport(
    school: string,
    previewId: string,
    body: { expected_etags: Record<string, string>; confirm_replace?: boolean },
  ): Promise<Operation>;
  getOperation(operationId: string, signal?: AbortSignal): Promise<Operation>;
  /** CONTRACT.md §13: permanent, no undo — entries, variants and embeddings
   *  for this one question are gone. */
  deleteEntry(school: string, pack: string, entryKey: string): Promise<DeleteResult>;
  /** CONTRACT.md §13.2/§13.3: the server rejects a mismatched
   *  `confirm_pack_id` with 409 DELETE_CONFIRMATION_MISMATCH — the client
   *  never pre-validates this beyond disabling the button on a mismatch. */
  deletePack(school: string, pack: string, body: { confirm_pack_id: string }): Promise<DeleteResult>;
  /** §14.11: which pack and entry_key an entry_id (e.g. a matched_entry_id) belongs to. */
  getEntryById(school: string, entryId: string, signal?: AbortSignal): Promise<EntryLocation>;
  createExport(
    school: string,
    body: { pack_ids: string[]; format?: "csv" | "zip"; lang?: string },
  ): Promise<ExportResult>;
  downloadExportUrl(exportId: string): string;

  // ---- Documents (CONTRACT.md §14.4) ----
  listDocuments(
    school: string,
    params?: DocumentListParams,
    signal?: AbortSignal,
  ): Promise<DocumentPage>;
  getDocument(school: string, fileUuid: string, signal?: AbortSignal): Promise<DocumentDetail>;
  getDocumentOptions(school: string, lang?: string, signal?: AbortSignal): Promise<DocumentOptions>;
  /** One item per call (202); a batch is several calls sharing `batch_id`. */
  uploadDocument(school: string, item: DocumentUploadItem): Promise<DocumentJob>;
  listDocumentJobs(school: string, params?: { batch_id?: string }, signal?: AbortSignal): Promise<DocumentJob[]>;
  deactivateDocument(school: string, fileUuid: string): Promise<DocumentSummary>;
  activateDocument(school: string, fileUuid: string): Promise<DocumentSummary>;
  /** §14.8: the only metadata that changes without a new ingest. */
  updateDocumentValidity(school: string, fileUuid: string, body: DocumentValidityBody): Promise<DocumentSummary>;
  /** §14.8: what an upload of these sources would retire or duplicate. */
  checkDocumentSources(school: string, sources: SourceCheckItem[], signal?: AbortSignal): Promise<SourceCheckResult[]>;
  /** §14.8: pages a web ingest would fetch (dry run, nothing stored). */
  previewDocumentUrl(school: string, body: UrlPreviewBody, signal?: AbortSignal): Promise<UrlPreviewFile[]>;
  /** §14.10: plain URL of the download route (works as `<a href download>`). */
  documentDownloadUrl(school: string, fileUuid: string): string;
  /** §14.10: one version's stored file as a blob; a missing file is an ApiError 404. */
  downloadDocument(school: string, fileUuid: string): Promise<{ blob: Blob; filename: string | null }>;
  /** §14.12: counts for the overview tiles. */
  getDocumentStats(school: string, signal?: AbortSignal): Promise<DocumentStats>;
  /** §14.13: the chunks of one VERSION; `cursor` is the `next_cursor` of the previous page. */
  listDocumentChunks(
    school: string,
    fileUuid: string,
    params?: { limit?: number; cursor?: string },
    signal?: AbortSignal,
  ): Promise<DocumentChunkPage>;
  /** §14.14: enqueues a URL job for a web document (202); a file document is a 422. */
  refreshDocument(school: string, fileUuid: string): Promise<DocumentJob>;
  /** Permanent, every version of the document (§14.3). */
  deleteDocument(school: string, fileUuid: string, body: { confirm_file_uuid: string }): Promise<DocumentDeleteResult>;

  // ---- Try a question (CONTRACT.md §14.9) ----
  /** Runs the school's chat cascade; a full answer is one LLM call, `retrieval_only` is free. */
  askPreview(school: string, body: AskPreviewBody, signal?: AbortSignal): Promise<AskPreviewResult>;
}
