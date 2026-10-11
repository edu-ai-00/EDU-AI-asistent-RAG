import type { ApiClient } from "./client";
import { ApiError } from "./types";
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
  Preview,
  School,
} from "./types";

// Every write is a "non-simple" request per CONTRACT.md §8: JSON/multipart
// content-type plus this header. The server also checks Origin/Referer;
// same-origin credentials cover cookies if the platform ever adds a session.
const CSRF_HEADER: Record<string, string> = { "X-QA-Portal": "1" };

type Query = Record<string, string | number | boolean | undefined>;

async function readBody(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function toApiError(status: number, body: unknown): ApiError {
  const envelope = body as { error?: { code?: string; message?: string; retryable?: boolean; fields?: unknown } } | null;
  return new ApiError(status, {
    code: envelope?.error?.code || "INTERNAL_ERROR",
    message: envelope?.error?.message || `Požadavek selhal (${status}).`,
    retryable: envelope?.error?.retryable ?? false,
    fields: envelope?.error?.fields as ApiError["fields"],
  });
}

/** Builds the real fetch-based client. Kept independent from mockClient.ts
 *  so a future frontend variant can reuse this file unchanged while only
 *  strings.cs.ts / tokens.css differ (CONTRACT.md §10). */
export function createLiveClient(baseUrl: string): ApiClient {
  function path(p: string, query?: Query): string {
    const usp = new URLSearchParams();
    if (query) {
      for (const [k, v] of Object.entries(query)) if (v !== undefined) usp.set(k, String(v));
    }
    const qs = usp.toString();
    return `${baseUrl}${p}${qs ? `?${qs}` : ""}`;
  }

  async function request<T>(
    method: string,
    p: string,
    opts: { query?: Query; json?: unknown; form?: FormData; signal?: AbortSignal } = {},
  ): Promise<T> {
    const headers: Record<string, string> = {};
    let body: BodyInit | undefined;
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      Object.assign(headers, CSRF_HEADER);
      body = JSON.stringify(opts.json);
    } else if (opts.form) {
      Object.assign(headers, CSRF_HEADER); // browser sets multipart boundary itself
      body = opts.form;
    } else if (method !== "GET") {
      // CONTRACT.md §8/§13.2: every write is "non-simple", including a
      // bodiless DELETE (permanent delete of one entry) — the header must
      // still be present or the server's origin/CSRF check rejects it.
      Object.assign(headers, CSRF_HEADER);
    }
    const response = await fetch(path(p, opts.query), {
      method,
      credentials: "same-origin",
      headers,
      body,
      signal: opts.signal,
    });
    const parsed = await readBody(response);
    if (!response.ok) throw toApiError(response.status, parsed);
    return parsed as T;
  }

  return {
    getContext: (signal) => request<Context>("GET", "/context", { signal }),
    listSchools: async (signal) => (await request<{ schools: School[] }>("GET", "/schools", { signal })).schools,
    listSchoolDomains: async (school, signal) =>
      (await request<{ domains: Domain[] }>("GET", `/schools/${encodeURIComponent(school)}/domains`, { signal })).domains,
    listCategories: async (params, signal) =>
      (await request<{ categories: Category[] }>("GET", "/categories", { query: params, signal })).categories,
    createCategory: (body) => request<Category>("POST", "/categories", { json: body }),
    listPacks: async (school, signal) =>
      (await request<{ packs: Pack[] }>("GET", `/schools/${encodeURIComponent(school)}/packs`, { signal })).packs,
    createPack: (school, body) =>
      request<{ pack_id: string; available: boolean }>("POST", `/schools/${encodeURIComponent(school)}/packs`, { json: body }),
    getMetadataOptions: (school, lang, signal) =>
      request<MetadataOptions>("GET", `/schools/${encodeURIComponent(school)}/metadata-options`, { query: { lang }, signal }),
    listEntries: (school, pack, params, signal) =>
      request<EntryPage>("GET", `/schools/${encodeURIComponent(school)}/packs/${encodeURIComponent(pack)}/entries`, {
        query: params,
        signal,
      }),
    getEntry: (school, pack, entryKey, signal) =>
      request<{ entry: Entry; pack_etag: string; pack_version: number }>(
        "GET",
        `/schools/${encodeURIComponent(school)}/packs/${encodeURIComponent(pack)}/entries/${encodeURIComponent(entryKey)}`,
        { signal },
      ),
    previewPackChanges: (school, pack, body) =>
      request<Preview>("POST", `/schools/${encodeURIComponent(school)}/packs/${encodeURIComponent(pack)}/preview`, {
        json: body,
      }),
    publishPackChanges: (school, pack, body) =>
      request<Operation>("POST", `/schools/${encodeURIComponent(school)}/packs/${encodeURIComponent(pack)}/publish`, {
        json: body,
      }),
    previewImport: (school, file, opts) => {
      const form = new FormData();
      form.set("file", file);
      if (opts.pack_id) form.set("pack_id", opts.pack_id);
      if (opts.mode) form.set("mode", opts.mode);
      // CONTRACT.md §2.4c: the server parses this as a JSON string field,
      // not a nested multipart structure — never send the object itself.
      if (opts.pack_metadata_by_pack) form.set("pack_metadata_by_pack", JSON.stringify(opts.pack_metadata_by_pack));
      return request<ImportPreview>("POST", `/schools/${encodeURIComponent(school)}/imports/preview`, { form });
    },
    publishImport: (school, previewId, body) =>
      request<Operation>(
        "POST",
        `/schools/${encodeURIComponent(school)}/imports/${encodeURIComponent(previewId)}/publish`,
        { json: body },
      ),
    getOperation: (operationId, signal) => request<Operation>("GET", `/operations/${encodeURIComponent(operationId)}`, { signal }),
    deleteEntry: (school, pack, entryKey) =>
      request<DeleteResult>(
        "DELETE",
        `/schools/${encodeURIComponent(school)}/packs/${encodeURIComponent(pack)}/entries/${encodeURIComponent(entryKey)}`,
      ),
    deletePack: (school, pack, body) =>
      request<DeleteResult>("DELETE", `/schools/${encodeURIComponent(school)}/packs/${encodeURIComponent(pack)}`, { json: body }),
    createExport: (school, body) =>
      request<ExportResult>("POST", `/schools/${encodeURIComponent(school)}/exports`, { json: body }),
    downloadExportUrl: (exportId) => `${baseUrl}/exports/${encodeURIComponent(exportId)}/download`,
  };
}
