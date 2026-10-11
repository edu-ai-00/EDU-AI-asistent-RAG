import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ApiClient } from "../api/client";
import { ApiError, type ImportPreview, type Pack } from "../api/types";
import { strings } from "../strings.cs";
import { ImportDialog } from "../components/ImportDialog";
import { ExportDialog } from "../components/ExportDialog";

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

/** Hand-rolled stub client: only the methods a given test cares about are
 *  supplied, everything else throws loudly if accidentally called. Keeps
 *  each scenario in full control of exactly what the "server" answers,
 *  independent of mockClient.ts's own fixture wiring. */
function makeClient(overrides: Partial<ApiClient>): ApiClient {
  return {
    getContext: notImplemented("getContext"),
    listSchools: notImplemented("listSchools"),
    listSchoolDomains: notImplemented("listSchoolDomains"),
    listCategories: notImplemented("listCategories"),
    createCategory: notImplemented("createCategory"),
    listPacks: notImplemented("listPacks"),
    createPack: notImplemented("createPack"),
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

const emptyDiff = { added: [], changed: [], unchanged: [], deactivated: [] };

function csvFile(name = "import.csv"): File {
  return new File(["entry_key;canonical_q;answer_md\n"], name, { type: "text/csv" });
}

function zipFile(name = "import.zip"): File {
  return new File([new Uint8Array([1, 2, 3])], name, { type: "application/zip" });
}

async function chooseFile(file: File) {
  const input = screen.getByLabelText(strings.importDialog.fileLabel) as HTMLInputElement;
  fireEvent.change(input, { target: { files: [file] } });
}

const PACKS: Pack[] = [
  { pack_id: "faq-provoz", version: 3, pack_etag: "sha256:aaa", entry_count: 4 },
  { pack_id: "skolni-druzina", version: 1, pack_etag: "sha256:bbb", entry_count: 2 },
];

describe("import: CSV preview, publish, and reload (A)", () => {
  it("renders per-pack row_count and the four diff counts, then polls to completed and reloads", async () => {
    const preview: ImportPreview = {
      preview_id: "prev-1",
      source_kind: "csv",
      mode: "merge",
      packs: [
        {
          pack_id: "faq-provoz",
          file: "faq-provoz.csv",
          base_etag: "sha256:aaa",
          is_new_pack: false,
          diff: { added: [{ entry_key: "a1", canonical_q: "Nová otázka?" }], changed: [], unchanged: ["b1"], deactivated: [] },
          ok: true,
          errors: [],
          legacy_cat_unresolved: [],
          // row_count: not part of the frozen ImportPackResult type — the
          // fixture/stub attaches it at runtime, same as mockClient.ts does.
          ...({ row_count: 2 } as Record<string, unknown>),
        },
      ],
      expires_at: new Date(Date.now() + 900000).toISOString(),
    } as unknown as ImportPreview;

    const onImported = vi.fn();
    let publishCalls = 0;
    let opCalls = 0;
    const client = makeClient({
      previewImport: async () => preview,
      publishImport: async (_school, previewId, body) => {
        publishCalls += 1;
        expect(previewId).toBe("prev-1");
        expect(body.expected_etags).toEqual({ "faq-provoz": "sha256:aaa" });
        return { operation_id: "op-1", status: "accepted", school: "ZS_X" };
      },
      getOperation: async () => {
        opCalls += 1;
        return { operation_id: "op-1", status: "completed", school: "ZS_X" };
      },
    });

    render(
      <ImportDialog client={client} school="ZS_X" packs={PACKS} initialPackId="faq-provoz" onClose={() => {}} onImported={onImported} />,
    );

    await chooseFile(csvFile());
    fireEvent.click(screen.getByRole("button", { name: strings.importDialog.check }));

    expect(await screen.findByText(strings.importDialog.rowCountTemplate(2))).toBeTruthy();
    const diffSummary = document.querySelector(".diff-summary") as HTMLElement;
    expect(diffSummary.textContent).toContain("Nová otázka?");

    fireEvent.click(screen.getByRole("button", { name: strings.importDialog.publishSelected }));

    await waitFor(() => {
      expect(screen.getByText(strings.operation.completed)).toBeTruthy();
      expect(onImported).toHaveBeenCalledTimes(1);
    });
    expect(publishCalls).toBe(1);
    expect(opCalls).toBeGreaterThan(0);
  });
});

describe("import: F-C4a-1 row_count vs empty file (B)", () => {
  it("reads an all-zero diff with row_count > 0 as 'N rows, nothing changes' — not empty/failed", async () => {
    const preview = {
      preview_id: "prev-2",
      source_kind: "csv",
      mode: "merge",
      packs: [
        {
          pack_id: "faq-provoz",
          file: "faq-provoz.csv",
          base_etag: "sha256:aaa",
          is_new_pack: false,
          diff: emptyDiff,
          ok: true,
          errors: [],
          legacy_cat_unresolved: [],
          row_count: 12,
        },
      ],
      expires_at: new Date(Date.now() + 900000).toISOString(),
    } as unknown as ImportPreview;

    const client = makeClient({ previewImport: async () => preview });
    render(<ImportDialog client={client} school="ZS_X" packs={PACKS} initialPackId="faq-provoz" onClose={() => {}} onImported={() => {}} />);

    await chooseFile(csvFile());
    fireEvent.click(screen.getByRole("button", { name: strings.importDialog.check }));

    expect(await screen.findByText(strings.importDialog.noRowsChangeTemplate(12))).toBeTruthy();
    expect(screen.queryByText(strings.importDialog.emptyFile)).toBeNull();
    expect(screen.queryByText(strings.importDialog.rowCountTemplate(12))).toBeNull();
  });

  it("reads row_count === 0 as the distinct empty-file message", async () => {
    const preview = {
      preview_id: "prev-3",
      source_kind: "csv",
      mode: "merge",
      packs: [
        {
          pack_id: "faq-provoz",
          file: "faq-provoz.csv",
          base_etag: "sha256:aaa",
          is_new_pack: false,
          diff: emptyDiff,
          ok: true,
          errors: [],
          legacy_cat_unresolved: [],
          row_count: 0,
        },
      ],
      expires_at: new Date(Date.now() + 900000).toISOString(),
    } as unknown as ImportPreview;

    const client = makeClient({ previewImport: async () => preview });
    render(<ImportDialog client={client} school="ZS_X" packs={PACKS} initialPackId="faq-provoz" onClose={() => {}} onImported={() => {}} />);

    await chooseFile(csvFile());
    fireEvent.click(screen.getByRole("button", { name: strings.importDialog.check }));

    expect(await screen.findByText(strings.importDialog.emptyFile)).toBeTruthy();
    expect(screen.queryByText(strings.importDialog.noRowsChangeTemplate(0))).toBeNull();
  });
});

describe("import: ZIP with one ok and one failing pack (C)", () => {
  it("shows both per-pack results separately and never offers the failing pack as publishable", async () => {
    const preview = {
      preview_id: "prev-zip",
      source_kind: "zip",
      mode: "merge",
      packs: [
        {
          pack_id: "faq-provoz",
          file: "packs/faq-provoz.csv",
          base_etag: "sha256:aaa",
          is_new_pack: false,
          diff: { added: [{ entry_key: "a1", canonical_q: "Q1" }], changed: [], unchanged: [], deactivated: [] },
          ok: true,
          errors: [],
          legacy_cat_unresolved: [],
          row_count: 1,
        },
        {
          pack_id: "skolni-druzina",
          file: "packs/skolni-druzina.csv",
          base_etag: "sha256:bbb",
          is_new_pack: false,
          diff: emptyDiff,
          ok: false,
          errors: [{ file: "packs/skolni-druzina.csv", row: 3, field: "domains", message: "Doména 'JIDELNA' neexistuje." }],
          legacy_cat_unresolved: [],
          row_count: 1,
        },
      ],
      expires_at: new Date(Date.now() + 900000).toISOString(),
    } as unknown as ImportPreview;

    let publishBody: Record<string, string> | null = null;
    const client = makeClient({
      previewImport: async () => preview,
      publishImport: async (_school, _previewId, body) => {
        publishBody = body.expected_etags;
        return { operation_id: "op-9", status: "accepted", school: "ZS_X" };
      },
      getOperation: async () => ({ operation_id: "op-9", status: "completed", school: "ZS_X" }),
    });

    render(<ImportDialog client={client} school="ZS_X" packs={PACKS} initialPackId={null} onClose={() => {}} onImported={() => {}} />);

    await chooseFile(zipFile());
    fireEvent.click(screen.getByRole("button", { name: strings.importDialog.check }));

    expect(await screen.findByText("faq-provoz")).toBeTruthy();
    expect(await screen.findByText("skolni-druzina")).toBeTruthy();
    expect(screen.getByText(/Doména 'JIDELNA' neexistuje/)).toBeTruthy();
    expect(screen.getAllByText(strings.importDialog.retryHint)).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: strings.importDialog.publishSelected }));
    await waitFor(() => expect(publishBody).toEqual({ "faq-provoz": "sha256:aaa" }));
  });
});

describe("import: replace mode requires explicit confirmation (D)", () => {
  it("cannot be published until the separate confirmation is checked, and states the deactivation count", async () => {
    const preview = {
      preview_id: "prev-replace",
      source_kind: "csv",
      mode: "replace",
      packs: [
        {
          pack_id: "faq-provoz",
          file: "faq-provoz.csv",
          base_etag: "sha256:aaa",
          is_new_pack: false,
          diff: {
            added: [],
            changed: [],
            unchanged: [],
            deactivated: [
              { entry_key: "old-1", canonical_q: "Stará otázka 1" },
              { entry_key: "old-2", canonical_q: "Stará otázka 2" },
              { entry_key: "old-3", canonical_q: "Stará otázka 3" },
            ],
          },
          ok: true,
          errors: [],
          legacy_cat_unresolved: [],
          row_count: 5,
        },
      ],
      expires_at: new Date(Date.now() + 900000).toISOString(),
    } as unknown as ImportPreview;

    let publishCalls = 0;
    const client = makeClient({
      previewImport: async () => preview,
      publishImport: async (_school, _previewId, body) => {
        publishCalls += 1;
        expect(body.confirm_replace).toBe(true);
        return { operation_id: "op-r", status: "accepted", school: "ZS_X" };
      },
      getOperation: async () => ({ operation_id: "op-r", status: "completed", school: "ZS_X" }),
    });

    render(<ImportDialog client={client} school="ZS_X" packs={PACKS} initialPackId="faq-provoz" onClose={() => {}} onImported={() => {}} />);

    await chooseFile(csvFile());
    fireEvent.click(screen.getByRole("radio", { name: strings.importDialog.modeReplace }));
    fireEvent.click(screen.getByRole("button", { name: strings.importDialog.check }));

    expect(await screen.findByText(strings.importDialog.replaceDeactivateCountTemplate(3))).toBeTruthy();

    const publishButton = screen.getByRole("button", { name: strings.importDialog.publishSelected }) as HTMLButtonElement;
    expect(publishButton.disabled).toBe(true);

    fireEvent.click(screen.getByRole("checkbox", { name: strings.importDialog.confirmReplace }));
    expect(publishButton.disabled).toBe(false);

    fireEvent.click(publishButton);
    await waitFor(() => expect(screen.getByText(strings.operation.completed)).toBeTruthy());
    expect(publishCalls).toBe(1);
  });
});

describe("import: a bad CSV row answers 422 (E)", () => {
  it("renders the row/field errors and offers no publish button, and no preview", async () => {
    const client = makeClient({
      previewImport: async () => {
        throw new ApiError(422, {
          code: "VALIDATION_ERROR",
          message: "Soubor obsahuje neplatné řádky.",
          retryable: false,
          fields: [{ field: "canonical_q", message: "Otázka je povinná.", file: "faq-provoz.csv", row: 5 }],
        });
      },
    });

    render(<ImportDialog client={client} school="ZS_X" packs={PACKS} initialPackId="faq-provoz" onClose={() => {}} onImported={() => {}} />);

    await chooseFile(csvFile());
    fireEvent.click(screen.getByRole("button", { name: strings.importDialog.check }));

    expect(await screen.findByText(/Otázka je povinná/)).toBeTruthy();
    expect(screen.getByText(/řádek 5/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: strings.importDialog.publishSelected })).toBeNull();
    expect(document.querySelector(".diff-summary")).toBeNull();
  });
});

describe("export dialog", () => {
  it("only offers csv for exactly one selected pack and shows per-pack contents before download", async () => {
    let createExportBody: { pack_ids: string[]; format?: string } | null = null;
    const client = makeClient({
      createExport: async (_school, body) => {
        createExportBody = body;
        return {
          export_id: "exp-1",
          format: body.format ?? "zip",
          download_url: "blob:mock",
          filename: "export.zip",
          expires_at: new Date(Date.now() + 900000).toISOString(),
          note: "ukázkový export",
        };
      },
      downloadExportUrl: () => "blob:mock",
    });

    render(<ExportDialog client={client} school="ZS_X" packs={PACKS} initialPackId="faq-provoz" onClose={() => {}} />);

    const csvRadio = screen.getByRole("radio", { name: strings.exportDialog.formatCsv }) as HTMLInputElement;
    expect(csvRadio.checked).toBe(true);
    expect(csvRadio.disabled).toBe(false);

    // Selecting a second pack must not leave "csv" offered for a multi-pack export.
    fireEvent.click(screen.getByRole("checkbox", { name: /skolni-druzina/ }));
    expect(csvRadio.disabled).toBe(true);
    expect((screen.getByRole("radio", { name: strings.exportDialog.formatZip }) as HTMLInputElement).checked).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: strings.exportDialog.prepare }));

    await waitFor(() => expect(createExportBody).not.toBeNull());
    expect(createExportBody!.format).toBe("zip");
    expect(createExportBody!.pack_ids.sort()).toEqual(["faq-provoz", "skolni-druzina"]);

    expect(await screen.findByText("faq-provoz · 4 otázek")).toBeTruthy();
    expect(await screen.findByText("skolni-druzina · 2 otázek")).toBeTruthy();
    expect(screen.getByRole("link", { name: strings.actions.download })).toBeTruthy();
  });
});
