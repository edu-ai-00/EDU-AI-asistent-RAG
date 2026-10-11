import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import App from "../App";
import { strings } from "../strings.cs";
import { buildEntryWrite } from "../lib/entryWrite";
import { draftFromEntry } from "../lib/drafts";
import { createMockClient } from "../api/mockClient";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// The default school picked on load is schools.json[0] = ZS_KAVCI_HORY, whose
// only pack (faq-provoz) has this question; ZS_JEDOVNICE only has "skolni-druzina".
const KAVCI_HORY_QUESTION = "Do kdy musím omluvit nepřítomnost dítěte ve škole?";
const JEDOVNICE_QUESTION = "Do kolika hodin je otevřená družina?";

async function selectSchool(nextLabel: string) {
  const select = screen.getByLabelText(strings.toolbar.school) as HTMLSelectElement;
  fireEvent.change(select, { target: { value: nextLabel } });
}

describe("school and pack selection with the entry list", () => {
  it("lets the admin pick a school, see its packs, and see its entries", async () => {
    render(<App />);

    const schoolSelect = await screen.findByLabelText(strings.toolbar.school);
    expect((schoolSelect as HTMLSelectElement).value).toBe("ZS_KAVCI_HORY");

    const packSelect = await screen.findByLabelText(strings.toolbar.pack);
    await waitFor(() => expect((packSelect as HTMLSelectElement).value).toBe("faq-provoz"));

    expect(await screen.findByText(KAVCI_HORY_QUESTION)).toBeTruthy();
  });

  it("does not mix entries when switching schools", async () => {
    render(<App />);
    expect(await screen.findByText(KAVCI_HORY_QUESTION)).toBeTruthy();

    await selectSchool("ZS_JEDOVNICE");

    expect(await screen.findByText(JEDOVNICE_QUESTION)).toBeTruthy();
    expect(screen.queryByText(KAVCI_HORY_QUESTION)).toBeNull();
  });

  it("passes the search box query through to listEntries", async () => {
    render(<App />);
    await screen.findByText(KAVCI_HORY_QUESTION);

    const search = screen.getByLabelText(strings.list.searchLabel);
    fireEvent.change(search, { target: { value: "družin" } });

    await waitFor(() => expect(screen.queryByText(KAVCI_HORY_QUESTION)).toBeNull());
    expect(await screen.findByText("Kolik stojí školní družina a jak se platí?")).toBeTruthy();
  });
});

describe("unsaved changes when switching schools", () => {
  it("keeps the original school and draft when the confirm is rejected, switches when accepted", async () => {
    render(<App />);
    fireEvent.click(await screen.findByText(KAVCI_HORY_QUESTION));

    const answer = (await screen.findByLabelText(strings.editor.answer)) as HTMLTextAreaElement;
    fireEvent.change(answer, { target: { value: "Rozepsaná, nezveřejněná odpověď." } });
    expect(answer.value).toBe("Rozepsaná, nezveřejněná odpověď.");

    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    await selectSchool("ZS_JEDOVNICE");
    expect(confirmSpy).toHaveBeenCalledTimes(1);

    // Rejected: still the original school, entry list unchanged, draft kept.
    expect((screen.getByLabelText(strings.toolbar.school) as HTMLSelectElement).value).toBe("ZS_KAVCI_HORY");
    expect(screen.getByText(KAVCI_HORY_QUESTION)).toBeTruthy();
    expect((screen.getByLabelText(strings.editor.answer) as HTMLTextAreaElement).value).toBe(
      "Rozepsaná, nezveřejněná odpověď.",
    );

    confirmSpy.mockReturnValueOnce(true);
    await selectSchool("ZS_JEDOVNICE");
    expect(confirmSpy).toHaveBeenCalledTimes(2);
    expect(await screen.findByText(JEDOVNICE_QUESTION)).toBeTruthy();
  });
});

describe("entry editor preserves key and metadata", () => {
  it("buildEntryWrite keeps the loaded entry_key and adds no extra metadata field after an answer edit", async () => {
    render(<App />);
    fireEvent.click(await screen.findByText(KAVCI_HORY_QUESTION));

    const answer = (await screen.findByLabelText(strings.editor.answer)) as HTMLTextAreaElement;
    fireEvent.change(answer, { target: { value: "Nová odpověď z editoru." } });
    expect(answer.value).toBe("Nová odpověď z editoru.");

    // Cross-check with the same building block the app uses: editing only the
    // answer must not touch entry_key, and the write shape must be exactly
    // the EntryWrite fields — no owner_user_id/visibility/etc leaking in.
    const client = createMockClient();
    const { entry } = await client.getEntry("ZS_KAVCI_HORY", "faq-provoz", "omluva-lhuta");
    const draft = draftFromEntry(entry);
    draft.answer_md = "Nová odpověď z editoru.";
    const write = buildEntryWrite(entry.entry_key, draft);

    expect(write.entry_key).toBe("omluva-lhuta");
    expect(write.answer_md).toBe("Nová odpověď z editoru.");
    expect(Object.keys(write).sort()).toEqual(
      ["answer_md", "canonical_q", "category_code", "domains", "entry_key", "is_active", "sources", "variants"].sort(),
    );
  });
});

describe("category is a catalog choice", () => {
  it("renders the category as a <select> and still shows an inactive category", async () => {
    render(<App />);
    fireEvent.click(await screen.findByText("Kolik stojí školní družina a jak se platí?"));

    fireEvent.click(await screen.findByText(strings.editor.detailsSummary));

    const topicSelect = (await screen.findByLabelText(strings.editor.topic)) as HTMLSelectElement;
    expect(topicSelect.tagName).toBe("SELECT");

    const options = within(topicSelect).getAllByRole("option");
    const labels = options.map((o) => o.textContent || "");
    expect(labels.some((l) => l.includes("Jídelna (neaktivní)"))).toBe(true);
    expect(labels.some((l) => l.includes(strings.editor.topicInactiveHint))).toBe(true);
  });
});

describe("markdown preview", () => {
  it("renders formatting and does not execute injected markup", async () => {
    render(<App />);
    fireEvent.click(await screen.findByText(KAVCI_HORY_QUESTION));

    const answer = (await screen.findByLabelText(strings.editor.answer)) as HTMLTextAreaElement;
    fireEvent.change(answer, {
      target: { value: "**tučně** a <img src=x onerror=\"window.__xss=true\"><script>window.__xss=true;</script>" },
    });

    fireEvent.click(screen.getByText(strings.editor.previewButton));

    const preview = document.querySelector(".markdown-preview") as HTMLElement;
    expect(preview).toBeTruthy();
    expect(preview.querySelector("strong")?.textContent).toBe("tučně");
    expect(preview.querySelector("script")).toBeNull();
    expect(preview.innerHTML).not.toContain("onerror");
    expect((window as unknown as { __xss?: boolean }).__xss).toBeUndefined();
  });
});
