import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import type { ApiClient } from "../api/client";
import type { Entry, EntryPage } from "../api/types";
import { EntryList } from "../components/EntryList";
import { useEntries } from "../hooks/useEntries";
import { filterEntries, pagerItems } from "../lib/entryList";
import { strings } from "../strings.cs";

const t = strings.list;

afterEach(() => cleanup());

function entry(i: number, over: Partial<Entry> = {}): Entry {
  return {
    entry_key: `k${i}`,
    canonical_q: `Otázka číslo ${i}`,
    answer_md: `Odpověď ${i}`,
    domains: i % 2 === 0 ? ["ATTENDANCE"] : [],
    is_active: true,
    ...over,
  };
}

function pageOf(entries: Entry[]): EntryPage {
  return { entries, next_cursor: null, total: entries.length, pack_etag: "e", pack_version: 1 };
}

const many = (n: number) => Array.from({ length: n }, (_, i) => entry(i + 1));

function Harness(props: {
  entries: Entry[];
  selected?: string | null;
  pending?: Array<{ key: string; canonical_q: string }>;
  onSelect?: (key: string) => void;
}) {
  const [query, setQuery] = useState("");
  return (
    <EntryList
      state={{ status: "ready", value: pageOf(props.entries) }}
      query={query}
      onQueryChange={setQuery}
      selectedEntryKey={props.selected ?? null}
      onSelect={props.onSelect ?? (() => {})}
      pendingNewEntries={props.pending ?? []}
      domains={[{ code: "ATTENDANCE", label: "Docházka" }]}
    />
  );
}

describe("EntryList – scroll container", () => {
  it("keeps search, count, add and filter in a fixed head and puts ONLY the list in the scroll area", () => {
    render(<Harness entries={many(5)} />);
    const top = document.querySelector(".entry-list-top") as HTMLElement;
    const scroll = document.querySelector(".entry-list-scroll") as HTMLElement;
    expect(within(top).getByLabelText(t.searchLabel)).toBeTruthy();
    // "+ Přidat otázku" is the page's primary action in the QA toolbar now.
    expect(within(top).queryByRole("button", { name: t.addQuestion })).toBeNull();
    expect(within(top).getByLabelText(t.onlyNoAreas)).toBeTruthy();
    expect(within(top).getByText(t.countAll(5))).toBeTruthy();
    expect(top.contains(scroll)).toBe(false);
    expect(within(scroll).getAllByRole("button")).toHaveLength(5);
    // The pager sits below the scroll area, inside the panel.
    const pager = screen.getByRole("navigation", { name: t.pagerLabel });
    expect(scroll.contains(pager)).toBe(false);
    expect(pager.closest(".entry-list")).toBe(scroll.closest(".entry-list"));
  });
});

describe("EntryList – pagination", () => {
  it("shows 50 entries per page with a range, numbered pages and prev/next", () => {
    render(<Harness entries={many(120)} />);
    expect(document.querySelectorAll(".entry-list-item")).toHaveLength(50);
    expect(screen.getByText(t.pagerRange(1, 50, 120))).toBeTruthy();
    expect(screen.getByText("Otázka číslo 1")).toBeTruthy();
    expect((screen.getByRole("button", { name: t.pagePrev }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("button", { name: t.pageGo(1) }).getAttribute("aria-current")).toBe("page");

    fireEvent.click(screen.getByRole("button", { name: t.pageNext }));
    expect(screen.getByText(t.pagerRange(51, 100, 120))).toBeTruthy();
    expect(screen.getByText("Otázka číslo 51")).toBeTruthy();
    expect(screen.queryByText("Otázka číslo 1")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: t.pageGo(3) }));
    expect(screen.getByText(t.pagerRange(101, 120, 120))).toBeTruthy();
    expect(document.querySelectorAll(".entry-list-item")).toHaveLength(20);
    expect((screen.getByRole("button", { name: t.pageNext }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("has no page buttons for a single page but still tells the range", () => {
    render(<Harness entries={many(7)} />);
    expect(screen.getByText(t.pagerRange(1, 7, 7))).toBeTruthy();
    expect(screen.queryByRole("button", { name: t.pageNext })).toBeNull();
  });

  it("builds the page window with gaps: 1 2 3 … 5", () => {
    expect(pagerItems(1, 5)).toEqual([1, 2, 3, 4, 5]);
    expect(pagerItems(1, 12)).toEqual([1, 2, "gap", 12]);
    expect(pagerItems(6, 12)).toEqual([1, "gap", 5, 6, 7, "gap", 12]);
    expect(pagerItems(12, 12)).toEqual([1, "gap", 11, 12]);
  });

  it("searches the WHOLE pack, resets to page 1 and shows the found range", () => {
    render(<Harness entries={many(120)} />);
    fireEvent.click(screen.getByRole("button", { name: t.pageGo(3) }));
    expect(screen.getByText(t.pagerRange(101, 120, 120))).toBeTruthy();

    fireEvent.change(screen.getByLabelText(t.searchLabel), { target: { value: "číslo 117" } });
    expect(screen.getByText("Otázka číslo 117")).toBeTruthy();
    expect(screen.getByText(t.pagerRange(1, 1, 1))).toBeTruthy();
    expect(screen.getByText(t.countFiltered(1, 120))).toBeTruthy();

    fireEvent.change(screen.getByLabelText(t.searchLabel), { target: { value: "žádná taková věc" } });
    expect(screen.getByText(t.noMatch)).toBeTruthy();
    expect(screen.queryByRole("navigation", { name: t.pagerLabel })).toBeNull();
  });

  it("also searches the answer and the variants, ignoring case", () => {
    const list = [entry(1, { answer_md: "Platí **Školní řád**" }), entry(2, { variants: ["Jak se omluvit?"] }), entry(3)];
    expect(filterEntries(list, "školní ŘÁD", false).map((e) => e.entry_key)).toEqual(["k1"]);
    expect(filterEntries(list, "OMLUVIT", false).map((e) => e.entry_key)).toEqual(["k2"]);
  });
});

describe("EntryList – only entries without an area", () => {
  it("hides the 'Bez oblasti' chip on cards and filters through a toggle that resets the page", () => {
    render(<Harness entries={many(120)} />);
    expect(screen.queryByText("Bez oblasti")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: t.pageGo(2) }));
    const toggle = screen.getByLabelText(t.onlyNoAreas) as HTMLInputElement;
    fireEvent.click(toggle);
    expect(toggle.checked).toBe(true);

    // 60 of 120 have no area (odd numbers): one page of 50, back on page 1.
    expect(screen.getByText(t.pagerRange(1, 50, 60))).toBeTruthy();
    expect(document.querySelectorAll(".entry-list-item .chip")).toHaveLength(0);
    expect(screen.getByText("Otázka číslo 1")).toBeTruthy();
    expect(screen.queryByText("Otázka číslo 2")).toBeNull();
    expect(screen.getByText(t.countFiltered(60, 120))).toBeTruthy();

    fireEvent.click(toggle);
    expect(screen.getByText(t.pagerRange(1, 50, 120))).toBeTruthy();
  });
});

describe("EntryList – drafts and selection", () => {
  it("keeps unpublished drafts listed whatever the filter or page says", () => {
    render(<Harness entries={many(120)} pending={[{ key: "__new__:1", canonical_q: "Moje nová otázka" }]} />);
    fireEvent.click(screen.getByRole("button", { name: t.pageGo(3) }));
    expect(screen.getByText("Moje nová otázka")).toBeTruthy();
    fireEvent.change(screen.getByLabelText(t.searchLabel), { target: { value: "nic takového" } });
    expect(screen.getByText("Moje nová otázka")).toBeTruthy();
    expect(screen.getByText(t.noMatch)).toBeTruthy();
  });

  describe("scrolling the selection into view", () => {
    const scrollIntoView = vi.fn();
    beforeEach(() => {
      scrollIntoView.mockClear();
      Element.prototype.scrollIntoView = scrollIntoView;
    });
    afterEach(() => {
      // jsdom has none: leave it as found.
      delete (Element.prototype as unknown as { scrollIntoView?: unknown }).scrollIntoView;
    });

    it("jumps to the page of the selected entry and scrolls it into view", () => {
      render(<Harness entries={many(120)} selected="k117" />);
      expect(screen.getByText(t.pagerRange(101, 120, 120))).toBeTruthy();
      const selected = document.querySelector(".entry-list-item.is-selected") as HTMLElement;
      expect(selected.textContent).toContain("Otázka číslo 117");
      expect(scrollIntoView).toHaveBeenCalled();
      expect(scrollIntoView.mock.instances.some((el) => el === selected)).toBe(true);
    });

    it("does not pull the pager back when the admin pages away from the selection", () => {
      render(<Harness entries={many(120)} selected="k3" />);
      fireEvent.click(screen.getByRole("button", { name: t.pageGo(2) }));
      expect(screen.getByText(t.pagerRange(51, 100, 120))).toBeTruthy();
    });
  });
});

describe("useEntries – the whole pack", () => {
  const OLD_PAGE = (n: number, from: number, next: string | null): EntryPage => ({
    entries: Array.from({ length: n }, (_, i) => entry(from + i)),
    next_cursor: next,
    total: 450,
    pack_etag: next ? "first" : "last",
    pack_version: 3,
  });

  it("follows next_cursor until the pack is complete and reports what it really holds", async () => {
    const listEntries = vi.fn(async (_s: string, _p: string, params?: { limit?: number; cursor?: string }) => {
      if (!params?.cursor) return OLD_PAGE(200, 0, "200");
      if (params.cursor === "200") return OLD_PAGE(200, 200, "400");
      return OLD_PAGE(50, 400, null);
    });
    const client = { listEntries } as unknown as ApiClient;
    const { result } = renderHook(() => useEntries(client, "ZS", "pack"));
    await waitFor(() => expect(result.current.state.status).toBe("ready"));
    const state = result.current.state;
    if (state.status !== "ready") throw new Error("not ready");
    expect(state.value.entries).toHaveLength(450);
    expect(state.value.total).toBe(450);
    expect(state.value.next_cursor).toBeNull();
    expect(state.value.pack_etag).toBe("first");
    expect(listEntries.mock.calls.map((c) => c[2])).toEqual([{ limit: 200 }, { limit: 200, cursor: "200" }, { limit: 200, cursor: "400" }]);
    // The search text is never sent: the server would only filter inside one page.
    expect(listEntries.mock.calls.every((c) => !(c[2] && "q" in c[2]))).toBe(true);
  });
});
