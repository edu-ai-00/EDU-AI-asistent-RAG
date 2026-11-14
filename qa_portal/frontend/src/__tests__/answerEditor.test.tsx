import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import type { ApiClient } from "../api/client";
import type { Entry } from "../api/types";
import { EntryEditor } from "../components/EntryEditor";
import { draftFromEntry, type EntryDraft } from "../lib/drafts";
import { applyMarkdownFormat } from "../lib/markdownToolbar";
import { strings } from "../strings.cs";

const t = strings.editor;

afterEach(() => cleanup());

const ENTRY: Entry = { entry_key: "e1", canonical_q: "Otázka", answer_md: "Hotová odpověď", domains: [], is_active: true };

function Harness({ initial = "Hotová odpověď" }: { initial?: string }) {
  const [draft, setDraft] = useState<EntryDraft>({ ...draftFromEntry(ENTRY), answer_md: initial });
  return (
    <EntryEditor
      entry={ENTRY}
      draft={draft}
      domainsState={{ status: "ready", value: [] }}
      categoriesState={{ status: "ready", value: [] }}
      onChange={(field, value) => setDraft((d) => ({ ...d, [field]: value }))}
      client={{} as ApiClient}
      mayManageCategories={false}
      uiDefaultLang="cs"
      onCategoryCreated={() => {}}
      school="S"
      pack="p"
      onEntryDeleted={() => {}}
    />
  );
}

const answer = () => screen.getByLabelText(t.answer) as HTMLTextAreaElement;

function select(start: number, end: number) {
  const area = answer();
  area.focus();
  area.setSelectionRange(start, end);
}

describe("answer editor – Text | Náhled tabs", () => {
  it("has a small tab switch instead of the full-width preview button", () => {
    render(<Harness />);
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual([t.tabText, t.tabPreview]);
    expect(tabs[0].getAttribute("aria-selected")).toBe("true");
    expect(screen.queryByText("Náhled odpovědi")).toBeNull();
    expect(screen.getByRole("tablist", { name: t.tabsLabel })).toBeTruthy();
    expect(screen.getByRole("toolbar", { name: t.toolbarLabel })).toBeTruthy();
  });

  it("renders the Markdown in the Náhled tab (sanitized) and returns to the textarea with the text intact", () => {
    render(<Harness initial={"**tučně** <script>window.__x=1</script>"} />);
    fireEvent.click(screen.getByRole("tab", { name: t.tabPreview }));
    expect(screen.queryByLabelText(t.answer)).toBeNull();
    expect(screen.queryByRole("toolbar")).toBeNull();
    const preview = document.querySelector(".markdown-preview") as HTMLElement;
    expect(preview.querySelector("strong")?.textContent).toBe("tučně");
    expect(preview.querySelector("script")).toBeNull();
    expect(screen.getByRole("tab", { name: t.tabPreview }).getAttribute("aria-selected")).toBe("true");

    fireEvent.click(screen.getByRole("tab", { name: t.tabText }));
    expect(answer().value).toBe("**tučně** <script>window.__x=1</script>");
  });
});

describe("answer editor – mini toolbar", () => {
  it("Tučně wraps the selection in ** and keeps it selected", () => {
    vi.useFakeTimers();
    try {
      render(<Harness initial="Zítra je volno" />);
      select(9, 14);
      fireEvent.click(screen.getByRole("button", { name: t.bold }));
      expect(answer().value).toBe("Zítra je **volno**");
      act(() => void vi.runAllTimers());
      expect(answer().value.slice(answer().selectionStart, answer().selectionEnd)).toBe("volno");
    } finally {
      vi.useRealTimers();
    }
  });

  it("Kurzíva wraps in single asterisks; an empty selection inserts a placeholder", () => {
    render(<Harness initial="" />);
    fireEvent.click(screen.getByRole("button", { name: t.italic }));
    expect(answer().value).toBe(`*${t.italicPlaceholder}*`);
  });

  it("Odrážky turns every selected line into a list item and keeps existing ones", () => {
    render(<Harness initial={"první\n- druhá\ntřetí"} />);
    select(0, 20);
    fireEvent.click(screen.getByRole("button", { name: t.bullets }));
    expect(answer().value).toBe("- první\n- druhá\n- třetí");
  });

  it("Odrážky on an empty field inserts one placeholder item", () => {
    render(<Harness initial="" />);
    fireEvent.click(screen.getByRole("button", { name: t.bullets }));
    expect(answer().value).toBe(`- ${t.bulletPlaceholder}`);
  });

  it("Odkaz makes [selection](https://) and selects the address part for typing", () => {
    vi.useFakeTimers();
    try {
      render(<Harness initial="Viz školní řád." />);
      select(4, 14);
      fireEvent.click(screen.getByRole("button", { name: t.link }));
      expect(answer().value).toBe("Viz [školní řád](https://).");
      act(() => void vi.runAllTimers());
      expect(answer().value.slice(answer().selectionStart, answer().selectionEnd)).toBe("https://");
    } finally {
      vi.useRealTimers();
    }
  });

  it("works on the pure function: edges and out-of-range selections", () => {
    expect(applyMarkdownFormat("abc", 99, 99, "bold", "x").value).toBe("abc**x**");
    expect(applyMarkdownFormat("a\n\nb", 2, 2, "bullets", "položka").value).toBe("a\n- položka\nb");
    expect(applyMarkdownFormat("", 0, 0, "link", "text")).toEqual({ value: "[text](https://)", selectionStart: 7, selectionEnd: 15 });
  });
});
