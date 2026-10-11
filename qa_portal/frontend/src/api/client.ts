import type {
  Category,
  Context,
  DeleteResult,
  Domain,
  Entry,
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
  createExport(
    school: string,
    body: { pack_ids: string[]; format?: "csv" | "zip"; lang?: string },
  ): Promise<ExportResult>;
  downloadExportUrl(exportId: string): string;
}
