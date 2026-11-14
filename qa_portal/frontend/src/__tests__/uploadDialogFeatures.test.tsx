import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ApiClient } from "../api/client";
import type { DocumentJob, DocumentOptions, DocumentUploadItem } from "../api/types";
import { createMockClient } from "../api/mockClient";
import { DocumentUploadDialog } from "../components/DocumentUploadDialog";
import { allSchoolGradeCodes } from "../lib/grades";
import { strings } from "../strings.cs";
import optionsFixture from "../fixtures/documentOptions.json";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const t = strings.documentUpload;
const m = strings.documentMetadata;
const SCHOOL = "ZS_KAVCI_HORY";
const GRADES = [
  { code: "1", label: "1. ročník" },
  { code: "2", label: "2. ročník" },
  { code: "3", label: "3. ročník" },
  { code: "9999", label: "Pro všechny ročníky" },
];
const OPTIONS: DocumentOptions = {
  ...(optionsFixture as unknown as DocumentOptions),
  school_grades: GRADES,
  domains: [{ code: "SAFETY", label: "Bezpečnost" }],
};

function file(name: string): File {
  return new File(["obsah"], name, { type: "application/octet-stream" });
}

function job(over: Partial<DocumentJob> = {}): DocumentJob {
  return {
    job_id: "job-1",
    batch_id: "b",
    school: SCHOOL,
    source_kind: "file",
    source: "a.pdf",
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

function renderDialog(client: Partial<ApiClient>, options: DocumentOptions = OPTIONS, school = SCHOOL) {
  render(
    <DocumentUploadDialog client={client as ApiClient} school={school} options={options} uiDefaultLang="cs" onClose={vi.fn()} />,
  );
}

function addFiles(files: File[]) {
  fireEvent.change(screen.getByLabelText(t.filesLabel), { target: { files } });
}

function fillDefaults() {
  fireEvent.click(screen.getByLabelText(strings.newPack.visibilityEntity));
  fireEvent.click(screen.getByLabelText("Rodič"));
  fireEvent.change(screen.getByLabelText(m.validity), { target: { value: "none" } });
}

function submit(): HTMLButtonElement {
  return screen.getByRole("button", { name: t.submit }) as HTMLButtonElement;
}

function editor(label: string): HTMLElement {
  return screen.getByRole("group", { name: t.itemEditorHeading(label) });
}

function openEditor(label: string): HTMLElement {
  fireEvent.click(screen.getByRole("button", { name: t.itemEditLabel(label) }));
  return editor(label);
}

describe("DocumentUploadDialog — per-item metadata", () => {
  it("sends the batch defaults for most items and a customised set for an overridden one", async () => {
    const calls: DocumentUploadItem[] = [];
    const uploadDocument = vi.fn(async (_s: string, item: DocumentUploadItem) => {
      calls.push(item);
      return job({ job_id: `j${calls.length}` });
    });
    renderDialog({ uploadDocument, listDocumentJobs: vi.fn(async () => []) });
    addFiles([file("a.pdf"), file("b.pdf"), file("c.pdf")]);
    fillDefaults();
    fireEvent.change(screen.getByLabelText(m.documentType), { target: { value: "MENU" } });

    expect(screen.queryByText(t.itemOverrideChip)).toBeNull();
    const ed = openEditor("b.pdf");
    // The editor starts from the defaults.
    expect((within(ed).getByLabelText(m.documentType) as HTMLSelectElement).value).toBe("MENU");
    expect((within(ed).getByLabelText("Rodič") as HTMLInputElement).checked).toBe(true);

    fireEvent.change(within(ed).getByLabelText(m.documentType), { target: { value: "SCHOOL_RULES" } });
    fireEvent.click(within(ed).getByLabelText("Učitel"));
    fireEvent.click(within(ed).getByLabelText(strings.newPack.visibilityOrganization));
    fireEvent.click(within(ed).getByLabelText("Bezpečnost"));
    fireEvent.change(within(ed).getByLabelText(m.validity), { target: { value: "period:1" } });
    expect(screen.getAllByText(t.itemOverrideChip)).toHaveLength(1);

    // The defaults are untouched.
    // The batch defaults come last in the DOM, after the item list.
    expect((screen.getAllByLabelText(m.documentType).at(-1) as HTMLSelectElement).value).toBe("MENU");

    fireEvent.click(submit());
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(3));
    const byName = Object.fromEntries(calls.map((c) => [c.file!.name, c.metadata]));
    expect(byName["a.pdf"]).toEqual({
      visibility: "entity",
      audience_roles: ["PARENT"],
      document_type: "MENU",
      document_language: null,
      school_subject: null,
      school_grade_codes: [],
      domain_codes: [],
      valid_from: null,
      valid_to: null,
    });
    expect(byName["c.pdf"]).toEqual(byName["a.pdf"]);
    expect(byName["b.pdf"]).toEqual({
      visibility: "organization",
      audience_roles: ["PARENT", "TEACHER"],
      document_type: "SCHOOL_RULES",
      document_language: null,
      school_subject: null,
      school_grade_codes: [],
      domain_codes: ["SAFETY"],
      valid_from: "2026-09-01",
      valid_to: "2027-08-31",
    });
  });

  it("'Obnovit výchozí' drops the override and the chip, so the item follows the defaults again", async () => {
    const calls: DocumentUploadItem[] = [];
    const uploadDocument = vi.fn(async (_s: string, item: DocumentUploadItem) => {
      calls.push(item);
      return job();
    });
    renderDialog({ uploadDocument, listDocumentJobs: vi.fn(async () => []) });
    addFiles([file("a.pdf")]);
    fillDefaults();
    const ed = openEditor("a.pdf");
    const reset = within(ed).getByRole("button", { name: t.itemResetDefaults }) as HTMLButtonElement;
    expect(reset.disabled).toBe(true); // nothing to reset yet

    fireEvent.change(within(ed).getByLabelText(m.documentType), { target: { value: "SCHEDULE" } });
    expect(screen.getByText(t.itemOverrideChip)).toBeTruthy();
    expect(reset.disabled).toBe(false);
    fireEvent.click(reset);
    expect(screen.queryByText(t.itemOverrideChip)).toBeNull();
    expect((within(editor("a.pdf")).getByLabelText(m.documentType) as HTMLSelectElement).value).toBe("");

    // Changing the defaults later reaches the item again.
    fireEvent.change(screen.getAllByLabelText(m.documentType).at(-1)!, { target: { value: "MENU" } });
    fireEvent.click(submit());
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(1));
    expect(calls[0].metadata.document_type).toBe("MENU");
  });

  it("an incomplete override blocks sending until it is fixed or reset", () => {
    renderDialog({});
    addFiles([file("a.pdf"), file("b.pdf")]);
    fillDefaults();
    expect(submit().disabled).toBe(false);
    const ed = openEditor("b.pdf");
    fireEvent.click(within(ed).getByLabelText("Rodič")); // no role left
    expect(within(ed).getByText(t.itemMetaIncomplete)).toBeTruthy();
    expect(submit().disabled).toBe(true);
    expect(screen.getByText(t.fixItemMetadata)).toBeTruthy();
    fireEvent.click(within(ed).getByRole("button", { name: t.itemResetDefaults }));
    expect(submit().disabled).toBe(false);
  });

  it("an item with its own complete metadata does not need the defaults", () => {
    renderDialog({});
    addFiles([file("a.pdf")]);
    expect(submit().disabled).toBe(true);
    const ed = openEditor("a.pdf");
    fireEvent.click(within(ed).getByLabelText(strings.newPack.visibilityEntity));
    fireEvent.click(within(ed).getByLabelText("Žák"));
    fireEvent.change(within(ed).getByLabelText(m.validity), { target: { value: "none" } });
    expect(submit().disabled).toBe(false);
  });
});

describe("DocumentUploadDialog — select all grades", () => {
  it("allSchoolGradeCodes skips 9999 and falls back to it when it is the only grade", () => {
    expect(allSchoolGradeCodes(GRADES)).toEqual(["1", "2", "3"]);
    expect(allSchoolGradeCodes([{ code: "9999", label: "Pro všechny ročníky" }])).toEqual(["9999"]);
    expect(allSchoolGradeCodes([])).toEqual([]);
  });

  it("checks every grade but 9999 in the batch defaults and sends them", async () => {
    const calls: DocumentUploadItem[] = [];
    const uploadDocument = vi.fn(async (_s: string, item: DocumentUploadItem) => {
      calls.push(item);
      return job();
    });
    renderDialog({ uploadDocument, listDocumentJobs: vi.fn(async () => []) });
    addFiles([file("a.pdf")]);
    fillDefaults();
    fireEvent.click(screen.getByRole("button", { name: t.selectAllGrades }));
    for (const label of ["1. ročník", "2. ročník", "3. ročník"]) {
      expect((screen.getByLabelText(label) as HTMLInputElement).checked).toBe(true);
    }
    expect((screen.getByLabelText("Pro všechny ročníky") as HTMLInputElement).checked).toBe(false);
    fireEvent.click(submit());
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(1));
    expect(calls[0].metadata.school_grade_codes).toEqual(["1", "2", "3"]);
  });

  it("checks 9999 when the school has no other grade", () => {
    renderDialog({}, { ...OPTIONS, school_grades: [{ code: "9999", label: "Pro všechny ročníky" }] });
    fireEvent.click(screen.getByRole("button", { name: t.selectAllGrades }));
    expect((screen.getByLabelText("Pro všechny ročníky") as HTMLInputElement).checked).toBe(true);
  });

  it("is also in the per-item editor and only touches that item", async () => {
    const calls: DocumentUploadItem[] = [];
    const uploadDocument = vi.fn(async (_s: string, item: DocumentUploadItem) => {
      calls.push(item);
      return job();
    });
    renderDialog({ uploadDocument, listDocumentJobs: vi.fn(async () => []) });
    addFiles([file("a.pdf"), file("b.pdf")]);
    fillDefaults();
    const ed = openEditor("b.pdf");
    fireEvent.click(within(ed).getByRole("button", { name: t.selectAllGrades }));
    expect((within(ed).getByLabelText("2. ročník") as HTMLInputElement).checked).toBe(true);
    expect((within(ed).getByLabelText("Pro všechny ročníky") as HTMLInputElement).checked).toBe(false);
    expect(screen.getByText(t.itemOverrideChip)).toBeTruthy();
    fireEvent.click(submit());
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(2));
    const byName = Object.fromEntries(calls.map((c) => [c.file!.name, c.metadata.school_grade_codes]));
    expect(byName).toEqual({ "a.pdf": [], "b.pdf": ["1", "2", "3"] });
  });
});

describe("DocumentUploadDialog — source check", () => {
  it("says which items replace a document and which only share a name with another author's", async () => {
    const real = createMockClient();
    const options = await real.getDocumentOptions("ZS_JEDOVNICE");
    renderDialog(real, options, "ZS_JEDOVNICE");
    addFiles([file("Pololetni_zprava.pdf"), file("Nova_zprava.pdf")]);
    fireEvent.click(screen.getByRole("tab", { name: /Webové stránky/ }));
    fireEvent.change(screen.getByLabelText(t.urlsLabel), {
      target: { value: "https://www.zsjedovnice.example/druzina\nhttps://www.zsjedovnice.example/novinky" },
    });

    expect(await screen.findAllByText(t.checkSameNameOtherAuthor)).toHaveLength(1);
    expect(screen.getAllByText(t.checkSupersedes)).toHaveLength(1);
    const otherAuthor = screen.getByText(t.checkSameNameOtherAuthor).closest("li") as HTMLElement;
    expect(within(otherAuthor).getByText("Pololetni_zprava.pdf")).toBeTruthy();
    const replaced = screen.getByText(t.checkSupersedes).closest("li") as HTMLElement;
    expect(within(replaced).getByText("https://www.zsjedovnice.example/druzina")).toBeTruthy();
    // A new name gets no note, and nothing blocks sending.
    expect(screen.getByText("Nova_zprava.pdf").closest("li")?.textContent).not.toMatch(/Nahradí|Stejný název/);
  });

  it("a file by the same uploader is replaced, and the check runs before the first upload", async () => {
    const real = createMockClient();
    const order: string[] = [];
    const checkDocumentSources = vi.fn(async (...args: Parameters<ApiClient["checkDocumentSources"]>) => {
      order.push("check");
      return real.checkDocumentSources(...args);
    });
    const uploadDocument = vi.fn(async (...args: Parameters<ApiClient["uploadDocument"]>) => {
      order.push("upload");
      return real.uploadDocument(...args);
    });
    const options = await real.getDocumentOptions(SCHOOL);
    renderDialog({ ...real, checkDocumentSources, uploadDocument }, options);
    addFiles([file("Provozni_rad_jidelny.pdf")]);
    fillDefaults();
    // Sent at once: the check must still have happened first.
    fireEvent.click(submit());
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(1));
    expect(order[0]).toBe("check");
    expect(checkDocumentSources.mock.calls[0][1]).toEqual([{ kind: "file", value: "Provozni_rad_jidelny.pdf" }]);
    expect(await screen.findByText(t.checkSupersedes)).toBeTruthy();
  });

  it("does not block when the check itself fails", async () => {
    const uploadDocument = vi.fn(async () => job());
    const checkDocumentSources = vi.fn(async () => {
      throw new Error("down");
    });
    renderDialog({ uploadDocument, checkDocumentSources, listDocumentJobs: vi.fn(async () => []) });
    addFiles([file("a.pdf")]);
    fillDefaults();
    fireEvent.click(submit());
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(1));
  });
});

describe("DocumentUploadDialog — URL preview", () => {
  it("needs complete metadata, then lists the parent and child pages the ingest would fetch", async () => {
    const previewDocumentUrl = vi.fn(async (_s: string, body: { url: string }) => [
      { source_url: body.url, source_type: "html", page_role: "parent" as const },
      { source_url: `${body.url}/jidelna`, source_type: "html", page_role: "child" as const },
    ]);
    renderDialog({ previewDocumentUrl, checkDocumentSources: vi.fn(async () => []) });
    fireEvent.click(screen.getByRole("tab", { name: /Webové stránky/ }));
    fireEvent.change(screen.getByLabelText(t.urlsLabel), { target: { value: "https://skola.example/a\nhttps://skola.example/b" } });

    const one = screen.getByRole("button", { name: t.previewLabel("https://skola.example/a") }) as HTMLButtonElement;
    expect(one.disabled).toBe(true);
    expect(screen.getByText(t.previewNeedsMetadata)).toBeTruthy();

    fillDefaults();
    fireEvent.click(screen.getByLabelText(t.childLinks));
    expect(one.disabled).toBe(false);
    fireEvent.click(one);

    expect(await screen.findByText(t.previewHeading(2))).toBeTruthy();
    expect(screen.getByText(`${t.previewParent} – https://skola.example/a`)).toBeTruthy();
    expect(screen.getByText(`${t.previewChild} – https://skola.example/a/jidelna`)).toBeTruthy();
    expect(previewDocumentUrl).toHaveBeenCalledTimes(1);
    expect(previewDocumentUrl.mock.calls[0][1]).toMatchObject({
      url: "https://skola.example/a",
      save_level_1_child: true,
      max_child_links: 5,
      metadata: { visibility: "entity", audience_roles: ["PARENT"], valid_from: null, valid_to: null },
    });
    expect(screen.queryByText(`${t.previewParent} – https://skola.example/b`)).toBeNull();
  });

  it("previews every valid URL with 'Náhled všech' and shows a failure per URL", async () => {
    const previewDocumentUrl = vi.fn(async (_s: string, body: { url: string }) => {
      if (body.url.endsWith("/b")) throw new Error("Gateway odmítl adresu.");
      return [{ source_url: body.url, source_type: "html", page_role: "parent" as const }];
    });
    renderDialog({ previewDocumentUrl, checkDocumentSources: vi.fn(async () => []) });
    fireEvent.click(screen.getByRole("tab", { name: /Webové stránky/ }));
    fireEvent.change(screen.getByLabelText(t.urlsLabel), { target: { value: "https://skola.example/a\nhttps://skola.example/b\nnení adresa" } });
    fillDefaults();
    fireEvent.click(screen.getByRole("button", { name: t.previewAll }));

    expect(await screen.findByText(t.previewError("Gateway odmítl adresu."))).toBeTruthy();
    expect(screen.getByText(`${t.previewParent} – https://skola.example/a`)).toBeTruthy();
    expect(previewDocumentUrl).toHaveBeenCalledTimes(2); // the invalid line is skipped
  });

  it("works against the mock client", async () => {
    const real = createMockClient();
    const options = await real.getDocumentOptions(SCHOOL);
    renderDialog(real, options);
    fireEvent.click(screen.getByRole("tab", { name: /Webové stránky/ }));
    fireEvent.change(screen.getByLabelText(t.urlsLabel), { target: { value: "https://skola.example/a" } });
    fillDefaults();
    fireEvent.click(screen.getByLabelText(t.childLinks));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: t.previewLabel("https://skola.example/a") }));
    });
    expect(await screen.findByText(t.previewHeading(4))).toBeTruthy();
  });
});
