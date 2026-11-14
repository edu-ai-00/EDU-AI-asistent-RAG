import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ApiClient } from "../api/client";
import type { DocumentChunk, DocumentDetail, DocumentJob, DocumentOptions, DocumentStats, School, SchoolPermissions } from "../api/types";
import { createMockClient } from "../api/mockClient";
import { DocumentsPage } from "../components/DocumentsPage";
import { DocumentUploadDialog } from "../components/DocumentUploadDialog";
import { CHUNK_CLIP } from "../components/DocumentContentTab";
import { strings } from "../strings.cs";
import optionsFixture from "../fixtures/documentOptions.json";

vi.mock("../config", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../config")>()),
  DOCUMENTS_SEARCH_DEBOUNCE_MS: 5,
  DOCUMENT_JOBS_POLL_INTERVAL_MS: 15,
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const t = strings.documents;
const dd = strings.documentDetail;
const ALL: SchoolPermissions = { read: true, publish: true, ingest: true, deactivate: true, delete_permanently: true };
const KAVCI: School = { code: "ZS_KAVCI_HORY", name: "ZŠ Kavčí hory", permissions: ALL };
const WEB_RULES = "https://www.zs-kavcihory.example/skolni-rad";
const CANTEEN = "Provozni_rad_jidelny.pdf";
const RULES_VERSION_NEW = "0b6f2c1e-6a4d-4c1b-9a51-2d1f3e5a7b01";
const RULES_VERSION_OLD = "0b6f2c1e-6a4d-4c1b-9a51-2d1f3e5a7b00";

const STATS: DocumentStats = { active: 12, inactive: 3, expired: 2, expiring_30: 4, future: 1, qa_entries_active: 220 };

// The jobs panel can list the same source, so pick the row that has the action buttons.
function row(title: string): HTMLElement {
  const rows = screen.getAllByText(title).map((el) => el.closest("tr") as HTMLElement);
  return rows.find((tr) => tr.querySelector(".doc-actions")) as HTMLElement;
}

function openRowMenu(title: string): HTMLElement {
  fireEvent.click(within(row(title)).getByRole("button", { name: t.moreActionsFor(title) }));
  return screen.getByRole("menu");
}

async function renderPage(client: ApiClient, school: School = KAVCI) {
  render(<DocumentsPage client={client} school={school} uiDefaultLang="cs" />);
  await screen.findByRole("table");
}

const tile = (label: string) => screen.getByRole("button", { name: new RegExp(label) });

describe("overview tiles (§14.12)", () => {
  it("shows the four document counts from documents/stats", async () => {
    const real = createMockClient();
    const getDocumentStats = vi.fn(async () => STATS);
    await renderPage({ ...real, getDocumentStats });
    const tiles = await screen.findByRole("region", { name: t.statsLabel });
    await waitFor(() => expect(within(tiles).getByText("12")).toBeTruthy());
    expect(tile(t.tileActive).textContent).toContain("12");
    expect(tile(t.tileInactive).textContent).toContain("3");
    expect(tile(t.tileExpired).textContent).toContain("2");
    expect(tile(t.tileExpiring).textContent).toContain("4");
    expect(within(tiles).queryByText("220")).toBeNull();
    // QA counts belong to the QA page, not to the documents overview.
    expect(within(tiles).queryByText(t.tileQa)).toBeNull();
    for (const icon of ["📄", "⏸️", "⌛", "⏰"]) expect(tiles.textContent).toContain(icon);
    expect(getDocumentStats).toHaveBeenCalledWith("ZS_KAVCI_HORY", expect.anything());
  });

  it("a tile sets the matching status and validity filter and shows it as pressed; pressing again resets", async () => {
    const real = createMockClient();
    const listDocuments = vi.fn(real.listDocuments);
    await renderPage({ ...real, getDocumentStats: async () => STATS, listDocuments });
    const status = screen.getByLabelText(t.filterLabel) as HTMLSelectElement;
    const validity = screen.getByLabelText(t.filterValidityLabel) as HTMLSelectElement;
    const lastParams = () => listDocuments.mock.calls[listDocuments.mock.calls.length - 1][1];
    expect(tile(t.tileActive).getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(tile(t.tileExpiring));
    await waitFor(() => expect(lastParams()).toMatchObject({ status: "active", validity: "expiring" }));
    expect(validity.value).toBe("expiring");
    expect(tile(t.tileExpiring).getAttribute("aria-pressed")).toBe("true");
    expect(tile(t.tileActive).getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(tile(t.tileExpired));
    await waitFor(() => expect(lastParams()).toMatchObject({ status: "active", validity: "expired" }));
    expect(status.value).toBe("active");

    fireEvent.click(tile(t.tileInactive));
    await waitFor(() => expect(lastParams()).toMatchObject({ status: "inactive" }));
    expect(lastParams()?.validity).toBeUndefined();
    expect(status.value).toBe("inactive");
    expect(validity.value).toBe("all");
    expect(await screen.findByText("Jidelnicek_zari.docx")).toBeTruthy();

    fireEvent.click(tile(t.tileInactive));
    await waitFor(() => expect(status.value).toBe("active"));
    expect(tile(t.tileActive).getAttribute("aria-pressed")).toBe("true");
  });

  it("offers 'Brzy vyprší' in the validity filter and sends validity=expiring", async () => {
    const real = createMockClient();
    const listDocuments = vi.fn(real.listDocuments);
    await renderPage({ ...real, getDocumentStats: async () => STATS, listDocuments });
    const select = screen.getByLabelText(t.filterValidityLabel) as HTMLSelectElement;
    expect(within(select).getByRole("option", { name: t.filterValidityExpiring })).toBeTruthy();
    fireEvent.change(select, { target: { value: "expiring" } });
    await waitFor(() => expect(listDocuments.mock.calls.some((c) => c[1]?.validity === "expiring")).toBe(true));
    expect(tile(t.tileExpiring).getAttribute("aria-pressed")).toBe("true");
  });

  it("reloads the counts after a change", async () => {
    const real = createMockClient();
    let active = 3;
    const getDocumentStats = vi.fn(async () => ({ ...STATS, active }));
    await renderPage({ ...real, getDocumentStats });
    await waitFor(() => expect(tile(t.tileActive).textContent).toContain("3"));
    const before = getDocumentStats.mock.calls.length;

    active = 2;
    fireEvent.click(within(row(CANTEEN)).getByRole("button", { name: t.deactivate }));
    await waitFor(() => expect(tile(t.tileActive).textContent).toContain("2"));
    expect(getDocumentStats.mock.calls.length).toBeGreaterThan(before);
  });

  it("says so when the counts cannot be loaded, and the table still works", async () => {
    const real = createMockClient();
    await renderPage({
      ...real,
      getDocumentStats: async () => {
        throw new Error("down");
      },
    });
    expect(await screen.findByText(t.statsError)).toBeTruthy();
    expect(screen.getByText(CANTEEN)).toBeTruthy();
  });
});

function chunk(i: number, text = `Text ${i}`): DocumentChunk {
  return { chunk_index: i, heading_path: `Kapitola ${i} > Oddíl`, section_heading: null, text, char_count: text.length };
}

async function openDetail(client: ApiClient, title = WEB_RULES) {
  await renderPage(client);
  fireEvent.click(within(row(title)).getByRole("button", { name: t.moreActionsFor(title) }));
  fireEvent.click(screen.getByRole("menuitem", { name: t.detail }));
  return screen.findByRole("dialog", { name: dd.title });
}

describe("detail drawer – Obsah tab (§14.13)", () => {
  it("shows chunks with heading path, index, char count and text clipped to ~400 characters", async () => {
    const real = createMockClient();
    const listDocumentChunks = vi.fn(real.listDocumentChunks);
    const drawer = await openDetail({ ...real, listDocumentChunks });
    expect(listDocumentChunks).not.toHaveBeenCalled(); // lazy: only the tab asks
    expect(within(drawer).getByRole("tab", { name: dd.tabOverview }).getAttribute("aria-selected")).toBe("true");

    fireEvent.click(within(drawer).getByRole("tab", { name: dd.tabContent }));
    expect(await within(drawer).findByText(dd.chunkTitle(0))).toBeTruthy();
    expect(listDocumentChunks).toHaveBeenCalledWith("ZS_KAVCI_HORY", RULES_VERSION_NEW, { limit: 20, cursor: undefined }, expect.anything());
    const first = within(drawer).getByText(dd.chunkTitle(0)).closest("li") as HTMLElement;
    expect(within(first).getByText("Kapitola 1 > Oddíl 1")).toBeTruthy();
    expect(within(first).getByText(/^\d+ znaků$/)).toBeTruthy();
    const clipped = (first.querySelector(".chunk-text") as HTMLElement).textContent!;
    expect(clipped.length).toBe(CHUNK_CLIP + 1);
    expect(clipped.endsWith("…")).toBe(true);

    fireEvent.click(within(first).getByRole("button", { name: dd.showFull }));
    expect((first.querySelector(".chunk-text") as HTMLElement).textContent!.length).toBeGreaterThan(CHUNK_CLIP + 1);
    expect(within(first).getByRole("button", { name: dd.showLess }).getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(within(first).getByRole("button", { name: dd.showLess }));
    expect((first.querySelector(".chunk-text") as HTMLElement).textContent!.length).toBe(CHUNK_CLIP + 1);

    // A short chunk has no button at all.
    const second = within(drawer).getByText(dd.chunkTitle(1)).closest("li") as HTMLElement;
    expect(within(second).queryByRole("button")).toBeNull();
  });

  it("pages 20 at a time through the cursors, back and forth, and says which range it shows", async () => {
    const real = createMockClient();
    const listDocumentChunks = vi.fn(async (_s: string, _f: string, params?: { limit?: number; cursor?: string }) => {
      const offset = params?.cursor ? Number(params.cursor) : 0;
      const n = Math.min(20, 45 - offset);
      return {
        chunks: Array.from({ length: n }, (_, i) => chunk(offset + i)),
        total: 45,
        next_cursor: offset + 20 < 45 ? String(offset + 20) : null,
      };
    });
    const drawer = await openDetail({ ...real, listDocumentChunks });
    fireEvent.click(within(drawer).getByRole("tab", { name: dd.tabContent }));
    expect(await within(drawer).findByText(dd.pagerRange(1, 20, 45))).toBeTruthy();
    expect((within(drawer).getByRole("button", { name: dd.pagePrev }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(within(drawer).getByRole("button", { name: dd.pageNext }));
    expect(await within(drawer).findByText(dd.pagerRange(21, 40, 45))).toBeTruthy();
    expect(listDocumentChunks.mock.calls[1][2]).toEqual({ limit: 20, cursor: "20" });
    expect(within(drawer).getByText(dd.chunkTitle(20))).toBeTruthy();

    fireEvent.click(within(drawer).getByRole("button", { name: dd.pageNext }));
    expect(await within(drawer).findByText(dd.pagerRange(41, 45, 45))).toBeTruthy();
    expect((within(drawer).getByRole("button", { name: dd.pageNext }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(within(drawer).getByRole("button", { name: dd.pagePrev }));
    expect(await within(drawer).findByText(dd.pagerRange(21, 40, 45))).toBeTruthy();
    expect(within(drawer).getByText(dd.chunkTitle(20))).toBeTruthy();
  });

  it("lets the admin pick another version, starting on the shown one and on page 1", async () => {
    const real = createMockClient();
    const listDocumentChunks = vi.fn(real.listDocumentChunks);
    const drawer = await openDetail({ ...real, listDocumentChunks });
    fireEvent.click(within(drawer).getByRole("tab", { name: dd.tabContent }));
    await within(drawer).findByText(dd.chunkTitle(0));
    const select = within(drawer).getByLabelText(dd.contentVersion) as HTMLSelectElement;
    expect(select.value).toBe(RULES_VERSION_NEW);
    await waitFor(() => expect(select.options.length).toBe(2));

    fireEvent.change(select, { target: { value: RULES_VERSION_OLD } });
    await waitFor(() => expect(listDocumentChunks.mock.calls.some((c) => c[1] === RULES_VERSION_OLD)).toBe(true));
    expect(listDocumentChunks.mock.calls.filter((c) => c[1] === RULES_VERSION_OLD)[0][2]).toEqual({ limit: 20, cursor: undefined });
    expect(await within(drawer).findByText(dd.pagerRange(1, 12, 12))).toBeTruthy();
  });

  it("reports a chunk load failure and an empty version", async () => {
    const real = createMockClient();
    const failing = await openDetail({
      ...real,
      listDocumentChunks: async () => {
        throw new Error("down");
      },
    });
    fireEvent.click(within(failing).getByRole("tab", { name: dd.tabContent }));
    expect(await within(failing).findByRole("alert")).toBeTruthy();
    cleanup();

    const empty = await openDetail({ ...real, listDocumentChunks: async () => ({ chunks: [], total: 0, next_cursor: null }) });
    fireEvent.click(within(empty).getByRole("tab", { name: dd.tabContent }));
    expect(await within(empty).findByText(dd.contentEmpty)).toBeTruthy();
  });
});

describe("content fingerprint in the detail drawer (§14.14)", () => {
  it("shows only a short prefix per version, never the whole hash", async () => {
    const real = createMockClient();
    const sha = "ab12cd34ef56".padEnd(64, "0");
    const getDocument = async (...args: Parameters<ApiClient["getDocument"]>): Promise<DocumentDetail> => {
      const detail = await real.getDocument(...args);
      return { ...detail, versions: detail.versions.map((v, i) => (i === 0 ? { ...v, content_sha256: sha } : v)) };
    };
    const drawer = await openDetail({ ...real, getDocument });
    expect(await within(drawer).findByText(`${dd.contentHash}: ${dd.contentHashValue("ab12cd34")}`)).toBeTruthy();
    expect(document.body.textContent).not.toContain(sha);
    expect(document.body.textContent).not.toContain("ab12cd34ef56");
    expect(within(drawer).getAllByText(new RegExp(dd.contentHash))).toHaveLength(1);
  });
});

describe("refresh from the web (§14.14)", () => {
  it("is in the ⋯ menu of web documents only, for those who may ingest", async () => {
    const real = createMockClient();
    await renderPage(real);
    expect(within(openRowMenu(WEB_RULES)).getByRole("menuitem", { name: t.refresh })).toBeTruthy();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(within(openRowMenu(CANTEEN)).queryByRole("menuitem", { name: t.refresh })).toBeNull();
    cleanup();

    const noIngest: School = { ...KAVCI, permissions: { ...ALL, ingest: false } };
    render(<DocumentsPage client={real} school={noIngest} uiDefaultLang="cs" />);
    await screen.findByRole("table");
    expect(within(openRowMenu(WEB_RULES)).queryByRole("menuitem", { name: t.refresh })).toBeNull();
  });

  it("posts the refresh and shows the job in the 'Probíhající nahrávání' panel, then 'Beze změny' on a second run", async () => {
    const real = createMockClient();
    const refreshDocument = vi.fn(real.refreshDocument);
    await renderPage({ ...real, refreshDocument });
    expect(screen.queryByRole("region", { name: strings.documentJobs.heading })).toBeNull();

    fireEvent.click(within(openRowMenu(WEB_RULES)).getByRole("menuitem", { name: t.refresh }));
    expect(refreshDocument).toHaveBeenCalledWith("ZS_KAVCI_HORY", RULES_VERSION_NEW);
    const panel = await screen.findByRole("region", { name: strings.documentJobs.heading });
    expect(within(panel).getByText(/zs-kavcihory\.example/)).toBeTruthy();
    expect(await within(panel).findByText(/^hotovo/, undefined, { timeout: 4000 })).toBeTruthy();

    // The page was ingested and its hash recorded: the same page again changes nothing.
    await waitFor(() => expect(screen.queryByText(t.loading)).toBeNull());
    await waitFor(() => expect(row(WEB_RULES)).toBeTruthy());
    fireEvent.click(within(openRowMenu(WEB_RULES)).getByRole("menuitem", { name: t.refresh }));
    const skipped = await within(panel).findByText(strings.documentUpload.jobSkipped, undefined, { timeout: 4000 });
    expect(skipped.className).toContain("badge-neutral");
  });

  it("shows the server's refusal", async () => {
    const real = createMockClient();
    await renderPage({
      ...real,
      refreshDocument: async () => {
        throw new Error("Obnovit z webu lze jen webový dokument.");
      },
    });
    fireEvent.click(within(openRowMenu(WEB_RULES)).getByRole("menuitem", { name: t.refresh }));
    expect(await screen.findByText(t.refreshFailed("Obnovit z webu lze jen webový dokument."))).toBeTruthy();
  });
});

describe("skipped jobs (§14.14)", () => {
  const job = (over: Partial<DocumentJob>): DocumentJob => ({
    job_id: "j1",
    batch_id: "b",
    school: "ZS_KAVCI_HORY",
    source_kind: "file",
    source: "rad.pdf",
    status: "skipped",
    created_at: new Date().toISOString(),
    started_at: null,
    finished_at: new Date().toISOString(),
    processing_uuid: null,
    result: { reason: "unchanged", file_uuid: "x" },
    stored_chunks: null,
    error: null,
    ...over,
  });

  it("is a neutral 'Beze změny – přeskočeno' badge in the jobs panel", async () => {
    const real = createMockClient();
    await renderPage({ ...real, listDocumentJobs: async () => [job({})] });
    const panel = await screen.findByRole("region", { name: strings.documentJobs.heading });
    const badge = within(panel).getByText("Beze změny – přeskočeno");
    expect(badge.className).toContain("badge-neutral");
    expect(badge.className).not.toContain("badge-inactive");
    expect(panel.querySelector(".doc-row-error")).toBeNull();
  });

  it("is the same badge in the item rows of the upload dialog, and polling stops", async () => {
    const options: DocumentOptions = { ...(optionsFixture as unknown as DocumentOptions), domains: [] };
    const uploadDocument = vi.fn(async () => job({ status: "queued", finished_at: null, result: null }));
    const listDocumentJobs = vi.fn(async () => [job({})]);
    render(
      <DocumentUploadDialog
        client={{ uploadDocument, listDocumentJobs } as unknown as ApiClient}
        school="ZS_KAVCI_HORY"
        options={options}
        uiDefaultLang="cs"
        onClose={() => {}}
      />,
    );
    fireEvent.change(screen.getByLabelText(strings.documentUpload.filesLabel), {
      target: { files: [new File(["x"], "rad.pdf", { type: "application/pdf" })] },
    });
    fireEvent.click(screen.getByLabelText(strings.newPack.visibilityEntity));
    fireEvent.click(screen.getByLabelText("Rodič"));
    fireEvent.change(screen.getByLabelText(strings.documentMetadata.validity), { target: { value: "none" } });
    fireEvent.click(screen.getByRole("button", { name: strings.documentUpload.submit }));

    const badge = await screen.findByText("Beze změny – přeskočeno");
    expect(badge.className).toContain("badge-neutral");
    const calls = listDocumentJobs.mock.calls.length;
    await act(async () => {
      await new Promise((r) => setTimeout(r, 100));
    });
    expect(listDocumentJobs.mock.calls.length).toBe(calls);
  });
});
