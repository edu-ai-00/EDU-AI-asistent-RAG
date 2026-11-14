import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/types";
import type { School, SchoolPermissions } from "../api/types";
import { createMockClient } from "../api/mockClient";
import { DocumentsPage } from "../components/DocumentsPage";
import { filenameFromDisposition } from "../lib/download";
import { strings } from "../strings.cs";

vi.mock("../config", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../config")>()),
  DOCUMENTS_SEARCH_DEBOUNCE_MS: 5,
}));

const t = strings.documents;
const dl = strings.documentDownload;
const ALL: SchoolPermissions = { read: true, publish: true, ingest: true, deactivate: true, delete_permanently: true };
const KAVCI: School = { code: "ZS_KAVCI_HORY", name: "ZŠ Kavčí hory", permissions: ALL };
const WEB_RULES = "https://www.zs-kavcihory.example/skolni-rad";
const CANTEEN = "Provozni_rad_jidelny.pdf";
const MENU = "Jidelnicek_zari.docx";
const CANTEEN_UUID = "1a2b3c4d-1111-4abc-8def-000000000201";

const urlApi = URL as unknown as { createObjectURL?: unknown; revokeObjectURL?: unknown };
let createObjectURL: ReturnType<typeof vi.fn>;
let anchorClick: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  createObjectURL = vi.fn(() => "blob:mock");
  urlApi.createObjectURL = createObjectURL;
  urlApi.revokeObjectURL = vi.fn();
  anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  delete urlApi.createObjectURL;
  delete urlApi.revokeObjectURL;
});

function row(title: string): HTMLElement {
  return screen.getByText(title).closest("tr") as HTMLElement;
}

function openRowMenu(title: string): HTMLElement {
  fireEvent.click(within(row(title)).getByRole("button", { name: t.moreActionsFor(title) }));
  return screen.getByRole("menu");
}

async function renderPage(client: ApiClient = createMockClient()) {
  render(<DocumentsPage client={client} school={KAVCI} uiDefaultLang="cs" />);
  await screen.findByText(WEB_RULES);
  await screen.findByText(CANTEEN);
}

describe("format chip and filter", () => {
  it("shows the file format as a chip next to the title", async () => {
    await renderPage();
    expect(within(row(WEB_RULES)).getByText("Web")).toBeTruthy();
    expect(within(row(CANTEEN)).getByText("PDF")).toBeTruthy();
    expect(within(row(CANTEEN)).getByText("PDF").className).toContain("chip");
    fireEvent.change(screen.getByLabelText(t.filterLabel), { target: { value: "all" } });
    expect(within(row(await screen.findByText(MENU) && MENU)).getByText("DOCX")).toBeTruthy();
  });

  it("falls back to the kind text when the backend sends no file_format", async () => {
    const client = createMockClient();
    const list = client.listDocuments.bind(client);
    client.listDocuments = async (...args) => {
      const page = await list(...args);
      return { ...page, documents: page.documents.map(({ file_format: _f, ...rest }) => rest) };
    };
    await renderPage(client);
    expect(within(row(CANTEEN)).getByText(t.kindFile)).toBeTruthy();
    expect(within(row(WEB_RULES)).getByText(t.kindWeb)).toBeTruthy();
  });

  it("offers the six format choices and sends the chosen one as `format`", async () => {
    const client = createMockClient();
    const spy = vi.spyOn(client, "listDocuments");
    await renderPage(client);
    const select = screen.getByLabelText(t.filterFormatLabel) as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => [o.value, o.textContent])).toEqual([
      ["", "Všechny formáty"],
      ["pdf", "PDF"],
      ["docx", "Word (DOCX)"],
      ["pptx", "PowerPoint (PPTX)"],
      ["xlsx", "Excel (XLSX)"],
      ["web", "Webová stránka"],
    ]);
    expect(spy.mock.calls[0][1]?.format).toBeUndefined();

    fireEvent.change(select, { target: { value: "pdf" } });
    expect(await screen.findByText(CANTEEN)).toBeTruthy();
    expect(screen.queryByText(WEB_RULES)).toBeNull();
    expect(spy.mock.calls.at(-1)?.[1]?.format).toBe("pdf");

    fireEvent.change(select, { target: { value: "web" } });
    expect(await screen.findByText(WEB_RULES)).toBeTruthy();
    expect(screen.queryByText(CANTEEN)).toBeNull();
  });

  it("is cleared by 'Zrušit filtry', which it also enables", async () => {
    await renderPage();
    const reset = screen.getByRole("button", { name: t.resetFilters }) as HTMLButtonElement;
    expect(reset.disabled).toBe(true);
    const select = screen.getByLabelText(t.filterFormatLabel) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "docx" } });
    await waitFor(() => expect(reset.disabled).toBe(false));
    fireEvent.click(reset);
    expect(select.value).toBe("");
    expect(await screen.findByText(WEB_RULES)).toBeTruthy();
  });
});

describe("filter bar layout", () => {
  it("keeps every control inside the one filter grid, reset button last", async () => {
    await renderPage();
    const grid = document.querySelector(".documents-filters") as HTMLElement;
    expect(grid.querySelectorAll("select").length).toBe(5);
    expect(grid.querySelectorAll("input[type=search]").length).toBe(1);
    expect(grid.lastElementChild?.textContent).toBe(t.resetFilters);
  });
});

describe("download from the row menu", () => {
  it("downloads the shown version through fetch→blob→object URL", async () => {
    const client = createMockClient();
    const spy = vi.spyOn(client, "downloadDocument");
    await renderPage(client);
    fireEvent.click(within(openRowMenu(CANTEEN)).getByRole("menuitem", { name: t.download }));
    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    expect(spy).toHaveBeenCalledWith("ZS_KAVCI_HORY", CANTEEN_UUID);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(createObjectURL.mock.calls[0][0]).toBeInstanceOf(Blob);
    const link = anchorClick.mock.contexts[0] as HTMLAnchorElement;
    expect(link.download).toBe(CANTEEN);
    expect(link.getAttribute("href")).toBe("blob:mock");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says the file is gone instead of navigating when the gateway answers 404", async () => {
    const client = createMockClient();
    client.downloadDocument = async () => {
      throw new ApiError(404, { code: "NOT_FOUND", message: "the file of this version is not stored", retryable: false });
    };
    await renderPage(client);
    fireEvent.click(within(openRowMenu(CANTEEN)).getByRole("menuitem", { name: t.download }));
    expect((await screen.findByRole("alert")).textContent).toBe(dl.notStored);
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(anchorClick).not.toHaveBeenCalled();
  });

  it("shows other failures with their message", async () => {
    const client = createMockClient();
    client.downloadDocument = async () => {
      throw new ApiError(503, { code: "UNAVAILABLE", message: "Dependency temporarily unavailable", retryable: true });
    };
    await renderPage(client);
    fireEvent.click(within(openRowMenu(CANTEEN)).getByRole("menuitem", { name: t.download }));
    expect((await screen.findByRole("alert")).textContent).toBe(dl.failed("Dependency temporarily unavailable"));
  });

  it("clears the message on the next successful download", async () => {
    const client = createMockClient();
    const real = client.downloadDocument.bind(client);
    let calls = 0;
    client.downloadDocument = async (...args) => {
      calls += 1;
      if (calls === 1) throw new ApiError(404, { code: "NOT_FOUND", message: "x", retryable: false });
      return real(...args);
    };
    await renderPage(client);
    fireEvent.click(within(openRowMenu(CANTEEN)).getByRole("menuitem", { name: t.download }));
    await screen.findByRole("alert");
    fireEvent.click(within(openRowMenu(CANTEEN)).getByRole("menuitem", { name: t.download }));
    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("download in the detail drawer", () => {
  async function openDrawer(title: string, client: ApiClient = createMockClient()) {
    await renderPage(client);
    fireEvent.click(within(openRowMenu(title)).getByRole("menuitem", { name: t.detail }));
    const drawer = await screen.findByRole("dialog", { name: strings.documentDetail.title });
    await within(drawer).findByText(strings.documentDetail.versionsHeading);
    await waitFor(() => expect(within(drawer).getAllByRole("row").length).toBeGreaterThan(1));
    return { drawer, client };
  }

  it("shows the format and a plain download link per version of a file", async () => {
    const { drawer, client } = await openDrawer(CANTEEN);
    expect(within(drawer).getByText(strings.documentDetail.format).nextElementSibling?.textContent).toBe("PDF");
    const link = within(drawer).getByRole("link", { name: dl.file });
    expect(link.getAttribute("href")).toBe(client.documentDownloadUrl("ZS_KAVCI_HORY", CANTEEN_UUID));
    expect(link.hasAttribute("download")).toBe(true);
  });

  it("labels a saved web page's download as HTML, one link per version", async () => {
    const { drawer } = await openDrawer(WEB_RULES);
    const links = within(drawer).getAllByRole("link", { name: dl.webPage });
    expect(links).toHaveLength(2);
    expect(within(drawer).queryByRole("link", { name: dl.file })).toBeNull();
  });

  it("fetches the version's file instead of following the link", async () => {
    const client = createMockClient();
    const spy = vi.spyOn(client, "downloadDocument");
    const { drawer } = await openDrawer(CANTEEN, client);
    const proceeded = fireEvent.click(within(drawer).getByRole("link", { name: dl.file }));
    expect(proceeded).toBe(false); // default prevented: no navigation to a JSON 404
    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    expect(spy).toHaveBeenCalledWith("ZS_KAVCI_HORY", CANTEEN_UUID);
  });

  it("shows the 'file is gone' message inline, in the drawer", async () => {
    const client = createMockClient();
    client.downloadDocument = async () => {
      throw new ApiError(404, { code: "NOT_FOUND", message: "x", retryable: false });
    };
    const { drawer } = await openDrawer(CANTEEN, client);
    fireEvent.click(within(drawer).getByRole("link", { name: dl.file }));
    expect((await within(drawer).findByRole("alert")).textContent).toBe(dl.notStored);
    expect(anchorClick).not.toHaveBeenCalled();
  });
});

describe("mock client download", () => {
  it("serves a file, and answers 404 for the fixture whose file is gone", async () => {
    const client = createMockClient();
    const ok = await client.downloadDocument("ZS_KAVCI_HORY", CANTEEN_UUID);
    expect(ok.filename).toBe(CANTEEN);
    await expect(client.downloadDocument("ZS_JEDOVNICE", "708192a3-7777-4abc-8def-000000000801")).rejects.toMatchObject({
      status: 404,
    });
  });
});

describe("filenameFromDisposition", () => {
  it.each([
    ['attachment; filename="skolni rad.pdf"', "skolni rad.pdf"],
    ["attachment; filename=plan.docx", "plan.docx"],
    ["attachment; filename=\"a.pdf\"; filename*=UTF-8''%C5%A1kola.pdf", "škola.pdf"],
    ["attachment", null],
    [null, null],
  ])("%s", (header, expected) => {
    expect(filenameFromDisposition(header)).toBe(expected);
  });
});
