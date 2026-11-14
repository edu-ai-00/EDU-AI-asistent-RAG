import type { ApiClient } from "./client";
import { ApiError } from "./types";
import type {
  Domain,
  DocumentJob,
  DocumentMetadata,
  DocumentChunk,
  DocumentOptions,
  DocumentSummary,
  DocumentUploadItem,
  DocumentVersion,
  ValidityState,
} from "./types";
import documentsFixture from "../fixtures/documents.json";
import documentOptionsFixture from "../fixtures/documentOptions.json";

type DocumentMethods = Pick<
  ApiClient,
  | "listDocuments"
  | "getDocument"
  | "getDocumentOptions"
  | "uploadDocument"
  | "listDocumentJobs"
  | "deactivateDocument"
  | "activateDocument"
  | "deleteDocument"
  | "updateDocumentValidity"
  | "checkDocumentSources"
  | "previewDocumentUrl"
  | "documentDownloadUrl"
  | "downloadDocument"
  | "getDocumentStats"
  | "listDocumentChunks"
  | "refreshDocument"
>;

type MockDoc = { base: DocumentSummary; versions: DocumentVersion[] };
type MockJob = DocumentJob & { _steps: number; _chunks: number; _fail: boolean; _item: DocumentUploadItem; _sha: string };

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function uuid(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const hex = Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-8${hex.slice(17, 20)}-${hex.slice(20)}`;
}

async function delay(ms = 30): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

function notFound(): ApiError {
  return new ApiError(404, { code: "NOT_FOUND", message: "Dokument nenalezen.", retryable: false });
}

function validationError(message: string, fields: Array<{ field: string; message: string }>): ApiError {
  return new ApiError(422, { code: "VALIDATION_ERROR", message, retryable: false, fields });
}

/** §14.8: against today's date, like CURRENT_DATE in search. */
function validityStateOf(validFrom: string | null, validTo: string | null): ValidityState {
  const today = new Date().toISOString().slice(0, 10);
  if (validFrom && validFrom > today) return "future";
  if (validTo && validTo < today) return "expired";
  return "current";
}

/** §14.10: the extension of the uploaded file, "web" for a saved page. */
function formatOf(base: DocumentSummary): string | null {
  if (base.source_kind === "web") return "web";
  const ext = /\.([A-Za-z0-9]+)$/.exec(base.original_file_name ?? "")?.[1];
  return ext ? ext.toLowerCase() : null;
}

/** A fixture document whose stored file is "gone" (data moved without uploads). */
export const MOCK_UNSTORED_FILE_UUID = "708192a3-7777-4abc-8def-000000000801";

/** §14.1: the shown version is the active one, else the newest. */
function summarize(doc: MockDoc): DocumentSummary {
  const sorted = [...doc.versions].sort(
    (a, b) => b.ingested_at.localeCompare(a.ingested_at) || b.file_uuid.localeCompare(a.file_uuid),
  );
  const shown = sorted.find((v) => v.is_active) ?? sorted[0];
  return {
    ...doc.base,
    file_uuid: shown.file_uuid,
    ingested_at: shown.ingested_at,
    is_active: sorted.some((v) => v.is_active),
    version_count: sorted.length,
    chunk_count: shown.chunk_count,
    validity_state: validityStateOf(doc.base.valid_from, doc.base.valid_to),
    file_format: formatOf(doc.base),
    content_sha256: shown.content_sha256 ?? null,
  };
}

/** A stable stand-in for the content hash: the same source always gives the same hex. */
function fakeSha(text: string): string {
  let h = 2166136261;
  let out = "";
  for (let round = 0; round < 8; round++) {
    for (let i = 0; i < text.length; i++) h = Math.imul(h ^ (text.charCodeAt(i) + round), 16777619) >>> 0;
    out += h.toString(16).padStart(8, "0");
  }
  return out;
}

function addDays(date: Date, days: number): string {
  const d = new Date(date);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

/** Deterministic chunks of one version (§14.13): a few long enough to be clipped in the UI. */
function mockChunk(title: string, index: number): DocumentChunk {
  const section = Math.floor(index / 4) + 1;
  const sentence = `Odstavec ${index + 1} dokumentu ${title} popisuje pravidla školy a jejich výjimky.`;
  const text = index % 3 === 0 ? Array.from({ length: 8 }, () => sentence).join(" ") : sentence;
  return {
    chunk_index: index,
    heading_path: `Kapitola ${section} > Oddíl ${(index % 4) + 1}`,
    section_heading: `Oddíl ${(index % 4) + 1}`,
    text,
    char_count: text.length,
  };
}

function sourceKey(d: DocumentSummary): string {
  return d.source_url ? `url:${d.source_url}` : `file:${d.original_file_name}|${d.user_name ?? ""}`;
}

/** Fixture-backed §14 endpoints for the mock adapter. Jobs advance one step
 *  per listDocumentJobs call (queued → processing → completed, then chunks),
 *  concurrency 1 per school like the real queue. */
export function createMockDocuments(
  domainsFor: (school: string) => Domain[],
  qaEntriesActive: (school: string) => number = () => 0,
): DocumentMethods {
  const fixture = documentsFixture as unknown as Record<string, Array<{ document: DocumentSummary; versions: DocumentVersion[] }>>;
  const docs = new Map<string, MockDoc[]>();
  for (const [school, list] of Object.entries(fixture)) {
    docs.set(school, list.map((d) => ({ base: clone(d.document), versions: clone(d.versions) })));
  }
  const jobs: MockJob[] = [];
  const options = documentOptionsFixture as unknown as DocumentOptions;

  function schoolDocs(school: string): MockDoc[] {
    domainsFor(school); // throws 404 for an unknown school
    if (!docs.has(school)) docs.set(school, []);
    return docs.get(school)!;
  }

  function findDoc(school: string, fileUuid: string): MockDoc {
    const doc = schoolDocs(school).find((d) => d.versions.some((v) => v.file_uuid === fileUuid));
    if (!doc) throw notFound();
    return doc;
  }

  function validateMetadata(school: string, meta: DocumentMetadata): Array<{ field: string; message: string }> {
    const out: Array<{ field: string; message: string }> = [];
    const has = (list: Array<{ code: string }>, code: string) => list.some((o) => o.code === code);
    if (meta.visibility !== "entity" && meta.visibility !== "organization") {
      out.push({ field: "metadata.visibility", message: "visibility must be 'entity' or 'organization'" });
    }
    if (!meta.audience_roles || meta.audience_roles.length === 0) {
      out.push({ field: "metadata.audience_roles", message: "at least one role is required" });
    }
    (meta.audience_roles || []).forEach((r, i) => {
      if (!has(options.audience_roles, r)) out.push({ field: `metadata.audience_roles[${i}]`, message: `unknown role ${r}` });
    });
    if (meta.document_type && !has(options.document_types, meta.document_type)) {
      out.push({ field: "metadata.document_type", message: `unknown document type ${meta.document_type}` });
    }
    if (meta.document_language && !has(options.languages, meta.document_language)) {
      out.push({ field: "metadata.document_language", message: `unknown language ${meta.document_language}` });
    }
    if (meta.school_subject && !has(options.school_subjects, meta.school_subject)) {
      out.push({ field: "metadata.school_subject", message: `unknown subject ${meta.school_subject}` });
    }
    const domains = domainsFor(school);
    (meta.domain_codes || []).forEach((d, i) => {
      if (!domains.some((x) => x.code === d)) out.push({ field: `metadata.domain_codes[${i}]`, message: `unknown domain ${d}` });
    });
    if (meta.valid_from && meta.valid_to && meta.valid_from > meta.valid_to) {
      out.push({ field: "metadata.valid_to", message: "valid_from must not be after valid_to" });
    }
    return out;
  }

  /** A completed job becomes a new active version of its source's document. */
  function storeCompleted(job: MockJob): void {
    const list = schoolDocs(job.school);
    const meta = job._item.metadata;
    const now = new Date().toISOString();
    const fileUuid = uuid();
    const base: DocumentSummary = {
      file_uuid: fileUuid,
      title: job.source,
      source_kind: job.source_kind === "url" ? "web" : "file",
      source_url: job.source_kind === "url" ? job.source : null,
      original_file_name: job.source_kind === "file" ? job.source : null,
      document_type: meta.document_type ?? "ANNOUNCEMENT",
      document_language: meta.document_language ?? "cs",
      school_subject: meta.school_subject,
      school_grade_codes: [...meta.school_grade_codes],
      domain_codes: [...meta.domain_codes],
      audience_roles: [...meta.audience_roles],
      visibility: meta.visibility,
      valid_from: meta.valid_from,
      valid_to: meta.valid_to,
      user_name: `qa_portal__${job.school}`,
      ingested_at: now,
      is_active: true,
      version_count: 1,
      chunk_count: job._chunks,
    };
    const version: DocumentVersion = {
      file_uuid: fileUuid,
      processing_uuid: job.processing_uuid,
      ingested_at: now,
      is_active: true,
      chunk_count: job._chunks,
      content_sha256: job._sha,
    };
    const existing = list.find((d) => sourceKey(d.base) === sourceKey(base));
    if (existing) {
      existing.versions.forEach((v) => (v.is_active = false));
      existing.versions.push(version);
      existing.base = { ...base };
    } else {
      list.push({ base, versions: [version] });
    }
  }

  function advanceJobs(school: string): void {
    const mine = jobs.filter((j) => j.school === school);
    const now = new Date().toISOString();
    // Chunk counts arrive one poll after completion (ingest-status lag).
    for (const j of mine) {
      if (j.status === "completed" && j.stored_chunks === null && j._steps <= 0) j.stored_chunks = j._chunks;
    }
    const running = mine.find((j) => j.status === "processing");
    if (running) {
      running._steps -= 1;
      if (running._steps <= 0) {
        running.finished_at = now;
        if (running._fail) {
          running.status = "failed";
          running.result = { status: "failed", total_files: 1, ok_count: 0, error_count: 1 };
          running.error = { code: "INGEST_FAILED", message: "Gateway dokument odmítl: soubor nejde přečíst." };
        } else {
          running.status = "completed";
          running.processing_uuid = uuid();
          running.result = { status: "completed", total_files: 1, ok_count: 1, error_count: 0 };
          storeCompleted(running);
        }
      }
      return;
    }
    const next = mine.filter((j) => j.status === "queued").sort((a, b) => a.created_at.localeCompare(b.created_at))[0];
    if (next) {
      // §14.14: unchanged content ends as "skipped" at once, with no ingest.
      const current = schoolDocs(next.school)
        .map(summarize)
        .find((d) => (next.source_kind === "url" ? d.source_url === next.source : d.original_file_name === next.source));
      if (current && current.content_sha256 && current.content_sha256 === next._sha) {
        next.status = "skipped";
        next.started_at = now;
        next.finished_at = now;
        next.result = { reason: "unchanged", file_uuid: current.file_uuid };
        return;
      }
      next.status = "processing";
      next.started_at = now;
    }
  }

  function publicJob(j: MockJob): DocumentJob {
    const { _steps, _chunks, _fail, _item, _sha, ...rest } = j;
    void _sha;
    void _steps;
    void _chunks;
    void _fail;
    void _item;
    return clone(rest);
  }

  return {
    async listDocuments(school, params) {
      await delay();
      const status = params?.status ?? "active";
      const q = (params?.q ?? "").trim().toLowerCase();
      const validity = params?.validity ?? "all";
      const limit = Math.min(Math.max(params?.limit ?? 50, 1), 200);
      const offset = params?.cursor ? Number(params.cursor) || 0 : 0;
      const all = schoolDocs(school)
        .map(summarize)
        .filter((d) => status === "all" || (status === "active" ? d.is_active : !d.is_active))
        .filter((d) => !q || `${d.source_url ?? ""} ${d.original_file_name ?? ""}`.toLowerCase().includes(q))
        .filter((d) => !params?.type || d.document_type === params.type)
        .filter((d) => !params?.role || d.audience_roles.includes(params.role))
        .filter((d) => validity === "all" || d.validity_state === validity)
        .filter((d) => !params?.format || d.file_format === params.format)
        .filter((d) => !params?.source || d.source_url === params.source || d.original_file_name === params.source)
        .sort((a, b) => b.ingested_at.localeCompare(a.ingested_at) || b.file_uuid.localeCompare(a.file_uuid));
      const page = all.slice(offset, offset + limit);
      return { documents: clone(page), next_cursor: offset + limit < all.length ? String(offset + limit) : null };
    },

    async getDocument(school, fileUuid) {
      await delay();
      const doc = findDoc(school, fileUuid);
      const versions = [...doc.versions].sort((a, b) => b.ingested_at.localeCompare(a.ingested_at));
      const format = formatOf(doc.base);
      return { document: summarize(doc), versions: clone(versions).map((v) => ({ ...v, file_format: format })) };
    },

    documentDownloadUrl(school, fileUuid) {
      return `/mock-download/${encodeURIComponent(school)}/${encodeURIComponent(fileUuid)}`;
    },

    async downloadDocument(school, fileUuid) {
      await delay();
      const doc = findDoc(school, fileUuid);
      if (fileUuid === MOCK_UNSTORED_FILE_UUID) {
        throw new ApiError(404, { code: "NOT_FOUND", message: "the file of this version is not stored", retryable: false });
      }
      const web = doc.base.source_kind === "web";
      const name = web ? `${fileUuid}.html` : (doc.base.original_file_name ?? `${fileUuid}.bin`);
      return {
        blob: new Blob([web ? "<html><body>Mock page</body></html>" : "Mock file content"], {
          type: "application/octet-stream",
        }),
        filename: name,
      };
    },

    async getDocumentOptions(school) {
      await delay();
      return { ...clone(options), domains: clone(domainsFor(school)) };
    },

    async uploadDocument(school, item) {
      await delay();
      schoolDocs(school);
      if (Boolean(item.file) === Boolean(item.url)) {
        throw validationError("Pošlete právě jeden soubor nebo jednu URL.", [{ field: "file", message: "file xor url" }]);
      }
      if (item.file) {
        const name = item.file.name;
        const ext = name.includes(".") ? name.slice(name.lastIndexOf(".")).toLowerCase() : "";
        if (!options.file_extensions.includes(ext)) {
          throw new ApiError(415, {
            code: "UNSUPPORTED_MEDIA_TYPE",
            message: `Typ souboru ${ext || "(bez přípony)"} není podporován. QA balíčky (.csv) se nahrávají na stránce QA balíčky.`,
            retryable: false,
          });
        }
        if (item.file.size === 0) throw validationError("Soubor je prázdný.", [{ field: "file", message: "empty file" }]);
        if (item.file.size > options.max_file_bytes) {
          throw new ApiError(413, { code: "PAYLOAD_TOO_LARGE", message: "Soubor je větší než povolený limit.", retryable: false });
        }
      } else if (!/^https?:\/\/[^\s/]+/i.test(item.url ?? "")) {
        throw validationError("Neplatná URL.", [{ field: "url", message: "absolute http(s) URL required" }]);
      }
      const fieldErrors = validateMetadata(school, item.metadata);
      if (fieldErrors.length > 0) throw validationError("Metadata neprošla kontrolou.", fieldErrors);
      if (jobs.filter((j) => j.batch_id === item.batch_id).length >= options.max_batch_items) {
        throw validationError("Dávka je příliš velká.", [{ field: "batch_id", message: "too many items in this batch" }]);
      }
      const source = item.file ? item.file.name : item.url!;
      const job: MockJob = {
        job_id: uuid(),
        batch_id: item.batch_id,
        school,
        source_kind: item.file ? "file" : "url",
        source,
        status: "queued",
        created_at: new Date(Date.now() + jobs.length).toISOString(),
        started_at: null,
        finished_at: null,
        processing_uuid: null,
        result: null,
        stored_chunks: null,
        error: null,
        _steps: 1,
        _chunks: 3 + (source.length % 9),
        _fail: /chyba|fail/i.test(source),
        _item: item,
        _sha: fakeSha(item.file ? `${item.file.name}:${item.file.size}` : source),
      };
      jobs.push(job);
      return publicJob(job);
    },

    async listDocumentJobs(school, params) {
      await delay();
      schoolDocs(school);
      advanceJobs(school);
      const mine = jobs.filter((j) => j.school === school);
      if (params?.batch_id) {
        return mine
          .filter((j) => j.batch_id === params.batch_id)
          .sort((a, b) => a.created_at.localeCompare(b.created_at))
          .map(publicJob);
      }
      return mine
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .slice(0, 200)
        .map(publicJob);
    },

    async deactivateDocument(school, fileUuid) {
      await delay();
      const doc = findDoc(school, fileUuid);
      doc.versions.forEach((v) => (v.is_active = false));
      return summarize(doc);
    },

    async activateDocument(school, fileUuid) {
      await delay();
      const doc = findDoc(school, fileUuid);
      const newest = [...doc.versions].sort(
        (a, b) => b.ingested_at.localeCompare(a.ingested_at) || b.file_uuid.localeCompare(a.file_uuid),
      )[0];
      doc.versions.forEach((v) => (v.is_active = v === newest));
      return summarize(doc);
    },

    async updateDocumentValidity(school, fileUuid, body) {
      await delay();
      const doc = findDoc(school, fileUuid);
      if (body.valid_from && body.valid_to && body.valid_from > body.valid_to) {
        throw validationError("Datum od nesmí být po datu do.", [{ field: "valid_to", message: "valid_from must not be after valid_to" }]);
      }
      doc.base = { ...doc.base, valid_from: body.valid_from, valid_to: body.valid_to };
      return summarize(doc);
    },

    async checkDocumentSources(school, sources) {
      await delay();
      const list = schoolDocs(school).map(summarize);
      const uploader = `qa_portal__${school}`;
      return sources.map((src) => {
        const existing =
          list.find((d) => (src.kind === "url" ? d.source_url === src.value : d.original_file_name === src.value)) ?? null;
        // §14.8: a URL is always retired; a file only when the same uploader sent it.
        const supersedes = existing !== null && (src.kind === "url" || existing.user_name === uploader);
        return { kind: src.kind, value: src.value, existing: existing ? clone(existing) : null, supersedes };
      });
    },

    async previewDocumentUrl(school, body) {
      await delay();
      schoolDocs(school);
      if (!/^https?:\/\/[^\s/]+/i.test(body.url)) throw validationError("Neplatná URL.", [{ field: "url", message: "absolute http(s) URL required" }]);
      const base = body.url.replace(/\/+$/, "");
      const children = body.save_level_1_child ? Array.from({ length: Math.min(body.max_child_links, 3) }, (_, i) => i + 1) : [];
      return [
        { source_url: body.url, source_type: "html", page_role: "parent" as const },
        ...children.map((n) => ({ source_url: `${base}/odkaz-${n}`, source_type: "html", page_role: "child" as const })),
      ];
    },

    async getDocumentStats(school) {
      await delay();
      const list = schoolDocs(school).map(summarize);
      const limit = addDays(new Date(), 30);
      const today = addDays(new Date(), 0);
      const active = list.filter((d) => d.is_active);
      return {
        active: active.length,
        inactive: list.length - active.length,
        expired: active.filter((d) => d.validity_state === "expired").length,
        future: active.filter((d) => d.validity_state === "future").length,
        expiring_30: active.filter((d) => d.validity_state === "current" && d.valid_to !== null && d.valid_to >= today && d.valid_to <= limit).length,
        qa_entries_active: qaEntriesActive(school),
      };
    },

    async listDocumentChunks(school, fileUuid, params) {
      await delay();
      const doc = findDoc(school, fileUuid);
      const version = doc.versions.find((v) => v.file_uuid === fileUuid)!;
      const limit = Math.min(Math.max(params?.limit ?? 20, 1), 100);
      const offset = params?.cursor ? Number(params.cursor) || 0 : 0;
      const total = version.chunk_count;
      const chunks = Array.from({ length: Math.max(0, Math.min(limit, total - offset)) }, (_, i) =>
        mockChunk(doc.base.title, offset + i),
      );
      return { chunks, total, next_cursor: offset + limit < total ? String(offset + limit) : null };
    },

    async refreshDocument(school, fileUuid) {
      await delay();
      const doc = findDoc(school, fileUuid);
      if (doc.base.source_kind !== "web" || !doc.base.source_url) {
        throw validationError("Obnovit z webu lze jen webový dokument.", [{ field: "file_uuid", message: "not a web document" }]);
      }
      const shown = summarize(doc);
      const metadata: DocumentMetadata = {
        visibility: shown.visibility === "organization" ? "organization" : "entity",
        audience_roles: [...shown.audience_roles],
        document_type: shown.document_type,
        document_language: shown.document_language,
        school_subject: shown.school_subject,
        school_grade_codes: [...shown.school_grade_codes],
        domain_codes: [...shown.domain_codes],
        valid_from: shown.valid_from,
        valid_to: shown.valid_to,
      };
      const url = doc.base.source_url;
      const job: MockJob = {
        job_id: uuid(),
        batch_id: uuid(),
        school,
        source_kind: "url",
        source: url,
        status: "queued",
        created_at: new Date(Date.now() + jobs.length).toISOString(),
        started_at: null,
        finished_at: null,
        processing_uuid: null,
        result: null,
        stored_chunks: null,
        error: null,
        _steps: 1,
        _chunks: shown.chunk_count || 4,
        _fail: false,
        _item: { url, batch_id: "refresh", metadata },
        // A real refresh compares the cleaned page text; the mock page "changes" only when the
        // current version has no recorded hash (an ingest from outside the portal).
        _sha: shown.content_sha256 ?? fakeSha(`${url}:${Date.now()}`),
      };
      jobs.push(job);
      return publicJob(job);
    },

    async deleteDocument(school, fileUuid, body) {
      await delay();
      const list = schoolDocs(school);
      const doc = findDoc(school, fileUuid);
      if (body.confirm_file_uuid !== fileUuid) {
        throw new ApiError(409, {
          code: "DELETE_CONFIRMATION_MISMATCH",
          message: "Potvrzení smazání nesouhlasí s dokumentem. Smazání bylo odmítnuto.",
          retryable: false,
        });
      }
      list.splice(list.indexOf(doc), 1);
      const chunks = doc.versions.reduce((n, v) => n + v.chunk_count, 0);
      return {
        deleted: { versions: doc.versions.length, chunks, elements: chunks * 2, tables: 0 },
        file_uuids: doc.versions.map((v) => v.file_uuid),
        files_removed: doc.base.source_kind === "file" ? doc.versions.length : 0,
        warnings: [],
      };
    },
  };
}
