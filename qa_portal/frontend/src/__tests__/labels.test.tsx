import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Domain, Entry, EntryPage } from "../api/types";
import { strings } from "../strings.cs";
import { EntryList } from "../components/EntryList";

// Records every entries request App makes, by wrapping the factory App itself
// calls (it builds its own mock client, so a spy on an instance is too late).
const asked: Array<[string, string]> = [];
vi.mock("../api/mockClient", async () => {
  const actual = await vi.importActual<typeof import("../api/mockClient")>("../api/mockClient");
  return {
    ...actual,
    createMockClient: () => {
      const c = actual.createMockClient();
      const listEntries = c.listEntries.bind(c);
      c.listEntries = (school, pack, params, signal) => {
        asked.push([school, pack]);
        return listEntries(school, pack, params, signal);
      };
      return c;
    },
  };
});

afterEach(() => {
  cleanup();
});

const DOMAINS: Domain[] = [
  { code: "ATTENDANCE", label: "Docházka", label_fallback: false },
  { code: "BULLYING", label: "Šikana", label_fallback: false },
  // A domain whose Czech translation is genuinely missing: the API already
  // told us so, the list still has to show something usable.
  { code: "SAFETY", label: "SAFETY", label_fallback: true },
] as unknown as Domain[];

function page(entries: Array<Partial<Entry>>): EntryPage {
  return {
    entries: entries.map((e, i) => ({
      entry_key: e.entry_key ?? `k${i}`,
      canonical_q: e.canonical_q ?? `Otázka ${i}?`,
      answer_md: "",
      variants: [],
      sources: [],
      domains: e.domains ?? [],
      domain_ids: null,
      category_code: null,
      category_label: null,
      category_label_fallback: false,
      category_is_active: null,
      category_id: null,
      cat_legacy: null,
      owner_user_id: null,
      visibility: "entity",
      audience_roles: ["TEACHER"],
      school_grade_ids: null,
      school_subject_ids: null,
      school_level: null,
      valid_from: null,
      valid_to: null,
      is_active: true,
      entry_id: `id-${i}`,
    })) as unknown as Entry[],
    next_cursor: null,
    pack_etag: "sha256:x",
    pack_version: 1,
    metadata_uniform: true,
  } as unknown as EntryPage;
}

function renderList(entries: Array<Partial<Entry>>, domains: Domain[] = DOMAINS) {
  render(
    <EntryList
      state={{ status: "ready", value: page(entries) }}
      query=""
      onQueryChange={() => {}}
      selectedEntryKey={null}
      onSelect={() => {}}
      pendingNewEntries={[]}
      onAddQuestion={() => {}}
      domains={domains}
    />,
  );
}

describe("entry list — areas are shown by their Czech name", () => {
  it("renders the catalog label, not the stable code", () => {
    renderList([{ domains: ["ATTENDANCE", "BULLYING"] }]);
    expect(screen.getByText(/Docházka, Šikana/)).toBeTruthy();
    expect(screen.queryByText(/ATTENDANCE/)).toBeNull();
    expect(screen.queryByText(/BULLYING/)).toBeNull();
  });

  it("keeps showing the code when the catalog does not know it", () => {
    // A stale or narrower catalog must never hide what an entry is tagged with.
    renderList([{ domains: ["ATTENDANCE", "MYSTERY"] }]);
    expect(screen.getByText(/Docházka, MYSTERY/)).toBeTruthy();
  });

  it("falls back to the code the API supplied for a missing translation", () => {
    renderList([{ domains: ["SAFETY"] }]);
    expect(screen.getByText(/SAFETY/)).toBeTruthy();
  });

  it("says so plainly when an entry has no area at all", () => {
    renderList([{ domains: [] }]);
    expect(screen.getByText(strings.list.noAreas)).toBeTruthy();
  });

  it("renders nothing about areas from an empty catalog beyond the codes", () => {
    renderList([{ domains: ["ATTENDANCE"] }], []);
    expect(screen.getByText(/ATTENDANCE/)).toBeTruthy();
  });
});

describe("missing-translation hint", () => {
  it("names the language that is missing instead of assuming Czech", () => {
    expect(strings.editor.missingTranslationHint("cs")).toContain("cs");
    expect(strings.editor.missingTranslationHint("en")).toContain("en");
    expect(strings.editor.missingTranslationHint("en")).not.toContain("česk");
  });
});


describe("switching school never asks for the previous school's pack", () => {
  it("does not fetch entries for a pack the new school does not own", async () => {
    // Regression: the pack list of the school we just left was still "ready",
    // so its first pack got auto-selected under the NEW school and the client
    // asked for .../schools/<new>/packs/<foreign pack>/entries -> 404.
    asked.length = 0;
    const { createMockClient } = await import("../api/mockClient");
    const { default: App } = await import("../App");
    render(<App />);

    const schoolSelect = await screen.findByLabelText(strings.toolbar.school);
    await waitFor(() => expect(asked.length).toBeGreaterThan(0));
    fireEvent.change(schoolSelect, { target: { value: "ZS_JEDOVNICE" } });
    await waitFor(() => expect(asked.some(([s]) => s === "ZS_JEDOVNICE")).toBe(true));

    const reference = createMockClient();
    const seen = new Map<string, Set<string>>();
    for (const [school, pack] of asked) {
      if (!seen.has(school)) seen.set(school, new Set());
      seen.get(school)!.add(pack);
    }
    for (const [school, packs] of seen) {
      const owned = new Set((await reference.listPacks(school)).map((p) => p.pack_id));
      for (const pack of packs) {
        expect(owned.has(pack), `${school} was asked for foreign pack ${pack}`).toBe(true);
      }
    }
  });
});
