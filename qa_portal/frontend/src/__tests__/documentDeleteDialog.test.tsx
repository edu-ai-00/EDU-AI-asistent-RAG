import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ApiClient } from "../api/client";
import { ApiError, type DocumentDetail } from "../api/types";
import { DocumentDeleteDialog } from "../components/DocumentDeleteDialog";
import { strings } from "../strings.cs";

afterEach(cleanup);

const s = strings.documentDelete;
const SCHOOL = "ZS_KAVCI_HORY";
const UUID = "0b6f2c1e-6a4d-4c1b-9a51-2d1f3e5a7b01";
const OLDER = "0b6f2c1e-6a4d-4c1b-9a51-2d1f3e5a7b00";

const DETAIL: DocumentDetail = {
  document: {
    file_uuid: UUID,
    title: "https://www.zs-kavcihory.example/skolni-rad",
    source_kind: "web",
    source_url: "https://www.zs-kavcihory.example/skolni-rad",
    original_file_name: null,
    document_type: "SCHOOL_RULES",
    document_language: "cs",
    school_subject: null,
    school_grade_codes: [],
    domain_codes: [],
    audience_roles: ["PARENT"],
    visibility: "entity",
    valid_from: null,
    valid_to: null,
    user_name: "qa_portal__ZS_KAVCI_HORY",
    ingested_at: "2026-10-20T08:15:00Z",
    is_active: true,
    version_count: 2,
    chunk_count: 14,
  },
  versions: [
    { file_uuid: UUID, processing_uuid: "p2", ingested_at: "2026-10-20T08:15:00Z", is_active: true, chunk_count: 14 },
    { file_uuid: OLDER, processing_uuid: "p1", ingested_at: "2026-09-02T07:00:00Z", is_active: false, chunk_count: 12 },
  ],
};

function renderDialog(client: Partial<ApiClient>) {
  const onDeleted = vi.fn();
  const onClose = vi.fn();
  render(
    <DocumentDeleteDialog
      client={client as ApiClient}
      school={SCHOOL}
      schoolName="ZŠ Kavčí hory"
      fileUuid={UUID}
      onClose={onClose}
      onDeleted={onDeleted}
    />,
  );
  return { onDeleted, onClose };
}

describe("DocumentDeleteDialog (CONTRACT.md §14.3)", () => {
  it("shows school, title, versions and chunks, warns it is irreversible, then deletes with confirm_file_uuid", async () => {
    const deleteDocument = vi.fn(async () => ({
      deleted: { versions: 2, chunks: 26, elements: 40, tables: 1 },
      file_uuids: [UUID, OLDER],
      files_removed: 0,
      warnings: ["Soubor se nepodařilo odstranit z úložiště."],
    }));
    const { onDeleted } = renderDialog({ getDocument: vi.fn(async () => DETAIL), deleteDocument });

    expect(screen.getByText(/ZŠ Kavčí hory \(ZS_KAVCI_HORY\)/)).toBeTruthy();
    expect(await screen.findByText(s.scopeTemplate(2, 26))).toBeTruthy();
    expect(screen.getByText(DETAIL.document.title)).toBeTruthy();
    expect(screen.getByText(s.warning)).toBeTruthy();
    expect(screen.getByText(s.alternative)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: s.confirm }));
    await waitFor(() => expect(deleteDocument).toHaveBeenCalledWith(SCHOOL, UUID, { confirm_file_uuid: UUID }));
    expect(await screen.findByText(s.resultTemplate(2, 26, 40, 1, 0))).toBeTruthy();
    expect(screen.getByText("Soubor se nepodařilo odstranit z úložiště.")).toBeTruthy();
    expect(onDeleted).toHaveBeenCalledTimes(1);
  });

  it("keeps the button disabled until the detail is loaded", () => {
    renderDialog({ getDocument: () => new Promise(() => {}), deleteDocument: vi.fn() });
    expect((screen.getByRole("button", { name: s.confirm }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(s.loading)).toBeTruthy();
  });

  it("shows the server refusal instead of reporting success", async () => {
    const deleteDocument = vi.fn(async () => {
      throw new ApiError(404, { code: "NOT_FOUND", message: "Dokument nenalezen.", retryable: false });
    });
    const { onDeleted } = renderDialog({ getDocument: vi.fn(async () => DETAIL), deleteDocument });
    await screen.findByText(s.scopeTemplate(2, 26));
    fireEvent.click(screen.getByRole("button", { name: s.confirm }));
    expect(await screen.findByText("Dokument nenalezen.")).toBeTruthy();
    expect(onDeleted).not.toHaveBeenCalled();
  });
});
