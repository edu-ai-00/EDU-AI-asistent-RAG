import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ApiClient } from "../api/client";
import type { MetadataOptions } from "../api/types";
import { strings } from "../strings.cs";
import { NewPackDialog } from "../components/NewPackDialog";

afterEach(() => {
  cleanup();
});

function notImplemented(name: string) {
  return (...args: unknown[]) => {
    throw new Error(`${name} not stubbed in this test (args: ${JSON.stringify(args)})`);
  };
}

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
    deleteEntry: notImplemented("deleteEntry"),
    deletePack: notImplemented("deletePack"),
    ...overrides,
  } as unknown as ApiClient;
}

const METADATA_OPTIONS: MetadataOptions = {
  visibility: ["entity", "organization"],
  audience_roles: [
    { code: "TEACHER", label: "Učitel" },
    { code: "PARENT", label: "Rodič" },
  ],
  school_grades: [
    { code: "1", label: "1. ročník" },
    { code: "2", label: "2. ročník" },
    { code: "3", label: "3. ročník" },
    { code: "4", label: "4. ročník" },
    { code: "5", label: "5. ročník" },
    { code: "6", label: "6. ročník" },
    { code: "7", label: "7. ročník" },
    { code: "8", label: "8. ročník" },
    { code: "9", label: "9. ročník" },
  ],
};

describe("NewPackDialog on a small/short viewport (Job 1)", () => {
  it("has a scrollable body (min-height:0 grid item, explicit dialog grid rows) holding the grade grid, with the confirm button in a fixed footer outside it", async () => {
    const client = makeClient({ getMetadataOptions: async () => METADATA_OPTIONS });
    render(<NewPackDialog client={client} school="ZS_X" onClose={() => {}} onCreated={() => {}} />);

    // Options load asynchronously.
    await screen.findByRole("checkbox", { name: "1. ročník" });

    // NOTE: jsdom does not lay out or compute overflow/scroll geometry, so
    // this file cannot prove the content is actually reachable by
    // wheel/touch/keyboard on a short viewport — only the DOM/attribute
    // shape that CONTRACT.md §10.1 requires. The browser test is what
    // proves reachability.
    const dialogBody = document.querySelector(".dialog-body") as HTMLElement;
    expect(dialogBody).toBeTruthy();

    const grid = dialogBody.querySelector(".checkbox-grid") as HTMLElement;
    expect(grid).toBeTruthy();
    // Dense multi-column, not one long column.
    expect(grid.querySelectorAll(".checkbox-row").length).toBe(METADATA_OPTIONS.school_grades.length);
    expect(dialogBody.contains(grid)).toBe(true);

    // The longest list's last item lives inside the scrolling body.
    const lastGrade = screen.getByRole("checkbox", { name: "9. ročník" });
    expect(dialogBody.contains(lastGrade)).toBe(true);

    // The scroll region is a real, reachable, named focusable area — but it
    // must not steal initial focus. Today's shell focuses the first
    // focusable element in DOM order (the dialog's own "×" close button,
    // which sits in the header ahead of the body); the scroll region must
    // not jump ahead of that once it becomes focusable too.
    expect(dialogBody.getAttribute("tabindex")).toBe("0");
    expect(dialogBody.getAttribute("aria-label")).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: strings.actions.close }));
    expect(document.activeElement).not.toBe(dialogBody);

    // The confirm/cancel footer is a fixed region OUTSIDE the scrolled body,
    // not clipped by .dialog's overflow:hidden + max-height.
    const footer = document.querySelector(".dialog-footer") as HTMLElement;
    expect(footer).toBeTruthy();
    expect(dialogBody.contains(footer)).toBe(false);

    const cancelButton = screen.getByRole("button", { name: strings.actions.cancel }) as HTMLButtonElement;
    expect(cancelButton.disabled).toBe(false);
    expect(dialogBody.contains(cancelButton)).toBe(false);
    expect(footer.contains(cancelButton)).toBe(true);

    // Confirm only enables once visibility + a role are picked — reachable
    // and clickable, not clipped out of the DOM/flow.
    fireEvent.change(screen.getByLabelText(strings.newPack.packId), { target: { value: "novy-pack" } });
    fireEvent.click(screen.getByRole("radio", { name: strings.newPack.visibilityEntity }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Učitel" }));
    const createButton = screen.getByRole("button", { name: strings.newPack.create }) as HTMLButtonElement;
    expect(createButton.disabled).toBe(false);
    expect(dialogBody.contains(createButton)).toBe(false);
    expect(footer.contains(createButton)).toBe(true);
  });
});
