import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ApiClient } from "../api/client";
import type { DocumentJob, School, SchoolPermissions } from "../api/types";
import { createMockClient } from "../api/mockClient";
import { DocumentsPage } from "../components/DocumentsPage";
import { formatDate } from "../lib/documentFormat";
import { strings } from "../strings.cs";

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
const b = strings.documentBulk;
const ALL: SchoolPermissions = { read: true, publish: true, ingest: true, deactivate: true, delete_permanently: true };
const KAVCI: School = { code: "ZS_KAVCI_HORY", name: "ZŠ Kavčí hory", permissions: ALL };
const JEDOVNICE: School = { code: "ZS_JEDOVNICE", name: "ZŠ Jedovnice", permissions: ALL };

const WEB_RULES = "https://www.zs-kavcihory.example/skolni-rad";
const CANTEEN = "Provozni_rad_jidelny.pdf";
const MATH = "Matematika_pracovni_list_3.pdf";
const DRUZINA = "https://www.zsjedovnice.example/druzina";
const EXPIRED = "Jidelnicek_2024_25.pdf";
const FUTURE = "Skolni_rok_2030_31.docx";
const OTHER_AUTHOR = "Pololetni_zprava.pdf";

function row(title: string): HTMLElement {
  return screen.getByText(title).closest("tr") as HTMLElement;
}

function openRowMenu(title: string): HTMLElement {
  fireEvent.click(within(row(title)).getByRole("button", { name: t.moreActionsFor(title) }));
  return screen.getByRole("menu");
}

function pick(title: string) {
  fireEvent.click(within(row(title)).getByRole("checkbox", { name: t.selectRow(title) }));
}

async function renderPage(school: School, client: ApiClient = createMockClient()) {
  render(<DocumentsPage client={client} school={school} uiDefaultLang="cs" />);
  await screen.findByRole("table");
  return client;
}

describe("DocumentsPage — validity badge", () => {
  it("marks expired and future documents next to the state and leaves current ones plain", async () => {
    await renderPage(JEDOVNICE);
    const expired = row(EXPIRED);
    expect(within(expired).getByText(t.stateActive)).toBeTruthy();
    expect(within(expired).getByText(t.validityExpired)).toBeTruthy();
    expect(within(row(FUTURE)).getByText(t.validityFuture(formatDate("2030-09-01")))).toBeTruthy();
    expect(within(row(DRUZINA)).queryByText(t.validityExpired)).toBeNull();
    expect(within(row(OTHER_AUTHOR)).queryByText(/^Platnost/)).toBeNull();
  });

  it("shortens long URLs with the full value in the title, and merges type and language", async () => {
    const long = "https://www.zs-kavcihory.example/dokumenty/provozni-rady/2026/skolni-rad-pro-zaky-a-rodice-final";
    const real = createMockClient();
    const client: ApiClient = {
      ...real,
      listDocuments: async (...args) => {
        const page = await real.listDocuments(...args);
        return {
          ...page,
          documents: page.documents.map((d) => (d.source_url === WEB_RULES ? { ...d, title: long, source_url: long } : d)),
        };
      },
    };
    await renderPage(KAVCI, client);
    const link = screen.getByTitle(long);
    expect(link.tagName).toBe("A");
    expect(link.textContent?.startsWith("www.zs-kavcihory.example/…/skolni-rad-pro-zaky")).toBe(true);
    expect(link.textContent?.endsWith("…")).toBe(true);
    expect(link.textContent!.length).toBeLessThanOrEqual(60);
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    expect(link.getAttribute("target")).toBe("_blank");
    const cell = link.closest("tr") as HTMLElement;
    await waitFor(() => expect(within(cell).getByText("Školní řád")).toBeTruthy());
    expect(within(cell).getByText("Čeština")).toBeTruthy();
  });
});

describe("DocumentsPage — filters", () => {
  it("filters by validity, type and role through the list parameters", async () => {
    const real = createMockClient();
    const listDocuments = vi.fn(real.listDocuments);
    await renderPage(JEDOVNICE, { ...real, listDocuments });
    await waitFor(() => expect((screen.getByLabelText(t.filterTypeLabel) as HTMLSelectElement).options.length).toBeGreaterThan(1));
    expect(screen.getByRole("option", { name: "Rodič" })).toBeTruthy();

    fireEvent.change(screen.getByLabelText(t.filterValidityLabel), { target: { value: "expired" } });
    expect(await screen.findByText(EXPIRED)).toBeTruthy();
    expect(screen.queryByText(DRUZINA)).toBeNull();
    expect(screen.queryByText(FUTURE)).toBeNull();
    expect(listDocuments.mock.calls.at(-1)?.[1]).toMatchObject({ validity: "expired", status: "active" });

    fireEvent.change(screen.getByLabelText(t.filterValidityLabel), { target: { value: "future" } });
    expect(await screen.findByText(FUTURE)).toBeTruthy();
    expect(screen.queryByText(EXPIRED)).toBeNull();

    fireEvent.change(screen.getByLabelText(t.filterValidityLabel), { target: { value: "all" } });
    fireEvent.change(screen.getByLabelText(t.filterRoleLabel), { target: { value: "TEACHER" } });
    expect(await screen.findByText(OTHER_AUTHOR)).toBeTruthy();
    expect(screen.queryByText(DRUZINA)).toBeNull();
    expect(screen.getByText(FUTURE)).toBeTruthy();
    expect(listDocuments.mock.calls.at(-1)?.[1]).toMatchObject({ role: "TEACHER" });

    fireEvent.change(screen.getByLabelText(t.filterTypeLabel), { target: { value: "ANNOUNCEMENT" } });
    expect(await screen.findByText(OTHER_AUTHOR)).toBeTruthy();
    expect(screen.queryByText(FUTURE)).toBeNull();
    expect(listDocuments.mock.calls.at(-1)?.[1]).toMatchObject({ type: "ANNOUNCEMENT", role: "TEACHER" });

    fireEvent.click(screen.getByRole("button", { name: t.resetFilters }));
    expect(await screen.findByText(DRUZINA)).toBeTruthy();
    expect(listDocuments.mock.calls.at(-1)?.[1]?.type).toBeUndefined();
  });
});

describe("DocumentsPage — bulk actions", () => {
  it("selects rows, selects all on the page and clears the selection", async () => {
    await renderPage(KAVCI);
    expect(screen.queryByRole("group", { name: b.selected(1) })).toBeNull();
    pick(CANTEEN);
    expect(screen.getByText(b.selected(1))).toBeTruthy();

    fireEvent.click(screen.getByRole("checkbox", { name: t.selectAll }));
    expect(screen.getByText(b.selected(3))).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: t.selectAll }));
    expect(screen.queryByText(b.selected(3))).toBeNull();

    pick(WEB_RULES);
    fireEvent.click(screen.getByRole("button", { name: b.clear }));
    expect(screen.queryByText(b.selected(1))).toBeNull();
  });

  it("deactivates the selected documents one after another and reports the outcome", async () => {
    const real = createMockClient();
    let running = 0;
    let overlap = false;
    const calls: string[] = [];
    const deactivateDocument: ApiClient["deactivateDocument"] = async (school, id) => {
      running += 1;
      overlap = overlap || running > 1;
      calls.push(id);
      try {
        if (id.startsWith("1a2b3c4d")) throw new Error("Server odmítl.");
        return await real.deactivateDocument(school, id);
      } finally {
        running -= 1;
      }
    };
    await renderPage(KAVCI, { ...real, deactivateDocument });
    pick(WEB_RULES);
    pick(CANTEEN);
    fireEvent.click(screen.getByRole("button", { name: b.deactivate(2) }));

    expect(await screen.findByText(b.resultOk(1, 2))).toBeTruthy();
    expect(screen.getByText(b.resultFailedItem(CANTEEN, "Server odmítl."))).toBeTruthy();
    expect(calls).toHaveLength(2);
    expect(overlap).toBe(false);
    expect(await screen.findByText(CANTEEN)).toBeTruthy(); // the failed one stays active
    expect(screen.queryByText(WEB_RULES)).toBeNull();
    expect(screen.queryByText(b.selected(2))).toBeNull(); // selection cleared after the run
  });

  it("activates only the inactive selected documents", async () => {
    const real = createMockClient();
    const activateDocument = vi.fn(real.activateDocument);
    await renderPage(KAVCI, { ...real, activateDocument });
    fireEvent.change(screen.getByLabelText(t.filterLabel), { target: { value: "all" } });
    await screen.findByText("Jidelnicek_zari.docx");
    pick("Jidelnicek_zari.docx");
    pick(CANTEEN);
    expect((screen.getByRole("button", { name: b.activate(1) }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByRole("button", { name: b.deactivate(1) })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: b.activate(1) }));
    expect(await screen.findByText(b.resultOk(1, 1))).toBeTruthy();
    expect(activateDocument).toHaveBeenCalledTimes(1);
    expect(activateDocument.mock.calls[0][1]).toBe("2b3c4d5e-2222-4abc-8def-000000000301");
  });

  it("asks once with the list of titles, deletes sequentially and lists per-item results", async () => {
    const real = createMockClient();
    const deleteDocument = vi.fn(async (...args: Parameters<ApiClient["deleteDocument"]>) => {
      if (args[1].startsWith("1a2b3c4d")) throw new Error("Smazání selhalo.");
      return real.deleteDocument(...args);
    });
    await renderPage(KAVCI, { ...real, deleteDocument });
    pick(WEB_RULES);
    pick(CANTEEN);
    fireEvent.click(screen.getByRole("button", { name: b.delete(2) }));

    const dialog = await screen.findByRole("dialog", { name: b.deleteTitle });
    expect(within(dialog).getByText(WEB_RULES)).toBeTruthy();
    expect(within(dialog).getByText(CANTEEN)).toBeTruthy();
    expect(within(dialog).getByText(b.deleteIntro(2))).toBeTruthy();
    expect(deleteDocument).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: b.deleteConfirm(2) }));
    expect(await within(dialog).findByText(b.resultOk(1, 2))).toBeTruthy();
    expect(within(dialog).getByText(new RegExp(b.itemFailed("Smazání selhalo.")))).toBeTruthy();
    expect(deleteDocument).toHaveBeenCalledTimes(2);
    expect(deleteDocument.mock.calls.map((c) => [c[1], c[2].confirm_file_uuid])).toEqual([
      ["0b6f2c1e-6a4d-4c1b-9a51-2d1f3e5a7b01", "0b6f2c1e-6a4d-4c1b-9a51-2d1f3e5a7b01"],
      ["1a2b3c4d-1111-4abc-8def-000000000201", "1a2b3c4d-1111-4abc-8def-000000000201"],
    ]);
    await waitFor(() => expect(screen.queryByText(WEB_RULES, { selector: "a" })).toBeNull());
    // The dialog still shows the run after the list reloaded behind it.
    expect(within(dialog).getByText(WEB_RULES)).toBeTruthy();
  });

  it("hides bulk actions the policy does not allow", async () => {
    await renderPage({ ...KAVCI, permissions: { ...ALL, delete_permanently: false, deactivate: false } });
    pick(CANTEEN);
    expect(screen.getByText(b.selected(1))).toBeTruthy();
    expect(screen.queryByRole("button", { name: b.delete(1) })).toBeNull();
    expect(screen.queryByRole("button", { name: b.deactivate(1) })).toBeNull();
  });
});

describe("DocumentsPage — detail drawer", () => {
  it("shows metadata, versions with processing_uuid, chunk counts and an external source link", async () => {
    await renderPage(KAVCI);
    fireEvent.click(within(openRowMenu(WEB_RULES)).getByRole("menuitem", { name: t.detail }));

    const drawer = await screen.findByRole("dialog", { name: strings.documentDetail.title });
    expect(drawer.getAttribute("aria-modal")).toBe("false");
    await within(drawer).findByText("5c3d7e10-0000-4000-8000-000000000101");
    expect(within(drawer).getByText("5c3d7e10-0000-4000-8000-000000000100")).toBeTruthy();
    expect(within(drawer).getByText("0b6f2c1e-6a4d-4c1b-9a51-2d1f3e5a7b00")).toBeTruthy();
    expect(within(drawer).getByText("Školní řád")).toBeTruthy();
    expect(within(drawer).getByText("Rodič, Žák")).toBeTruthy();
    expect(within(drawer).getByText(strings.documentDetail.chunks).nextElementSibling?.textContent).toBe("14");

    const source = within(drawer).getByRole("link", { name: strings.documentDetail.openSource });
    expect(source.getAttribute("href")).toBe(WEB_RULES);
    expect(source.getAttribute("rel")).toBe("noopener noreferrer");
    expect(source.getAttribute("target")).toBe("_blank");
    expect(drawer.contains(document.activeElement)).toBe(true);
  });

  it("closes with the button and with Escape, restoring focus", async () => {
    await renderPage(KAVCI);
    fireEvent.click(within(openRowMenu(CANTEEN)).getByRole("menuitem", { name: t.detail }));
    await screen.findByRole("dialog", { name: strings.documentDetail.title });
    fireEvent.click(screen.getByRole("button", { name: strings.documentDetail.close }));
    expect(screen.queryByRole("dialog", { name: strings.documentDetail.title })).toBeNull();

    fireEvent.click(within(openRowMenu(CANTEEN)).getByRole("menuitem", { name: t.detail }));
    await screen.findByRole("dialog", { name: strings.documentDetail.title });
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: strings.documentDetail.title })).toBeNull();
  });
});

describe("DocumentsPage — edit validity", () => {
  it("saves 'bez omezení' as two nulls and reloads the row", async () => {
    const real = createMockClient();
    const updateDocumentValidity = vi.fn(real.updateDocumentValidity);
    await renderPage(KAVCI, { ...real, updateDocumentValidity });
    fireEvent.click(within(openRowMenu(CANTEEN)).getByRole("menuitem", { name: t.editValidity }));

    const dialog = await screen.findByRole("dialog", { name: strings.documentValidity.title });
    const select = within(dialog).getByLabelText(strings.documentMetadata.validity) as HTMLSelectElement;
    // 2026-09-01 – 2027-08-31 is the default school-year period.
    expect(select.value).toBe("period:1");
    fireEvent.change(select, { target: { value: "none" } });
    fireEvent.click(within(dialog).getByRole("button", { name: strings.documentValidity.save }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: strings.documentValidity.title })).toBeNull());
    expect(updateDocumentValidity).toHaveBeenCalledWith(KAVCI.code, "1a2b3c4d-1111-4abc-8def-000000000201", {
      valid_from: null,
      valid_to: null,
    });
    await waitFor(() => expect(within(row(CANTEEN)).getByText(t.validityUnlimited)).toBeTruthy());
  });

  it("custom dates must satisfy from <= to and make an expired document current again", async () => {
    const real = createMockClient();
    const updateDocumentValidity = vi.fn(real.updateDocumentValidity);
    await renderPage(JEDOVNICE, { ...real, updateDocumentValidity });
    expect(within(row(EXPIRED)).getByText(t.validityExpired)).toBeTruthy();
    fireEvent.click(within(openRowMenu(EXPIRED)).getByRole("menuitem", { name: t.editValidity }));

    const dialog = await screen.findByRole("dialog", { name: strings.documentValidity.title });
    const save = within(dialog).getByRole("button", { name: strings.documentValidity.save }) as HTMLButtonElement;
    expect((within(dialog).getByLabelText(strings.documentMetadata.validity) as HTMLSelectElement).value).toBe("custom");
    fireEvent.change(within(dialog).getByLabelText(strings.documentMetadata.validFrom), { target: { value: "2099-02-01" } });
    fireEvent.change(within(dialog).getByLabelText(strings.documentMetadata.validTo), { target: { value: "2099-01-01" } });
    expect(within(dialog).getByText(strings.documentMetadata.errRange)).toBeTruthy();
    expect(save.disabled).toBe(true);

    fireEvent.change(within(dialog).getByLabelText(strings.documentMetadata.validFrom), { target: { value: "2020-01-01" } });
    fireEvent.change(within(dialog).getByLabelText(strings.documentMetadata.validTo), { target: { value: "2099-12-31" } });
    expect(save.disabled).toBe(false);
    fireEvent.click(save);

    await waitFor(() => expect(updateDocumentValidity).toHaveBeenCalledTimes(1));
    expect(updateDocumentValidity.mock.calls[0][2]).toEqual({ valid_from: "2020-01-01", valid_to: "2099-12-31" });
    await waitFor(() => expect(within(row(EXPIRED)).queryByText(t.validityExpired)).toBeNull());
  });

  it("keeps the dialog open and says why when the server refuses", async () => {
    const real = createMockClient();
    const client: ApiClient = {
      ...real,
      updateDocumentValidity: async () => {
        throw new Error("Úprava platnosti selhala.");
      },
    };
    await renderPage(KAVCI, client);
    fireEvent.click(within(openRowMenu(CANTEEN)).getByRole("menuitem", { name: t.editValidity }));
    const dialog = await screen.findByRole("dialog", { name: strings.documentValidity.title });
    fireEvent.change(within(dialog).getByLabelText(strings.documentMetadata.validity), { target: { value: "none" } });
    fireEvent.click(within(dialog).getByRole("button", { name: strings.documentValidity.save }));
    expect(await within(dialog).findByText("Úprava platnosti selhala.")).toBeTruthy();
  });
});

describe("DocumentsPage — new version", () => {
  it("prefills a web document with its URL and metadata and posts the same source", async () => {
    const real = createMockClient();
    const uploadDocument = vi.fn(real.uploadDocument);
    await renderPage(KAVCI, { ...real, uploadDocument });
    fireEvent.click(within(openRowMenu(WEB_RULES)).getByRole("menuitem", { name: t.newVersion }));

    const dialog = await screen.findByRole("dialog", { name: strings.documentUpload.newVersionTitle });
    expect(within(dialog).queryByRole("tab")).toBeNull();
    const urls = within(dialog).getByLabelText(strings.documentUpload.urlsLabel) as HTMLTextAreaElement;
    expect(urls.value).toBe(WEB_RULES);
    expect(urls.readOnly).toBe(true);
    expect((within(dialog).getByLabelText(strings.newPack.visibilityEntity) as HTMLInputElement).checked).toBe(true);
    expect((within(dialog).getByLabelText("Rodič") as HTMLInputElement).checked).toBe(true);
    expect((within(dialog).getByLabelText("Žák") as HTMLInputElement).checked).toBe(true);
    expect((within(dialog).getByLabelText("Učitel") as HTMLInputElement).checked).toBe(false);
    expect((within(dialog).getByLabelText(strings.documentMetadata.documentType) as HTMLSelectElement).value).toBe("SCHOOL_RULES");
    expect((within(dialog).getByLabelText(strings.documentMetadata.language) as HTMLSelectElement).value).toBe("cs");
    expect((within(dialog).getByLabelText("Provoz školy") as HTMLInputElement).checked).toBe(true);
    expect((within(dialog).getByLabelText(strings.documentMetadata.validity) as HTMLSelectElement).value).toBe("none");

    const submit = within(dialog).getByRole("button", { name: strings.documentUpload.submit }) as HTMLButtonElement;
    await waitFor(() => expect(submit.disabled).toBe(false));
    fireEvent.click(submit);
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(1));
    const item = uploadDocument.mock.calls[0][1];
    expect(item.url).toBe(WEB_RULES);
    expect(item.file).toBeUndefined();
    expect(item.metadata).toEqual({
      visibility: "entity",
      audience_roles: ["PARENT", "STUDENT"],
      document_type: "SCHOOL_RULES",
      document_language: "cs",
      school_subject: null,
      school_grade_codes: [],
      domain_codes: ["SCHOOL_OPERATIONS"],
      valid_from: null,
      valid_to: null,
    });
  });

  it("requires exactly one file for a file document and sends it under the original name", async () => {
    const real = createMockClient();
    const uploadDocument = vi.fn(real.uploadDocument);
    await renderPage(KAVCI, { ...real, uploadDocument });
    fireEvent.click(within(openRowMenu(CANTEEN)).getByRole("menuitem", { name: t.newVersion }));

    const dialog = await screen.findByRole("dialog", { name: strings.documentUpload.newVersionTitle });
    const submit = within(dialog).getByRole("button", { name: strings.documentUpload.submit }) as HTMLButtonElement;
    expect(within(dialog).queryByLabelText(strings.documentUpload.urlsLabel)).toBeNull();
    expect(within(dialog).getByText(strings.documentUpload.newVersionFileName(CANTEEN))).toBeTruthy();
    expect((within(dialog).getByLabelText(strings.newPack.visibilityOrganization) as HTMLInputElement).checked).toBe(true);
    expect((within(dialog).getByLabelText("1. ročník") as HTMLInputElement).checked).toBe(true);
    expect((within(dialog).getByLabelText("3. ročník") as HTMLInputElement).checked).toBe(true);
    expect((within(dialog).getByLabelText(strings.documentMetadata.validity) as HTMLSelectElement).value).toBe("period:1");
    expect(submit.disabled).toBe(true);
    expect(within(dialog).getByText(strings.documentUpload.newVersionOneFile)).toBeTruthy();

    const input = within(dialog).getByLabelText(strings.documentUpload.filesLabel);
    fireEvent.change(input, { target: { files: [new File(["x"], "novy_rad.docx")] } });
    expect(within(dialog).getByText(strings.documentUpload.errExtensionMismatch(".pdf"))).toBeTruthy();
    expect(submit.disabled).toBe(true);

    fireEvent.change(input, { target: { files: [new File(["obsah"], "novy_rad_2027.pdf", { type: "application/pdf" })] } });
    expect(within(dialog).queryByText(strings.documentUpload.errExtensionMismatch(".pdf"))).toBeNull();
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);

    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(1));
    const item = uploadDocument.mock.calls[0][1];
    expect(item.file?.name).toBe(CANTEEN);
    expect(item.url).toBeUndefined();
    expect(item.metadata).toMatchObject({
      visibility: "organization",
      audience_roles: ["PARENT"],
      school_grade_codes: ["1", "2", "3"],
      domain_codes: ["SCHOOL_OPERATIONS", "SAFETY"],
      valid_from: "2026-09-01",
      valid_to: "2027-08-31",
    });
  });
});

function job(over: Partial<DocumentJob>): DocumentJob {
  return {
    job_id: "job-1",
    batch_id: "b",
    school: KAVCI.code,
    source_kind: "file",
    source: "Rozvrh.pdf",
    status: "queued",
    created_at: new Date().toISOString(),
    started_at: null,
    finished_at: null,
    processing_uuid: null,
    result: null,
    stored_chunks: null,
    error: null,
    ...over,
  };
}

describe("DocumentsPage — jobs panel", () => {
  it("lists queued, processing and recently finished jobs and polls only while something is active", async () => {
    const hour = 60 * 60 * 1000;
    const stages: DocumentJob[][] = [
      [
        job({ job_id: "j1", source: "Rozvrh.pdf", status: "queued" }),
        job({ job_id: "j2", source: "Hotovo.pdf", status: "completed", finished_at: new Date(Date.now() - hour).toISOString(), stored_chunks: 4 }),
        job({ job_id: "j3", source: "Stare.pdf", status: "completed", finished_at: new Date(Date.now() - 30 * hour).toISOString(), stored_chunks: 2 }),
      ],
      [
        job({ job_id: "j1", source: "Rozvrh.pdf", status: "processing", started_at: new Date().toISOString() }),
        job({ job_id: "j2", source: "Hotovo.pdf", status: "completed", finished_at: new Date(Date.now() - hour).toISOString(), stored_chunks: 4 }),
      ],
      [
        job({ job_id: "j1", source: "Rozvrh.pdf", status: "completed", finished_at: new Date().toISOString(), stored_chunks: 9 }),
        job({ job_id: "j2", source: "Hotovo.pdf", status: "completed", finished_at: new Date(Date.now() - hour).toISOString(), stored_chunks: 4 }),
      ],
    ];
    let call = 0;
    const real = createMockClient();
    const listDocumentJobs = vi.fn(async () => stages[Math.min(call++, stages.length - 1)]);
    const listDocuments = vi.fn(real.listDocuments);
    await renderPage(KAVCI, { ...real, listDocumentJobs, listDocuments });

    const panel = await screen.findByRole("region", { name: strings.documentJobs.heading });
    expect(within(panel).getByText("Rozvrh.pdf")).toBeTruthy();
    expect(within(panel).getByText("Hotovo.pdf")).toBeTruthy();
    expect(within(panel).queryByText("Stare.pdf")).toBeNull(); // finished more than 24 h ago
    expect(within(panel).getByText(strings.documentUpload.jobQueued)).toBeTruthy();

    expect(await within(panel).findByText(strings.documentUpload.jobCompletedChunks(9))).toBeTruthy();
    const listCallsBefore = listDocuments.mock.calls.length;
    await waitFor(() => expect(listDocuments.mock.calls.length).toBeGreaterThan(listCallsBefore - 1));

    // Nothing queued/processing any more: polling must stop.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 80));
    });
    const settled = listDocumentJobs.mock.calls.length;
    await act(async () => {
      await new Promise((r) => setTimeout(r, 120));
    });
    expect(listDocumentJobs.mock.calls.length).toBe(settled);
    expect(settled).toBe(3);
  });

  it("can be collapsed, and is absent when there is nothing recent", async () => {
    const real = createMockClient();
    const listDocumentJobs = vi.fn(async () => [job({ job_id: "j1", status: "failed", finished_at: new Date().toISOString(), error: { code: "X", message: "Rozbito." } })]);
    await renderPage(KAVCI, { ...real, listDocumentJobs });
    const panel = await screen.findByRole("region", { name: strings.documentJobs.heading });
    expect(within(panel).getByText(strings.documentUpload.jobFailed("Rozbito."))).toBeTruthy();
    const toggle = within(panel).getByRole("button", { name: strings.documentJobs.collapse });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(toggle);
    expect(within(panel).queryByText("Rozvrh.pdf")).toBeNull();
    expect(within(panel).getByRole("button", { name: strings.documentJobs.expand }).getAttribute("aria-expanded")).toBe("false");
    cleanup();

    await renderPage(KAVCI, { ...real, listDocumentJobs: async () => [] });
    expect(screen.queryByRole("region", { name: strings.documentJobs.heading })).toBeNull();
  });
});
