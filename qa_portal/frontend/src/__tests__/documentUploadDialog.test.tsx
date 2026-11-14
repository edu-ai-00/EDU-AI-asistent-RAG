import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ApiClient } from "../api/client";
import { ApiError, type DocumentJob, type DocumentOptions, type DocumentUploadItem } from "../api/types";
import { createMockClient } from "../api/mockClient";
import { DocumentUploadDialog } from "../components/DocumentUploadDialog";
import { strings } from "../strings.cs";
import optionsFixture from "../fixtures/documentOptions.json";

// Production polls every 5 s; the test only shortens the clock.
vi.mock("../config", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../config")>()),
  DOCUMENT_JOBS_POLL_INTERVAL_MS: 10,
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const t = strings.documentUpload;
const SCHOOL = "ZS_KAVCI_HORY";
const OPTIONS: DocumentOptions = {
  ...(optionsFixture as unknown as DocumentOptions),
  domains: [{ code: "SAFETY", label: "Bezpečnost" }],
};

function file(name: string, content = "obsah"): File {
  return new File([content], name, { type: "application/octet-stream" });
}

function job(over: Partial<DocumentJob>): DocumentJob {
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

function renderDialog(client: Partial<ApiClient>, options = OPTIONS, onClose = vi.fn()) {
  render(
    <DocumentUploadDialog
      client={client as ApiClient}
      school={SCHOOL}
      options={options}
      uiDefaultLang="cs"
      onClose={onClose}
    />,
  );
  return onClose;
}

function addFiles(files: File[]) {
  fireEvent.change(screen.getByLabelText(t.filesLabel), { target: { files } });
}

function fillMetadata() {
  fireEvent.click(screen.getByLabelText(strings.newPack.visibilityEntity));
  fireEvent.click(screen.getByLabelText("Rodič"));
  fireEvent.change(screen.getByLabelText(strings.documentMetadata.validity), { target: { value: "none" } });
}

function submitButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: t.submit }) as HTMLButtonElement;
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("DocumentUploadDialog — client-side validation", () => {
  it("lists several files with sizes and rejects bad extension, empty, oversized and duplicate names", () => {
    renderDialog({}, { ...OPTIONS, max_file_bytes: 10 });
    addFiles([file("rad.pdf"), file("otazky.csv"), file("prazdny.pptx", ""), file("velky.docx", "x".repeat(20)), file("rad.pdf")]);

    expect(screen.getAllByText("rad.pdf")).toHaveLength(2);
    expect(screen.getAllByText("5 B").length).toBeGreaterThan(0);
    expect(screen.getByText(t.errExtension(".csv"))).toBeTruthy();
    expect(screen.getByText(t.errEmpty)).toBeTruthy();
    expect(screen.getByText(t.errTooLarge("10 B"))).toBeTruthy();
    expect(screen.getByText(t.errDuplicateName)).toBeTruthy();

    fillMetadata();
    expect(submitButton().disabled).toBe(true);
    expect(screen.getByText(t.fixItems)).toBeTruthy();

    // Remove the four bad ones (second "rad.pdf" is the duplicate).
    fireEvent.click(screen.getAllByRole("button", { name: t.removeFile("rad.pdf") })[1]);
    fireEvent.click(screen.getByRole("button", { name: t.removeFile("otazky.csv") }));
    fireEvent.click(screen.getByRole("button", { name: t.removeFile("prazdny.pptx") }));
    fireEvent.click(screen.getByRole("button", { name: t.removeFile("velky.docx") }));
    expect(submitButton().disabled).toBe(false);
  });

  it("validates URL lines (http/https only, no duplicates) and the child-link count", () => {
    renderDialog({});
    fireEvent.click(screen.getByRole("tab", { name: /Webové stránky/ }));
    fireEvent.change(screen.getByLabelText(t.urlsLabel), {
      target: { value: "https://skola.example/a\nftp://skola.example/b\n\nhttps://skola.example/a\nnení adresa" },
    });
    expect(screen.getByText(t.urlLineTemplate(2, "ftp://skola.example/b", t.errUrlInvalid))).toBeTruthy();
    expect(screen.getByText(t.urlLineTemplate(4, "https://skola.example/a", t.errUrlDuplicate))).toBeTruthy();
    expect(screen.getByText(t.urlLineTemplate(5, "není adresa", t.errUrlInvalid))).toBeTruthy();

    fillMetadata();
    expect(submitButton().disabled).toBe(true);

    fireEvent.change(screen.getByLabelText(t.urlsLabel), { target: { value: "https://skola.example/a\nhttp://skola.example/b" } });
    expect(submitButton().disabled).toBe(false);

    fireEvent.click(screen.getByLabelText(t.childLinks));
    const count = screen.getByLabelText(t.childLinksCount(10)) as HTMLInputElement;
    expect(count.value).toBe("5");
    fireEvent.change(count, { target: { value: "11" } });
    expect(screen.getByText(t.errChildLinks(10))).toBeTruthy();
    expect(submitButton().disabled).toBe(true);
    fireEvent.change(count, { target: { value: "3" } });
    expect(submitButton().disabled).toBe(false);
  });

  it("defaults nothing: visibility, a role and validity must be chosen; from must not be after to", () => {
    renderDialog({});
    addFiles([file("rad.pdf")]);
    expect(submitButton().disabled).toBe(true);
    expect(screen.getByText(t.missingVisibility)).toBeTruthy();
    expect(screen.getByText(t.missingRole)).toBeTruthy();
    expect(screen.getByText(t.missingValidity)).toBeTruthy();

    fireEvent.click(screen.getByLabelText(strings.newPack.visibilityOrganization));
    fireEvent.click(screen.getByLabelText("Učitel"));
    fireEvent.change(screen.getByLabelText(strings.documentMetadata.validity), { target: { value: "custom" } });
    expect(screen.getByText(strings.documentMetadata.errCustomEmpty)).toBeTruthy();
    fireEvent.change(screen.getByLabelText(strings.documentMetadata.validFrom), { target: { value: "2027-01-01" } });
    fireEvent.change(screen.getByLabelText(strings.documentMetadata.validTo), { target: { value: "2026-01-01" } });
    expect(screen.getByText(strings.documentMetadata.errRange)).toBeTruthy();
    expect(submitButton().disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(strings.documentMetadata.validTo), { target: { value: "2027-06-30" } });
    expect(submitButton().disabled).toBe(false);
  });
});

describe("DocumentUploadDialog — sending", () => {
  it("sends items one by one with one batch_id, shows a per-item server error and the job result", async () => {
    const calls: DocumentUploadItem[] = [];
    const pending = [deferred<DocumentJob>(), deferred<DocumentJob>(), deferred<DocumentJob>()];
    const uploadDocument = vi.fn((_school: string, item: DocumentUploadItem) => {
      calls.push(item);
      return pending[calls.length - 1].promise;
    });
    const listDocumentJobs = vi.fn(async () => [
      job({ job_id: "j1", source: "a.pdf", status: "completed", finished_at: new Date().toISOString(), stored_chunks: 7 }),
      job({ job_id: "j3", source_kind: "url", source: "https://skola.example/x", status: "processing" }),
    ]);
    renderDialog({ uploadDocument, listDocumentJobs });

    addFiles([file("a.pdf"), file("b.pdf")]);
    fireEvent.click(screen.getByRole("tab", { name: /Webové stránky/ }));
    fireEvent.change(screen.getByLabelText(t.urlsLabel), { target: { value: "https://skola.example/x" } });
    fillMetadata();
    fireEvent.change(screen.getByLabelText(strings.documentMetadata.documentType), { target: { value: "SCHOOL_RULES" } });
    fireEvent.click(screen.getByLabelText("Bezpečnost"));
    fireEvent.click(submitButton());

    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(1));
    expect(screen.getAllByText(t.statePending)).toHaveLength(2);
    await new Promise((r) => setTimeout(r, 30));
    expect(uploadDocument).toHaveBeenCalledTimes(1); // never in parallel

    pending[0].resolve(job({ job_id: "j1", source: "a.pdf" }));
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(2));
    pending[1].reject(
      new ApiError(422, {
        code: "VALIDATION_ERROR",
        message: "Metadata neprošla kontrolou.",
        retryable: false,
        fields: [{ field: "metadata.document_type", message: "unknown document type" }],
      }),
    );
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(3));
    pending[2].resolve(job({ job_id: "j3", source_kind: "url", source: "https://skola.example/x" }));

    expect(await screen.findByText(t.stateRejected("Metadata neprošla kontrolou."))).toBeTruthy();
    expect(screen.getByText("metadata.document_type: unknown document type")).toBeTruthy();
    expect(await screen.findByText(t.jobCompletedChunks(7))).toBeTruthy();
    expect(await screen.findByText(t.jobProcessing)).toBeTruthy();
    expect(listDocumentJobs).toHaveBeenCalledWith(SCHOOL, { batch_id: calls[0].batch_id }, expect.anything());

    expect(new Set(calls.map((c) => c.batch_id)).size).toBe(1);
    expect(calls[0].file?.name).toBe("a.pdf");
    expect(calls[0].url).toBeUndefined();
    expect(calls[2]).toMatchObject({ url: "https://skola.example/x", save_level_1_child: false, file: undefined });
    expect(calls[0].metadata).toEqual({
      visibility: "entity",
      audience_roles: ["PARENT"],
      document_type: "SCHOOL_RULES",
      document_language: null,
      school_subject: null,
      school_grade_codes: [],
      domain_codes: ["SAFETY"],
      valid_from: null,
      valid_to: null,
    });
  });

  it("polls the mock queue until the job is completed with chunks, then stops polling", async () => {
    const real = createMockClient();
    const listDocumentJobs = vi.fn(real.listDocumentJobs);
    const options = await real.getDocumentOptions(SCHOOL);
    renderDialog({ ...real, listDocumentJobs }, options);

    addFiles([file("Rozvrh.pdf"), file("chyba.pdf")]);
    fillMetadata();
    fireEvent.click(submitButton());

    expect(await screen.findByText(/hotovo · uloženo \d+ chunků/, {}, { timeout: 4000 })).toBeTruthy();
    expect(await screen.findByText(/^selhalo:/, {}, { timeout: 4000 })).toBeTruthy();
    await new Promise((r) => setTimeout(r, 100));
    const settled = listDocumentJobs.mock.calls.length;
    await new Promise((r) => setTimeout(r, 150));
    expect(listDocumentJobs.mock.calls.length).toBe(settled);
  });

  it("explains an `unknown` job state", async () => {
    const uploadDocument = vi.fn(async () => job({ job_id: "j1" }));
    const listDocumentJobs = vi.fn(async () => [job({ job_id: "j1", status: "unknown" })]);
    renderDialog({ uploadDocument, listDocumentJobs });
    addFiles([file("a.pdf")]);
    fillMetadata();
    fireEvent.click(submitButton());
    expect(await screen.findByText(t.jobUnknown)).toBeTruthy();
  });

  it("asks before closing with unsent items, and never sends them after confirming", async () => {
    const first = deferred<DocumentJob>();
    const uploadDocument = vi.fn(() => first.promise);
    const onClose = renderDialog({ uploadDocument, listDocumentJobs: vi.fn(async () => []) });
    addFiles([file("a.pdf"), file("b.pdf")]);
    fillMetadata();
    fireEvent.click(submitButton());
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(1));

    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    const footerClose = () => {
      const buttons = screen.getAllByRole("button", { name: t.close });
      return buttons[buttons.length - 1];
    };
    fireEvent.click(footerClose());
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(String(confirm.mock.calls[0][0])).toContain("b.pdf");
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeTruthy();

    confirm.mockReturnValueOnce(true);
    fireEvent.click(footerClose());
    expect(onClose).toHaveBeenCalledWith(true);

    first.resolve(job({ job_id: "j1" }));
    await new Promise((r) => setTimeout(r, 30));
    expect(uploadDocument).toHaveBeenCalledTimes(1);
  });

  it("closes without asking once everything is sent", async () => {
    const uploadDocument = vi.fn(async () => job({ job_id: "j1" }));
    const listDocumentJobs = vi.fn(async () => [job({ job_id: "j1", status: "processing" })]);
    const onClose = renderDialog({ uploadDocument, listDocumentJobs });
    addFiles([file("a.pdf")]);
    fillMetadata();
    fireEvent.click(submitButton());
    await screen.findByText(t.jobProcessing);

    const confirm = vi.spyOn(window, "confirm");
    const dialog = screen.getByRole("dialog");
    const buttons = within(dialog).getAllByRole("button", { name: t.close });
    fireEvent.click(buttons[buttons.length - 1]);
    expect(confirm).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledWith(true);
  });
});
