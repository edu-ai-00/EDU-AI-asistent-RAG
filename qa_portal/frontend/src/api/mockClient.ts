import type { ApiClient } from "./client";
import { ApiError } from "./types";
import type {
  Category,
  Context,
  Diff,
  Domain,
  Entry,
  EntryPage,
  EntryWrite,
  ImportPreview,
  MetadataOptions,
  Operation,
  Pack,
  PackMetadata,
  Preview,
  School,
} from "./types";
import { parseCsv, rowsToRecords, serializeCsv } from "../lib/csv";

import contextFixture from "../fixtures/context.json";
import schoolsFixture from "../fixtures/schools.json";
import domainsKavciHory from "../fixtures/domains.ZS_KAVCI_HORY.json";
import domainsJedovnice from "../fixtures/domains.ZS_JEDOVNICE.json";
import categoriesFixture from "../fixtures/categories.json";
import packsKavciHory from "../fixtures/packs.ZS_KAVCI_HORY.json";
import packsJedovnice from "../fixtures/packs.ZS_JEDOVNICE.json";
import entriesKavciHoryFaq from "../fixtures/entries.ZS_KAVCI_HORY.faq-provoz.json";
import entriesJedovniceDruzina from "../fixtures/entries.ZS_JEDOVNICE.skolni-druzina.json";
import conflictFixture from "../fixtures/conflict.json";
import importPreviewZipFixture from "../fixtures/importPreviewZip.json";
import metadataOptionsFixture from "../fixtures/metadataOptions.json";

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function randomId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `mock-${Math.random().toString(16).slice(2)}-${Date.now().toString(16)}`;
}

async function delay(ms = 30): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

/** NOT the backend's real pack_etag (sha256 of canonical_json per
 *  CONTRACT.md §3 — that is search-service-owned). Only a mock-adapter
 *  stand-in so the demo can exercise the version-conflict flow deterministically. */
function computeMockEtag(entries: Entry[], version: number): string {
  const key = entries
    .map((e) => `${e.entry_key}:${e.answer_md.length}:${e.is_active}:${e.domains.join(",")}`)
    .sort()
    .join("|");
  let hash = 0;
  for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  return `sha256:mock-${version}-${hash.toString(16)}`;
}

type PackState = { meta: Pack; entries: Entry[] };
type SchoolState = { domains: Domain[]; packs: Map<string, PackState> };

type PreviewRecord = {
  school: string;
  pack: string;
  nextEntries: Entry[];
  baseEtag: string;
  /** Set only when this preview belongs to a pack with no server state yet
   *  (CONTRACT.md §2.4a) — publish materializes the pack row from this. */
  newPackMetadata?: PackMetadata;
};
type OperationRecord = Operation & { _pollsRemaining: number };

const METADATA_ROLE_CODES = new Set(metadataOptionsFixture.audience_roles.map((r) => r.code.toUpperCase()));
const METADATA_GRADE_CODES = new Set(metadataOptionsFixture.school_grades.map((g) => g.code.toUpperCase()));

/** Mirrors qa_portal/backend/app/services/validation.py::validate_pack_metadata
 *  closely enough for the fixture adapter: same field names (always prefixed
 *  `pack_metadata.`, per the backend, regardless of which endpoint sent it),
 *  same required-role and unknown-code checks. */
function validatePackMetadataMock(meta: PackMetadata): Array<{ field: string; message: string }> {
  const out: Array<{ field: string; message: string }> = [];
  if (meta.visibility !== "entity" && meta.visibility !== "organization") {
    out.push({
      field: "pack_metadata.visibility",
      message: `visibility must be 'entity' or 'organization', got ${JSON.stringify(meta.visibility)}`,
    });
  }
  const roles = meta.audience_roles || [];
  if (roles.length === 0) {
    out.push({ field: "pack_metadata.audience_roles", message: "at least one role is required" });
  }
  roles.forEach((r, i) => {
    if (!METADATA_ROLE_CODES.has(String(r).toUpperCase())) {
      out.push({ field: `pack_metadata.audience_roles[${i}]`, message: `unknown role code ${JSON.stringify(r)}` });
    }
  });
  const grades = meta.school_grade_codes || [];
  grades.forEach((g, i) => {
    if (!METADATA_GRADE_CODES.has(String(g).toUpperCase())) {
      out.push({ field: `pack_metadata.school_grade_codes[${i}]`, message: `unknown school_grade code ${JSON.stringify(g)}` });
    }
  });
  return out;
}

const CSV_COLUMNS = ["entry_key", "canonical_q", "answer_md", "cat", "variants", "sources", "is_active", "domains", "category_code"];

/** Mirrors preview_service.py::_resolve_pack_metadata (CONTRACT.md §2.4c) for
 *  the fixture adapter: validates `pack_metadata_by_pack` against the SAME
 *  per-pack rules the backend applies (unknown pack_id, existing pack, or a
 *  metadata shape error) and returns the top-level field errors that make
 *  the whole preview a 422 — never a partial 200. `by_id` packs are mutated
 *  neither here nor by the caller; the confirmed map is returned instead so
 *  the caller decides what to persist alongside the stored preview. */
function resolveImportMetadataMock(
  packs: Array<{ pack_id: string; file: string; ok: boolean; is_new_pack?: boolean }>,
  requested: Record<string, PackMetadata>,
): { fieldErrors: Array<{ field: string; message: string; file?: string }>; confirmed: Map<string, PackMetadata> } {
  const byId = new Map(packs.map((p) => [p.pack_id, p]));
  const fieldErrors: Array<{ field: string; message: string; file?: string }> = [];
  const confirmed = new Map<string, PackMetadata>();

  for (const [packId, meta] of Object.entries(requested)) {
    const p = byId.get(packId);
    if (!p) {
      fieldErrors.push({ field: `pack_metadata_by_pack.${packId}`, message: "no such pack in this import" });
      continue;
    }
    if (!p.is_new_pack) {
      fieldErrors.push({
        field: `pack_metadata_by_pack.${packId}`,
        message: "an existing pack keeps its own metadata (CONTRACT.md §2.4)",
      });
      continue;
    }
    const errs = validatePackMetadataMock(meta);
    if (errs.length > 0) {
      for (const e of errs) fieldErrors.push({ ...e, file: p.file });
      continue;
    }
    confirmed.set(packId, meta);
  }

  return { fieldErrors, confirmed };
}

/** Sets metadata_required/publishable (and, when applicable, suggested_metadata)
 *  on each pack result in place — same derived flags the backend attaches. */
function annotateMetadataFlags(
  packs: Array<{ pack_id: string; ok: boolean; is_new_pack?: boolean } & Record<string, unknown>>,
  confirmed: Map<string, PackMetadata>,
  suggestedByPack?: Map<string, PackMetadata>,
): void {
  for (const p of packs) {
    if (p.is_new_pack) {
      const hasConfirmed = confirmed.has(p.pack_id);
      p.metadata_required = !hasConfirmed;
      if (!hasConfirmed) {
        const suggestion = suggestedByPack?.get(p.pack_id);
        if (suggestion) p.suggested_metadata = suggestion;
      }
    } else {
      p.metadata_required = false;
    }
    p.publishable = Boolean(p.ok) && !p.metadata_required;
  }
}

function diffEntries(base: Entry[], writes: EntryWrite[]): { diff: Diff; nextEntries: Entry[] } {
  const byKey = new Map(base.map((e) => [e.entry_key, e]));
  const added: Diff["added"] = [];
  const changed: Diff["changed"] = [];
  const deactivated: Diff["deactivated"] = [];
  const touched = new Set<string>();

  for (const w of writes) {
    const key = w.entry_key && w.entry_key.trim() ? w.entry_key : `new-${randomId().slice(0, 8)}`;
    touched.add(key);
    const existing = byKey.get(key);

    if (!existing) {
      added.push({ entry_key: key, canonical_q: w.canonical_q });
      byKey.set(key, {
        entry_key: key,
        canonical_q: w.canonical_q,
        answer_md: w.answer_md,
        variants: [...w.variants],
        sources: w.sources.map((s) => ({ ...s })),
        domains: [...w.domains],
        category_code: w.category_code,
        is_active: w.is_active,
      });
      continue;
    }

    const fields: Diff["changed"][number]["fields"] = [];
    const push = (field: string, oldV: unknown, newV: unknown) => {
      if (JSON.stringify(oldV) !== JSON.stringify(newV)) fields.push({ field, old: oldV, new: newV });
    };
    push("canonical_q", existing.canonical_q, w.canonical_q);
    push("answer_md", existing.answer_md, w.answer_md);
    push("domains", existing.domains, w.domains);
    push("category_code", existing.category_code ?? null, w.category_code);
    push("variants", existing.variants ?? [], w.variants);
    push("is_active", existing.is_active, w.is_active);

    if (existing.is_active && !w.is_active) deactivated.push({ entry_key: key, canonical_q: existing.canonical_q });
    if (fields.length > 0) changed.push({ entry_key: key, fields });

    byKey.set(key, {
      ...existing,
      canonical_q: w.canonical_q,
      answer_md: w.answer_md,
      variants: [...w.variants],
      sources: w.sources.map((s) => ({ ...s })),
      domains: [...w.domains],
      category_code: w.category_code,
      is_active: w.is_active,
    });
  }

  const unchanged = base.map((e) => e.entry_key).filter((k) => !touched.has(k));
  return { diff: { added, changed, unchanged, deactivated }, nextEntries: Array.from(byKey.values()) };
}

/** Import-specific wrapper around diffEntries: in "replace" mode, any
 *  existing entry the file does not mention is not merely "unchanged" — it
 *  is deactivated (moved from diff.unchanged into diff.deactivated, and its
 *  is_active flips), because the whole pack is meant to end up matching the
 *  file. "merge" mode (the default) never touches anything outside the
 *  file, so it is just diffEntries. Used identically at preview and at
 *  publish time so the two never disagree about what replace does. */
function computeImportDiff(base: Entry[], writes: EntryWrite[], mode: "merge" | "replace"): { diff: Diff; nextEntries: Entry[] } {
  const { diff, nextEntries } = diffEntries(base, writes);
  if (mode !== "replace") return { diff, nextEntries };
  const byKey = new Map(nextEntries.map((e) => [e.entry_key, e]));
  const deactivated = [...diff.deactivated];
  const remainingUnchanged: string[] = [];
  for (const key of diff.unchanged) {
    const entry = byKey.get(key);
    if (!entry) continue;
    if (entry.is_active) {
      deactivated.push({ entry_key: key, canonical_q: entry.canonical_q });
      byKey.set(key, { ...entry, is_active: false });
    } else {
      remainingUnchanged.push(key);
    }
  }
  return { diff: { ...diff, unchanged: remainingUnchanged, deactivated }, nextEntries: Array.from(byKey.values()) };
}

/** Fixture-backed implementation of ApiClient (CONTRACT.md §10: "staví proti
 *  zmrazeným JSON fixturám ... ne proti živému backendu"). Reads come
 *  straight from src/fixtures/*.json; writes mutate an in-memory clone so the
 *  UI is genuinely interactive without a backend. Never used when
 *  VITE_QA_USE_FIXTURES=false (see ../config.ts). */
export function createMockClient(): ApiClient {
  const state = new Map<string, SchoolState>();
  state.set("ZS_KAVCI_HORY", {
    domains: clone(domainsKavciHory.domains) as unknown as Domain[],
    packs: new Map([
      ["faq-provoz", { meta: clone(packsKavciHory.packs[0]) as unknown as Pack, entries: clone(entriesKavciHoryFaq.entries) as unknown as Entry[] }],
    ]),
  });
  state.set("ZS_JEDOVNICE", {
    domains: clone(domainsJedovnice.domains) as unknown as Domain[],
    packs: new Map([
      [
        "skolni-druzina",
        { meta: clone(packsJedovnice.packs[0]) as unknown as Pack, entries: clone(entriesJedovniceDruzina.entries) as unknown as Entry[] },
      ],
    ]),
  });

  let categories: Category[] = clone(categoriesFixture.categories) as unknown as Category[];
  const previews = new Map<string, PreviewRecord>();
  const importPreviews = new Map<string, ImportPreview & { school: string }>();
  const operations = new Map<string, OperationRecord>();
  const exportBlobs = new Map<string, { url: string; filename: string }>();

  // CONTRACT.md §13.2: publish and permanent delete share one lock per
  // (school, pack) for the whole duration of either operation.
  const packLocks = new Set<string>();
  function lockKey(school: string, pack: string): string {
    return `${school}::${pack}`;
  }
  function acquirePackLock(school: string, pack: string): void {
    const key = lockKey(school, pack);
    if (packLocks.has(key)) {
      throw new ApiError(409, {
        code: "PUBLISH_IN_PROGRESS",
        message: `Pro balíček ${pack} právě probíhá jiná operace (publikace nebo smazání).`,
        retryable: true,
      });
    }
    packLocks.add(key);
  }
  function releasePackLock(school: string, pack: string): void {
    packLocks.delete(lockKey(school, pack));
  }

  function entryToCsvRow(e: Entry): string[] {
    return [
      e.entry_key,
      e.canonical_q,
      e.answer_md,
      e.cat_legacy || "",
      (e.variants || []).join("\n"),
      (e.sources || []).map((s) => `${s.title} | ${s.url}`).join("\n"),
      String(e.is_active),
      (e.domains || []).join(","),
      e.category_code || "",
    ];
  }

  function requireSchool(school: string): SchoolState {
    const s = state.get(school);
    if (!s) throw new ApiError(404, { code: "NOT_FOUND", message: `Neznámá nebo neaktivní škola ${school}.`, retryable: false });
    return s;
  }

  function requirePack(school: string, pack: string): PackState {
    const s = requireSchool(school);
    const p = s.packs.get(pack);
    if (!p) throw new ApiError(404, { code: "NOT_FOUND", message: `Neznámý balíček ${pack}.`, retryable: false });
    return p;
  }

  function csvRowToWrite(record: Record<string, string>): { write: EntryWrite; legacyCat: string } {
    const domains = (record.domains || "")
      .split(",")
      .map((d) => d.trim())
      .filter(Boolean);
    const variants = (record.variants || "").split("\n").map((v) => v.trim()).filter(Boolean);
    const sources = (record.sources || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const [title, url] = line.split("|").map((part) => part.trim());
        return { title: title || line, url: url || "" };
      });
    const categoryCode = (record.category_code ?? "").trim();
    return {
      write: {
        entry_key: record.entry_key?.trim() || null,
        canonical_q: record.canonical_q || "",
        answer_md: record.answer_md || "",
        variants,
        sources,
        domains,
        category_code: categoryCode ? categoryCode : null,
        is_active: record.is_active === "" || record.is_active === undefined ? true : record.is_active === "true",
      },
      legacyCat: record.cat || "",
    };
  }

  function buildCsvPreview(
    school: string,
    packId: string,
    csvText: string,
    mode: "merge" | "replace",
    packMetadataByPack?: Record<string, PackMetadata>,
  ): ImportPreview {
    const rows = parseCsv(csvText);
    const records = rowsToRecords(rows);
    const s = requireSchool(school);
    const existing = s.packs.get(packId);
    const knownCodes = new Set(categories.map((c) => c.code));
    const errors: NonNullable<ImportPreview["packs"][number]["errors"]> = [];
    const legacyCatUnresolved: NonNullable<ImportPreview["packs"][number]["legacy_cat_unresolved"]> = [];
    const writes: EntryWrite[] = [];
    const domainCatalog = new Set(s.domains.map((d) => d.code));

    records.forEach((record, index) => {
      const rowNumber = index + 2; // header is row 1
      const { write, legacyCat } = csvRowToWrite(record);
      if (!write.canonical_q.trim() || !write.answer_md.trim()) {
        errors.push({ file: "import.csv", row: rowNumber, field: "canonical_q", code: "VALIDATION_ERROR", message: "Otázka i odpověď jsou povinné." });
        return;
      }
      const unknownDomain = write.domains.find((d) => !domainCatalog.has(d));
      if (unknownDomain) {
        errors.push({
          file: "import.csv",
          row: rowNumber,
          field: "domains",
          code: "UNKNOWN_DOMAIN",
          message: `Doména '${unknownDomain}' není v katalogu školy.`,
        });
        return;
      }
      if (write.category_code && !knownCodes.has(write.category_code)) {
        errors.push({
          file: "import.csv",
          row: rowNumber,
          field: "category_code",
          code: "UNKNOWN_CATEGORY",
          message: `Kategorie '${write.category_code}' neexistuje v katalogu.`,
        });
        return;
      }
      if (!write.category_code && legacyCat && !("category_code" in record)) {
        legacyCatUnresolved.push({ entry_key: write.entry_key || "", cat: legacyCat });
      }
      writes.push(write);
    });

    // CONTRACT.md §5: a single CSV with a bad row answers 422 with `fields`
    // — never a usable ok:false preview the admin could still publish. The
    // per-pack ok/errors shape below is for ZIP imports, where each pack in
    // the archive gets its OWN result inside one 200 response.
    if (errors.length > 0) {
      throw new ApiError(422, {
        code: "VALIDATION_ERROR",
        message: "Soubor obsahuje neplatné řádky.",
        retryable: false,
        fields: errors.map((e) => ({ field: e.field || "", message: e.message || "", file: e.file, row: e.row })),
      });
    }

    const base = existing ? existing.entries : [];
    const { diff } = computeImportDiff(base, writes, mode);
    const previewId = randomId();
    // row_count is not part of the frozen ImportPackResult type (see
    // ImportDialog.tsx's PackResultWithCount) — attached here as a plain
    // extra field so the fixture adapter can demonstrate F-C4a-1.
    const packResult: ImportPreview["packs"][number] = {
      pack_id: packId,
      file: "import.csv",
      base_etag: existing ? existing.meta.pack_etag : "",
      is_new_pack: !existing,
      diff,
      ok: true,
      errors: [],
      legacy_cat_unresolved: legacyCatUnresolved,
      row_count: records.length,
    };

    // CONTRACT.md §2.4c: same validation and same explicit confirmation as
    // a pack founded in the dialog — never inherited from a default or a
    // manifest. Any top-level error here (unknown pack_id key, metadata for
    // an existing pack, an invalid metadata shape) is a 422 for the WHOLE
    // preview, same as the row errors above — never a partial 200.
    const { fieldErrors, confirmed } = resolveImportMetadataMock([packResult], packMetadataByPack || {});
    if (fieldErrors.length > 0) {
      throw new ApiError(422, {
        code: "VALIDATION_ERROR",
        message: "pack_metadata_by_pack did not pass validation",
        retryable: false,
        fields: fieldErrors,
      });
    }
    annotateMetadataFlags([packResult], confirmed);

    const preview: ImportPreview & { school: string } = {
      preview_id: previewId,
      source_kind: "csv",
      mode,
      school,
      packs: [packResult],
      expires_at: new Date(Date.now() + 15 * 60000).toISOString(),
    };
    importPreviews.set(previewId, preview);
    // Store the parsed writes so publishImport can apply them without
    // re-parsing; keyed on the same preview_id. metadataRequired travels
    // along so publishImport can refuse this pack truthfully instead of
    // silently applying writes with no confirmed metadata.
    pendingImportWrites.set(previewId, {
      school,
      packId,
      writes,
      mode,
      metadataRequired: packResult.metadata_required === true,
    });
    return preview;
  }

  const pendingImportWrites = new Map<
    string,
    { school: string; packId: string; writes: EntryWrite[]; mode: "merge" | "replace"; metadataRequired: boolean }
  >();

  function advanceOperation(op: OperationRecord): OperationRecord {
    if (op.status === "completed" || op.status === "failed" || op.status === "unknown") return op;
    if (op._pollsRemaining > 0) {
      op._pollsRemaining -= 1;
      op.status = "processing";
      return op;
    }
    // CONTRACT.md §6: `completed` only when NO pack ended `failed` — a
    // pack already marked failed (e.g. PACK_METADATA_REQUIRED, §2.4c) must
    // flip the whole operation, never be silently reported as success.
    const failedCount = op.packs?.filter((p) => p.status === "failed").length ?? 0;
    if (failedCount > 0) {
      op.status = "failed";
      op.error = {
        code: "IMPORT_PACKS_FAILED",
        message: `${failedCount} of ${op.packs!.length} packs failed`,
        retryable: true,
      };
    } else {
      op.status = "completed";
    }
    op.finished_at = new Date().toISOString();
    return op;
  }

  async function publishPackChangesLocked(
    school: string,
    pack: string,
    body: { preview_id: string; expected_etag: string },
  ): Promise<Operation> {
    await delay();
    const s = requireSchool(school);
    const existing = s.packs.get(pack);
    const saved = previews.get(body.preview_id);
    if (!saved || saved.school !== school || saved.pack !== pack) {
      throw new ApiError(404, { code: "NOT_FOUND", message: "Náhled nenalezen nebo vypršel.", retryable: false });
    }

    if (existing) {
      if (existing.meta.metadata_uniform === false) {
        throw new ApiError(409, {
          code: "PACK_METADATA_DIVERGENT",
          message: "Položky balíčku mají různá metadata. Publikace je odmítnuta.",
          retryable: false,
        });
      }
      if (body.expected_etag !== existing.meta.pack_etag) {
        throw new ApiError(409, clone(conflictFixture).error);
      }
    } else if (body.expected_etag !== "") {
      throw new ApiError(409, clone(conflictFixture).error);
    }

    const operationId = randomId();
    const operation: OperationRecord = {
      operation_id: operationId,
      status: "accepted",
      school,
      pack_id: pack,
      preview_id: body.preview_id,
      started_at: new Date().toISOString(),
      finished_at: null,
      result_etag: null,
      _pollsRemaining: 2,
    };
    operations.set(operationId, operation);
    previews.delete(body.preview_id);

    // The mock commits the change now but only REPORTS completion after a
    // couple of polls (advanceOperation) — callers must still wait for
    // GET /operations/{id} to say "completed", exactly as against the real
    // backend (CONTRACT.md §6).
    if (existing) {
      existing.entries = saved.nextEntries;
      existing.meta.version += 1;
      existing.meta.pack_etag = computeMockEtag(existing.entries, existing.meta.version);
      existing.meta.entry_count = existing.entries.filter((e) => e.is_active).length;
      operation.result_etag = existing.meta.pack_etag;
    } else {
      // First publish of a brand-new pack: the manifest row is created
      // now, never earlier (CONTRACT.md §2). Metadata comes ONLY from the
      // stored preview, never from this publish request body.
      const version = 1;
      const meta: Pack = {
        pack_id: pack,
        version,
        pack_etag: computeMockEtag(saved.nextEntries, version),
        lang: "cs",
        built_at: new Date().toISOString(),
        entry_count: saved.nextEntries.filter((e) => e.is_active).length,
        metadata_uniform: true,
        publishable: true,
      };
      s.packs.set(pack, { meta, entries: saved.nextEntries });
      operation.result_etag = meta.pack_etag;
    }

    return clone(operation);
  }

  return {
    async getContext() {
      await delay();
      return clone(contextFixture) as unknown as Context;
    },

    async listSchools() {
      await delay();
      return clone(schoolsFixture.schools) as unknown as School[];
    },

    async listSchoolDomains(school) {
      await delay();
      return clone(requireSchool(school).domains);
    },

    async listCategories(params) {
      await delay();
      const includeInactive = params?.include_inactive ?? false;
      return clone(categories.filter((c) => includeInactive || c.is_active));
    },

    async createCategory(body) {
      await delay();
      if (categories.some((c) => c.code === body.code)) {
        throw new ApiError(409, { code: "CATEGORY_CODE_EXISTS", message: `Kód ${body.code} už existuje.`, retryable: false });
      }
      const created: Category = {
        code: body.code,
        label: body.labels.cs || body.labels.en || body.code,
        label_fallback: !body.labels.cs,
        labels: body.labels,
        is_active: true,
        sort_order: body.sort_order ?? categories.length + 1,
        notes: body.notes ?? null,
      };
      categories = [...categories, created];
      return clone(created);
    },

    async listPacks(school) {
      await delay();
      return clone(Array.from(requireSchool(school).packs.values()).map((p) => p.meta));
    },

    async createPack(school, body) {
      await delay();
      const s = requireSchool(school);
      // CONTRACT.md §7: the canonical code is PACK_ID_TAKEN, not PACK_EXISTS.
      if (s.packs.has(body.pack_id)) {
        throw new ApiError(409, { code: "PACK_ID_TAKEN", message: `Balíček ${body.pack_id} už ve škole existuje.`, retryable: false });
      }
      // Optional immediate check against the code lists (CONTRACT.md §2.4a);
      // nothing is stored — the binding copy travels later to /preview.
      if (body.metadata) {
        const fieldErrors = validatePackMetadataMock(body.metadata);
        if (fieldErrors.length > 0) {
          throw new ApiError(422, {
            code: "VALIDATION_ERROR",
            message: "pack_metadata did not pass validation",
            retryable: false,
            fields: fieldErrors,
          });
        }
      }
      return { pack_id: body.pack_id, available: true };
    },

    async getMetadataOptions() {
      await delay();
      return clone(metadataOptionsFixture) as unknown as MetadataOptions;
    },

    async listEntries(school, pack, params) {
      await delay();
      const p = requirePack(school, pack);
      const q = (params?.q || "").toLocaleLowerCase("cs");
      const filtered = q ? p.entries.filter((e) => (e.canonical_q + " " + e.answer_md).toLocaleLowerCase("cs").includes(q)) : p.entries;
      return {
        entries: clone(filtered),
        next_cursor: null,
        total: p.entries.length,
        pack_etag: p.meta.pack_etag,
        pack_version: p.meta.version,
        metadata_uniform: p.meta.metadata_uniform ?? true,
      } as unknown as EntryPage;
    },

    async getEntry(school, pack, entryKey) {
      await delay();
      const p = requirePack(school, pack);
      const entry = p.entries.find((e) => e.entry_key === entryKey);
      if (!entry) throw new ApiError(404, { code: "NOT_FOUND", message: "Otázka nenalezena.", retryable: false });
      return { entry: clone(entry), pack_etag: p.meta.pack_etag, pack_version: p.meta.version };
    },

    async previewPackChanges(school, pack, body) {
      await delay();
      const s = requireSchool(school);
      const existing = s.packs.get(pack);

      // CONTRACT.md §2.4a: pack_metadata is REQUIRED for a pack with no
      // server state and FORBIDDEN (422) for one that already exists — one
      // metadata set applies to the whole pack, so accepting it here would
      // silently rewrite an existing pack's visibility.
      if (!existing) {
        if (!body.pack_metadata) {
          throw new ApiError(422, {
            code: "VALIDATION_ERROR",
            message: "pack_metadata is required for a pack that does not exist yet",
            retryable: false,
            fields: [{ field: "pack_metadata", message: "required for a new pack" }],
          });
        }
        const fieldErrors = validatePackMetadataMock(body.pack_metadata);
        if (fieldErrors.length > 0) {
          throw new ApiError(422, {
            code: "VALIDATION_ERROR",
            message: "pack_metadata did not pass validation",
            retryable: false,
            fields: fieldErrors,
          });
        }
        if (body.base_etag !== "") {
          throw new ApiError(409, clone(conflictFixture).error);
        }
        const { diff, nextEntries } = diffEntries([], body.entries);
        const previewId = randomId();
        previews.set(previewId, { school, pack, nextEntries, baseEtag: "", newPackMetadata: body.pack_metadata });
        return {
          preview_id: previewId,
          base_etag: "",
          diff,
          warnings: [],
          expires_at: new Date(Date.now() + 15 * 60000).toISOString(),
        } as unknown as Preview;
      }

      if (body.pack_metadata) {
        throw new ApiError(422, {
          code: "VALIDATION_ERROR",
          message: "pack_metadata is not allowed for an existing pack",
          retryable: false,
          fields: [{ field: "pack_metadata", message: "not allowed for an existing pack" }],
        });
      }
      if (body.base_etag !== existing.meta.pack_etag) {
        throw new ApiError(409, clone(conflictFixture).error);
      }
      const { diff, nextEntries } = diffEntries(existing.entries, body.entries);
      const previewId = randomId();
      previews.set(previewId, { school, pack, nextEntries, baseEtag: body.base_etag });
      return {
        preview_id: previewId,
        base_etag: body.base_etag,
        diff,
        warnings: [],
        expires_at: new Date(Date.now() + 15 * 60000).toISOString(),
      } as unknown as Preview;
    },

    async publishPackChanges(school, pack, body) {
      // Held for the whole publish (CONTRACT.md §13.2) so a concurrent
      // permanent delete on the same (school, pack) sees PUBLISH_IN_PROGRESS.
      acquirePackLock(school, pack);
      try {
        return await publishPackChangesLocked(school, pack, body);
      } finally {
        releasePackLock(school, pack);
      }
    },

    async previewImport(school, file, opts) {
      await delay();
      requireSchool(school);
      if (file.name.toLowerCase().endsWith(".zip")) {
        // The mock does not parse the uploaded ZIP's actual bytes (same
        // limitation as createExport's ZIP branch below) — it demonstrates
        // §2.4c's metadata rules against the static fixture's packs instead
        // of against whatever the admin actually picked.
        const fixture = clone(importPreviewZipFixture) as unknown as ImportPreview;
        const { fieldErrors, confirmed } = resolveImportMetadataMock(fixture.packs, opts.pack_metadata_by_pack || {});
        if (fieldErrors.length > 0) {
          throw new ApiError(422, {
            code: "VALIDATION_ERROR",
            message: "pack_metadata_by_pack did not pass validation",
            retryable: false,
            fields: fieldErrors,
          });
        }
        annotateMetadataFlags(fixture.packs, confirmed);
        return fixture;
      }
      const text = await file.text();
      return buildCsvPreview(school, opts.pack_id || "faq-provoz", text, opts.mode ?? "merge", opts.pack_metadata_by_pack);
    },

    async publishImport(school, previewId, body) {
      await delay();
      const stored = pendingImportWrites.get(previewId);
      if (!stored || stored.school !== school) {
        throw new ApiError(404, { code: "NOT_FOUND", message: "Náhled importu nenalezen nebo vypršel.", retryable: false });
      }
      const s = requireSchool(school);
      // CONTRACT.md §2.4c: a new pack that never got confirmed metadata is
      // refused truthfully — no entries are written and no pack is created.
      if (stored.metadataRequired) {
        pendingImportWrites.delete(previewId);
        importPreviews.delete(previewId);
        const operationId = randomId();
        const operation: OperationRecord = {
          operation_id: operationId,
          status: "accepted",
          school,
          pack_id: stored.packId,
          preview_id: previewId,
          started_at: new Date().toISOString(),
          finished_at: null,
          result_etag: null,
          packs: [{ pack_id: stored.packId, status: "failed", message: "PACK_METADATA_REQUIRED" }],
          _pollsRemaining: 2,
        };
        operations.set(operationId, operation);
        return clone(operation);
      }
      let p = s.packs.get(stored.packId);
      const expectedEtag = body.expected_etags[stored.packId] ?? "";
      if (p && expectedEtag !== p.meta.pack_etag) {
        throw new ApiError(409, clone(conflictFixture).error);
      }
      if (!p) {
        p = {
          meta: {
            pack_id: stored.packId,
            version: 0,
            pack_etag: "",
            lang: "cs",
            built_at: null,
            entry_count: 0,
            metadata_uniform: true,
            publishable: true,
          },
          entries: [],
        };
        s.packs.set(stored.packId, p);
      }
      const { nextEntries } = computeImportDiff(p.entries, stored.writes, stored.mode);
      p.entries = nextEntries;
      p.meta.version += 1;
      p.meta.pack_etag = computeMockEtag(p.entries, p.meta.version);
      p.meta.entry_count = p.entries.filter((e) => e.is_active).length;
      pendingImportWrites.delete(previewId);
      importPreviews.delete(previewId);

      const operationId = randomId();
      const operation: OperationRecord = {
        operation_id: operationId,
        status: "accepted",
        school,
        pack_id: stored.packId,
        preview_id: previewId,
        started_at: new Date().toISOString(),
        finished_at: null,
        result_etag: p.meta.pack_etag,
        packs: [{ pack_id: stored.packId, status: "pending" }],
        _pollsRemaining: 2,
      };
      operations.set(operationId, operation);
      return clone(operation);
    },

    async getOperation(operationId) {
      await delay(200);
      const op = operations.get(operationId);
      if (!op) throw new ApiError(404, { code: "NOT_FOUND", message: "Operace nenalezena.", retryable: false });
      advanceOperation(op);
      if ((op.status === "completed" || op.status === "failed") && op.packs) {
        // A pack already marked "failed" (e.g. PACK_METADATA_REQUIRED, §2.4c)
        // keeps that status — completion only resolves genuinely pending ones.
        op.packs = op.packs.map((entry) => (entry.status === "failed" ? entry : { ...entry, status: "ok" }));
        op.summary = { added: 0, updated: 0, deactivated: 0, untouched: 0 };
      }
      return clone(op);
    },

    async deleteEntry(school, pack, entryKey) {
      acquirePackLock(school, pack);
      try {
        await delay();
        const p = requirePack(school, pack);
        const before = p.entries.length;
        const target = p.entries.find((e) => e.entry_key === entryKey);
        if (!target) {
          throw new ApiError(404, { code: "NOT_FOUND", message: "Otázka nenalezena.", retryable: false });
        }
        p.entries = p.entries.filter((e) => e.entry_key !== entryKey);
        p.meta.entry_count = p.entries.filter((e) => e.is_active).length;
        // CONTRACT.md §13.2: a stale stored preview must not resurrect what
        // was just permanently deleted — drop every preview for this pack.
        for (const [id, rec] of previews) if (rec.school === school && rec.pack === pack) previews.delete(id);
        return { deleted: { entries: before - p.entries.length, variants: 1, embeddings: 1, manifests: 0 } };
      } finally {
        releasePackLock(school, pack);
      }
    },

    async deletePack(school, pack, body) {
      acquirePackLock(school, pack);
      try {
        await delay();
        const s = requireSchool(school);
        const p = s.packs.get(pack);
        if (!p) throw new ApiError(404, { code: "NOT_FOUND", message: `Neznámý balíček ${pack}.`, retryable: false });
        // CONTRACT.md §13.2: the admin must type the exact pack id.
        if (body.confirm_pack_id !== pack) {
          throw new ApiError(409, {
            code: "DELETE_CONFIRMATION_MISMATCH",
            message: "Opsaný identifikátor balíčku nesouhlasí. Smazání bylo odmítnuto.",
            retryable: false,
          });
        }
        const entryCount = p.entries.length;
        s.packs.delete(pack);
        for (const [id, rec] of previews) if (rec.school === school && rec.pack === pack) previews.delete(id);
        return { deleted: { entries: entryCount, variants: entryCount, embeddings: entryCount, manifests: 1 } };
      } finally {
        releasePackLock(school, pack);
      }
    },

    async createExport(school, body) {
      await delay();
      const s = requireSchool(school);
      const format = body.format ?? "zip";
      if (format === "csv" && body.pack_ids.length !== 1) {
        throw new ApiError(422, {
          code: "VALIDATION_ERROR",
          message: "CSV export je možný jen pro jeden balíček najednou.",
          retryable: false,
          fields: [{ field: "pack_ids", message: "Zvolte právě jeden balíček, nebo přepněte na ZIP." }],
        });
      }
      for (const id of body.pack_ids) requirePack(school, id);

      const exportId = randomId();
      if (format === "csv") {
        const pack = s.packs.get(body.pack_ids[0])!;
        const rows = [CSV_COLUMNS, ...pack.entries.map((e) => entryToCsvRow(e))];
        const blob = new Blob([serializeCsv(rows)], { type: "text/csv;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        exportBlobs.set(exportId, { url, filename: `${body.pack_ids[0]}.csv` });
        return {
          export_id: exportId,
          format: "csv",
          download_url: url,
          filename: `${body.pack_ids[0]}.csv`,
          expires_at: new Date(Date.now() + 15 * 60000).toISOString(),
          note: "CSV export není úplná záloha balíčku — neobsahuje všechna packová metadata.",
        };
      }
      // ZIP: the mock does not actually zip in-browser; it hands back one
      // CSV as a stand-in so the download link is real, and says so.
      const firstPack = s.packs.get(body.pack_ids[0])!;
      const rows = [CSV_COLUMNS, ...firstPack.entries.map((e) => entryToCsvRow(e))];
      const blob = new Blob([serializeCsv(rows)], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      exportBlobs.set(exportId, { url, filename: `${school}.zip` });
      return {
        export_id: exportId,
        format: "zip",
        download_url: url,
        filename: `${school}.zip`,
        expires_at: new Date(Date.now() + 15 * 60000).toISOString(),
        note: "Mock režim: ZIP se v prohlížeči nesestavuje; stažený soubor je ukázkové CSV prvního balíčku.",
      };
    },

    downloadExportUrl(exportId) {
      return exportBlobs.get(exportId)?.url ?? "#";
    },
  } satisfies ApiClient;
}
