import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ApiClient } from "../api/client";
import { ApiError, type ImportPreview, type MetadataOptions, type Pack } from "../api/types";
import { strings } from "../strings.cs";
import { ImportDialog } from "../components/ImportDialog";

// Production polls every 1.5 s for up to three minutes (config.ts). The test
// only shortens the clock here — never the reason the shipped build hammers
// GET /operations/{id}.
vi.mock("../config", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../config")>()),
  PUBLISH_POLL_INTERVAL_MS: 5,
  PUBLISH_POLL_MAX_ATTEMPTS: 20,
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function notImplemented(name: string) {
  return (...args: unknown[]) => {
    throw new Error(`${name} not stubbed in this test (args: ${JSON.stringify(args)})`);
  };
}

/** Hand-rolled stub client, same technique as importExport.test.tsx: only
 *  the methods a given test cares about are supplied, everything else
 *  throws loudly if accidentally called. */
function makeClient(overrides: Partial<ApiClient>): ApiClient {
  return {
    getContext: notImplemented("getContext"),
    listSchools: notImplemented("listSchools"),
    listSchoolDomains: notImplemented("listSchoolDomains"),
    listCategories: notImplemented("listCategories"),
    createCategory: notImplemented("createCategory"),
    listPacks: notImplemented("listPacks"),
    createPack: notImplemented("createPack"),
    getMetadataOptions: notImplemented("getMetadataOptions"),
    listEntries: notImplemented("listEntries"),
    getEntry: notImplemented("getEntry"),
    previewPackChanges: notImplemented("previewPackChanges"),
    publishPackChanges: notImplemented("publishPackChanges"),
    previewImport: notImplemented("previewImport"),
    publishImport: notImplemented("publishImport"),
    getOperation: notImplemented("getOperation"),
    createExport: notImplemented("createExport"),
    downloadExportUrl: () => "#",
    ...overrides,
  } as unknown as ApiClient;
}

const METADATA_OPTIONS: MetadataOptions = {
  visibility: ["entity", "organization"],
  audience_roles: [
    { code: "TEACHER", label: "Učitel" },
    { code: "PARENT", label: "Rodič" },
  ],
  school_grades: [{ code: "1", label: "1. ročník" }],
};

function csvFile(name = "newpack.csv"): File {
  return new File(["entry_key;canonical_q;answer_md\n"], name, { type: "text/csv" });
}

function zipFile(name = "import.zip"): File {
  return new File([new Uint8Array([1, 2, 3])], name, { type: "application/zip" });
}

async function chooseFile(file: File) {
  const input = screen.getByLabelText(strings.importDialog.fileLabel) as HTMLInputElement;
  fireEvent.change(input, { target: { files: [file] } });
}

const PACKS: Pack[] = [{ pack_id: "faq-provoz", version: 3, pack_etag: "sha256:aaa", entry_count: 4 }];

function newPackResult(overrides: Partial<ImportPreview["packs"][number]> = {}): ImportPreview["packs"][number] {
  return {
    pack_id: "newpack",
    file: "newpack.csv",
    base_etag: "",
    is_new_pack: true,
    diff: { added: [{ entry_key: "k1", canonical_q: "Kde je sekretariat?" }], changed: [], unchanged: [], deactivated: [] },
    ok: true,
    errors: [],
    legacy_cat_unresolved: [],
    row_count: 1,
    metadata_required: true,
    publishable: false,
    ...overrides,
  } as unknown as ImportPreview["packs"][number];
}

describe("import into a not-yet-existing pack (CONTRACT.md §2.4c)", () => {
  it("asks for metadata, disables publish, then re-previews with pack_metadata_by_pack keyed by pack_id and publishes", async () => {
    const previewCalls: Array<[string, File, { pack_id?: string; mode?: string; pack_metadata_by_pack?: unknown }]> = [];
    let publishBody: { expected_etags: Record<string, string> } | null = null;

    const client = makeClient({
      getMetadataOptions: async () => METADATA_OPTIONS,
      previewImport: async (school, file, opts) => {
        previewCalls.push([school, file, opts as never]);
        if (opts.pack_metadata_by_pack) {
          return {
            preview_id: "prev-confirmed",
            source_kind: "csv",
            mode: "merge",
            packs: [newPackResult({ metadata_required: false, publishable: true })],
            expires_at: new Date(Date.now() + 900000).toISOString(),
          };
        }
        return {
          preview_id: "prev-initial",
          source_kind: "csv",
          mode: "merge",
          packs: [newPackResult()],
          expires_at: new Date(Date.now() + 900000).toISOString(),
        };
      },
      publishImport: async (_school, previewId, body) => {
        publishBody = body;
        expect(previewId).toBe("prev-confirmed");
        return { operation_id: "op-1", status: "accepted", school: "ZS_X" };
      },
      getOperation: async () => ({ operation_id: "op-1", status: "completed", school: "ZS_X" }),
    });

    render(<ImportDialog client={client} school="ZS_X" packs={PACKS} initialPackId={null} onClose={() => {}} onImported={() => {}} />);

    await chooseFile(csvFile());
    fireEvent.change(screen.getByLabelText(strings.importDialog.packIdLabel), { target: { value: "newpack" } });
    fireEvent.click(screen.getByRole("button", { name: strings.importDialog.check }));

    expect(await screen.findByText(strings.importDialog.metadataRequiredHeadingTemplate("newpack"))).toBeTruthy();
    const publishButton = (await screen.findByRole("button", { name: strings.importDialog.publishSelected })) as HTMLButtonElement;
    expect(publishButton.disabled).toBe(true);

    const confirmButton = screen.getByRole("button", { name: strings.importDialog.confirmMetadata }) as HTMLButtonElement;
    expect(confirmButton.disabled).toBe(true);

    fireEvent.click(screen.getByRole("radio", { name: strings.newPack.visibilityEntity }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Učitel" }));
    expect(confirmButton.disabled).toBe(false);

    fireEvent.click(confirmButton);

    await waitFor(() => expect(previewCalls).toHaveLength(2));
    const secondOpts = previewCalls[1][2];
    expect(secondOpts.pack_metadata_by_pack).toEqual({
      newpack: { visibility: "entity", audience_roles: ["TEACHER"], school_grade_codes: [] },
    });

    const publishButtonAfter = (await screen.findByRole("button", { name: strings.importDialog.publishSelected })) as HTMLButtonElement;
    await waitFor(() => expect(publishButtonAfter.disabled).toBe(false));

    fireEvent.click(publishButtonAfter);
    await waitFor(() => expect(screen.getByText(strings.operation.completed)).toBeTruthy());
    expect(publishBody).toEqual({ expected_etags: { newpack: "" } });
  });
});

describe("ZIP with two new packs each gets its own metadata block (per-pack keying)", () => {
  it("sends a DIFFERENT metadata object under each pack_id on the re-run preview", async () => {
    const previewCalls: Array<{ pack_metadata_by_pack?: Record<string, unknown> }> = [];

    const client = makeClient({
      getMetadataOptions: async () => METADATA_OPTIONS,
      previewImport: async (_school, _file, opts) => {
        previewCalls.push(opts as never);
        const confirmed = opts.pack_metadata_by_pack;
        return {
          preview_id: confirmed ? "prev-confirmed" : "prev-initial",
          source_kind: "zip",
          mode: "merge",
          packs: [
            newPackResult({
              pack_id: "packa",
              file: "packs/packa.csv",
              metadata_required: !confirmed?.["packa"],
              publishable: !!confirmed?.["packa"],
            }),
            newPackResult({
              pack_id: "packb",
              file: "packs/packb.csv",
              metadata_required: !confirmed?.["packb"],
              publishable: !!confirmed?.["packb"],
            }),
          ],
          expires_at: new Date(Date.now() + 900000).toISOString(),
        };
      },
      publishImport: async () => ({ operation_id: "op-9", status: "accepted", school: "ZS_X" }),
      getOperation: async () => ({ operation_id: "op-9", status: "completed", school: "ZS_X" }),
    });

    render(<ImportDialog client={client} school="ZS_X" packs={PACKS} initialPackId={null} onClose={() => {}} onImported={() => {}} />);

    await chooseFile(zipFile());
    fireEvent.click(screen.getByRole("button", { name: strings.importDialog.check }));

    expect(await screen.findByText(strings.importDialog.metadataRequiredHeadingTemplate("packa"))).toBeTruthy();
    expect(await screen.findByText(strings.importDialog.metadataRequiredHeadingTemplate("packb"))).toBeTruthy();

    const sections = document.querySelectorAll<HTMLElement>(".import-pack-metadata");
    expect(sections).toHaveLength(2);
    const [sectionA, sectionB] = Array.from(sections);

    fireEvent.click(within(sectionA).getByRole("radio", { name: strings.newPack.visibilityEntity }));
    fireEvent.click(within(sectionA).getByRole("checkbox", { name: "Učitel" }));

    fireEvent.click(within(sectionB).getByRole("radio", { name: strings.newPack.visibilityOrganization }));
    fireEvent.click(within(sectionB).getByRole("checkbox", { name: "Rodič" }));

    fireEvent.click(screen.getByRole("button", { name: strings.importDialog.confirmMetadata }));

    await waitFor(() => expect(previewCalls).toHaveLength(2));
    const secondCall = previewCalls[1];
    expect(secondCall.pack_metadata_by_pack).toEqual({
      packa: { visibility: "entity", audience_roles: ["TEACHER"], school_grade_codes: [] },
      packb: { visibility: "organization", audience_roles: ["PARENT"], school_grade_codes: [] },
    });
  });
});

describe("a 422 on the confirmed metadata re-preview is shown, not swallowed", () => {
  it("keeps the dialog open and displays the pack_metadata_by_pack.<pack_id> field message", async () => {
    let calls = 0;
    const client = makeClient({
      getMetadataOptions: async () => METADATA_OPTIONS,
      previewImport: async (_school, _file, opts) => {
        calls += 1;
        if (opts.pack_metadata_by_pack) {
          throw new ApiError(422, {
            code: "VALIDATION_ERROR",
            message: "pack_metadata_by_pack did not pass validation",
            retryable: false,
            fields: [{ field: "pack_metadata_by_pack.newpack", message: "an existing pack keeps its own metadata" }],
          });
        }
        return {
          preview_id: "prev-initial",
          source_kind: "csv",
          mode: "merge",
          packs: [newPackResult()],
          expires_at: new Date(Date.now() + 900000).toISOString(),
        };
      },
    });

    render(<ImportDialog client={client} school="ZS_X" packs={PACKS} initialPackId={null} onClose={() => {}} onImported={() => {}} />);

    await chooseFile(csvFile());
    fireEvent.change(screen.getByLabelText(strings.importDialog.packIdLabel), { target: { value: "newpack" } });
    fireEvent.click(screen.getByRole("button", { name: strings.importDialog.check }));

    await screen.findByText(strings.importDialog.metadataRequiredHeadingTemplate("newpack"));
    fireEvent.click(screen.getByRole("radio", { name: strings.newPack.visibilityEntity }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Učitel" }));
    fireEvent.click(screen.getByRole("button", { name: strings.importDialog.confirmMetadata }));

    expect(await screen.findByText(/pack_metadata_by_pack\.newpack/)).toBeTruthy();
    expect(screen.getByText(/an existing pack keeps its own metadata/)).toBeTruthy();
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(calls).toBe(2);
  });
});

describe("regression: importing into an existing pack sends no pack_metadata_by_pack", () => {
  it("omits the field entirely from the preview call and publishes normally", async () => {
    let previewOpts: { pack_metadata_by_pack?: unknown } | null = null;
    let publishBody: { expected_etags: Record<string, string> } | null = null;

    const client = makeClient({
      previewImport: async (_school, _file, opts) => {
        previewOpts = opts as never;
        return {
          preview_id: "prev-existing",
          source_kind: "csv",
          mode: "merge",
          packs: [
            {
              pack_id: "faq-provoz",
              file: "faq-provoz.csv",
              base_etag: "sha256:aaa",
              is_new_pack: false,
              diff: { added: [], changed: [], unchanged: ["b1"], deactivated: [] },
              ok: true,
              errors: [],
              legacy_cat_unresolved: [],
              row_count: 1,
              metadata_required: false,
              publishable: true,
            },
          ],
          expires_at: new Date(Date.now() + 900000).toISOString(),
        };
      },
      publishImport: async (_school, _previewId, body) => {
        publishBody = body;
        return { operation_id: "op-2", status: "accepted", school: "ZS_X" };
      },
      getOperation: async () => ({ operation_id: "op-2", status: "completed", school: "ZS_X" }),
    });

    render(<ImportDialog client={client} school="ZS_X" packs={PACKS} initialPackId="faq-provoz" onClose={() => {}} onImported={() => {}} />);

    await chooseFile(csvFile("faq-provoz.csv"));
    fireEvent.click(screen.getByRole("button", { name: strings.importDialog.check }));

    await waitFor(() => expect(previewOpts).not.toBeNull());
    expect(previewOpts).not.toHaveProperty("pack_metadata_by_pack");
    expect(screen.queryByRole("button", { name: strings.importDialog.confirmMetadata })).toBeNull();

    const publishButton = (await screen.findByRole("button", { name: strings.importDialog.publishSelected })) as HTMLButtonElement;
    expect(publishButton.disabled).toBe(false);

    fireEvent.click(publishButton);
    await waitFor(() => expect(screen.getByText(strings.operation.completed)).toBeTruthy());
    expect(publishBody).toEqual({ expected_etags: { "faq-provoz": "sha256:aaa" } });
  });
});

function existingPackResult(overrides: Partial<ImportPreview["packs"][number]> = {}): ImportPreview["packs"][number] {
  return {
    pack_id: "faq-provoz",
    file: "faq-provoz.csv",
    base_etag: "sha256:aaa",
    is_new_pack: false,
    diff: { added: [], changed: [], unchanged: ["b1"], deactivated: [] },
    ok: true,
    errors: [],
    legacy_cat_unresolved: [],
    row_count: 1,
    metadata_required: false,
    publishable: true,
    ...overrides,
  } as unknown as ImportPreview["packs"][number];
}

describe("a bulk import must never read as success when a pack failed (review finding, CONTRACT.md §6)", () => {
  it("FAILURE: the dialog shows the failure heading and the failed pack's id and message, never the success text", async () => {
    const client = makeClient({
      previewImport: async () => ({
        preview_id: "prev-fail",
        source_kind: "csv",
        mode: "merge",
        packs: [existingPackResult()],
        expires_at: new Date(Date.now() + 900000).toISOString(),
      }),
      publishImport: async () => ({ operation_id: "op-fail", status: "accepted", school: "ZS_X" }),
      getOperation: async () => ({
        operation_id: "op-fail",
        status: "failed",
        school: "ZS_X",
        error: { code: "IMPORT_PACKS_FAILED", message: "1 of 1 packs failed", retryable: true },
        packs: [{ pack_id: "faq-provoz", status: "failed", message: "PACK_METADATA_REQUIRED" }],
      }),
    });

    render(<ImportDialog client={client} school="ZS_X" packs={PACKS} initialPackId="faq-provoz" onClose={() => {}} onImported={() => {}} />);

    await chooseFile(csvFile("faq-provoz.csv"));
    fireEvent.click(screen.getByRole("button", { name: strings.importDialog.check }));

    const publishButton = (await screen.findByRole("button", { name: strings.importDialog.publishSelected })) as HTMLButtonElement;
    fireEvent.click(publishButton);

    expect(await screen.findByText(strings.importDialog.publishFailedHeading)).toBeTruthy();
    const packList = document.querySelector(".import-publish-packs") as HTMLElement;
    expect(within(packList).getByText("faq-provoz", { exact: false })).toBeTruthy();
    expect(within(packList).getByText(strings.importDialog.packMetadataRequiredMessage, { exact: false })).toBeTruthy();
    expect(screen.queryByText(strings.operation.completed)).toBeNull();
  });

  it("MIXED: two packs, one ok and one failed, are both listed with their own status and the heading is the failure one", async () => {
    const client = makeClient({
      previewImport: async () => ({
        preview_id: "prev-mixed",
        source_kind: "zip",
        mode: "merge",
        packs: [existingPackResult({ pack_id: "packa", file: "packs/packa.csv" }), existingPackResult({ pack_id: "packb", file: "packs/packb.csv" })],
        expires_at: new Date(Date.now() + 900000).toISOString(),
      }),
      publishImport: async () => ({ operation_id: "op-mixed", status: "accepted", school: "ZS_X" }),
      getOperation: async () => ({
        operation_id: "op-mixed",
        status: "failed",
        school: "ZS_X",
        error: { code: "IMPORT_PACKS_FAILED", message: "1 of 2 packs failed", retryable: true },
        packs: [
          { pack_id: "packa", status: "ok" },
          { pack_id: "packb", status: "failed", message: "pack_etag conflict (current='sha256:zzz')" },
        ],
      }),
    });

    render(<ImportDialog client={client} school="ZS_X" packs={PACKS} initialPackId={null} onClose={() => {}} onImported={() => {}} />);

    await chooseFile(zipFile());
    fireEvent.click(screen.getByRole("button", { name: strings.importDialog.check }));

    const publishButton = (await screen.findByRole("button", { name: strings.importDialog.publishSelected })) as HTMLButtonElement;
    fireEvent.click(publishButton);

    expect(await screen.findByText(strings.importDialog.publishFailedHeading)).toBeTruthy();
    const packList = document.querySelector(".import-publish-packs") as HTMLElement;
    expect(within(packList).getByText("packa", { exact: false })).toBeTruthy();
    expect(within(packList).getByText("packb", { exact: false })).toBeTruthy();
    expect(within(packList).getByText(/pack_etag conflict/)).toBeTruthy();
    expect(screen.queryByText(strings.operation.completed)).toBeNull();

    const failedItems = packList.querySelectorAll(".import-publish-pack-failed");
    expect(failedItems).toHaveLength(1);
    const okItems = packList.querySelectorAll(".import-publish-pack:not(.import-publish-pack-failed)");
    expect(okItems).toHaveLength(1);
  });
});

