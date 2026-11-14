import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ApiClient } from "../api/client";
import type { School, SchoolPermissions } from "../api/types";
import { createMockClient } from "../api/mockClient";
import { DocumentsPage } from "../components/DocumentsPage";
import { strings } from "../strings.cs";

// Page size 2 makes the fixture (3 active docs) need "Načíst další".
vi.mock("../config", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../config")>()),
  DOCUMENTS_PAGE_SIZE: 2,
  DOCUMENTS_SEARCH_DEBOUNCE_MS: 5,
}));

afterEach(cleanup);

const t = strings.documents;
const ALL: SchoolPermissions = { read: true, publish: true, ingest: true, deactivate: true, delete_permanently: true };
const KAVCI: School = { code: "ZS_KAVCI_HORY", name: "ZŠ Kavčí hory", permissions: ALL };
const JEDOVNICE: School = { code: "ZS_JEDOVNICE", name: "ZŠ Jedovnice", permissions: ALL };
const WEB_RULES = "https://www.zs-kavcihory.example/skolni-rad";
const CANTEEN = "Provozni_rad_jidelny.pdf";
const MATH = "Matematika_pracovni_list_3.pdf";
const MENU = "Jidelnicek_zari.docx";

function row(title: string): HTMLElement {
  return screen.getByText(title).closest("tr") as HTMLElement;
}

/** Opens the row's "⋯" menu and returns the menu element. */
function openRowMenu(title: string): HTMLElement {
  fireEvent.click(within(row(title)).getByRole("button", { name: t.moreActionsFor(title) }));
  return screen.getByRole("menu");
}

describe("DocumentsPage — list", () => {
  it("renders the documents with labels, counts and state, and pages with next_cursor", async () => {
    render(<DocumentsPage client={createMockClient()} school={KAVCI} uiDefaultLang="cs" />);

    expect(await screen.findByText(WEB_RULES)).toBeTruthy();
    expect(screen.getByText(CANTEEN)).toBeTruthy();
    expect(screen.queryByText(MATH)).toBeNull();

    const rules = row(WEB_RULES);
    await waitFor(() => expect(within(rules).getByText("Školní řád")).toBeTruthy());
    expect(within(rules).getByText("14")).toBeTruthy(); // chunks of the shown version
    expect(within(rules).getByText("2")).toBeTruthy(); // versions
    expect(within(rules).getByText(t.stateActive)).toBeTruthy();
    expect(within(rules).getByText("Rodič, Žák")).toBeTruthy();
    expect(within(row(CANTEEN)).getByText(t.validityRange("2026-09-01", "2027-08-31"))).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: t.loadMore }));
    expect(await screen.findByText(MATH)).toBeTruthy();
    expect(screen.getByText(WEB_RULES)).toBeTruthy();
    expect(screen.queryByRole("button", { name: t.loadMore })).toBeNull();
  });

  it("filters by state and searches (debounced) by file name or URL", async () => {
    render(<DocumentsPage client={createMockClient()} school={KAVCI} uiDefaultLang="cs" />);
    await screen.findByText(WEB_RULES);
    expect(screen.queryByText(MENU)).toBeNull();

    fireEvent.change(screen.getByLabelText(t.filterLabel), { target: { value: "inactive" } });
    expect(await screen.findByText(MENU)).toBeTruthy();
    expect(screen.queryByText(WEB_RULES)).toBeNull();
    expect(within(row(MENU)).getByText(t.stateInactive)).toBeTruthy();

    fireEvent.change(screen.getByLabelText(t.filterLabel), { target: { value: "all" } });
    fireEvent.change(screen.getByLabelText(t.searchLabel), { target: { value: "jidel" } });
    await waitFor(() => expect(screen.queryByText(WEB_RULES)).toBeNull());
    expect(await screen.findByText(CANTEEN)).toBeTruthy();
    expect(screen.getByText(MENU)).toBeTruthy();

    fireEvent.change(screen.getByLabelText(t.searchLabel), { target: { value: "nic-takoveho" } });
    expect(await screen.findByText(t.empty)).toBeTruthy();
  });

  it("drops a late answer for the previous school after a switch", async () => {
    const real = createMockClient();
    const client: ApiClient = {
      ...real,
      listDocuments: async (school, params, signal) => {
        if (school === KAVCI.code) await new Promise((r) => setTimeout(r, 150));
        return real.listDocuments(school, params, signal);
      },
    };
    const view = render(<DocumentsPage client={client} school={KAVCI} uiDefaultLang="cs" />);
    view.rerender(<DocumentsPage client={client} school={JEDOVNICE} uiDefaultLang="cs" />);

    expect(await screen.findByText("https://www.zsjedovnice.example/druzina")).toBeTruthy();
    await new Promise((r) => setTimeout(r, 250));
    expect(screen.queryByText(WEB_RULES)).toBeNull();
    expect(screen.getByText("https://www.zsjedovnice.example/druzina")).toBeTruthy();
  });
});

describe("DocumentsPage — actions and permissions", () => {
  it("deactivates and re-activates a document, reloading the list each time", async () => {
    const real = createMockClient();
    const deactivate = vi.fn(real.deactivateDocument);
    const activate = vi.fn(real.activateDocument);
    const client: ApiClient = { ...real, deactivateDocument: deactivate, activateDocument: activate };
    render(<DocumentsPage client={client} school={KAVCI} uiDefaultLang="cs" />);
    await screen.findByText(CANTEEN);

    fireEvent.click(within(row(CANTEEN)).getByRole("button", { name: t.deactivate }));
    await waitFor(() => expect(screen.queryByText(CANTEEN)).toBeNull());
    expect(deactivate).toHaveBeenCalledWith(KAVCI.code, "1a2b3c4d-1111-4abc-8def-000000000201");

    fireEvent.change(screen.getByLabelText(t.filterLabel), { target: { value: "inactive" } });
    expect(await screen.findByText(CANTEEN)).toBeTruthy();
    fireEvent.click(within(row(CANTEEN)).getByRole("button", { name: t.activate }));
    await waitFor(() => expect(screen.queryByText(CANTEEN)).toBeNull());
    expect(activate).toHaveBeenCalledTimes(1);
  });

  it("shows every action when all is allowed", async () => {
    render(<DocumentsPage client={createMockClient()} school={KAVCI} uiDefaultLang="cs" />);
    await screen.findByText(WEB_RULES);
    expect(within(row(WEB_RULES)).getByRole("button", { name: t.deactivate })).toBeTruthy();
    const menu = openRowMenu(WEB_RULES);
    for (const name of [t.detail, t.newVersion, t.editValidity, t.delete]) {
      expect(within(menu).getByRole("menuitem", { name })).toBeTruthy();
    }
    await waitFor(() =>
      expect((screen.getByRole("button", { name: t.addDocuments }) as HTMLButtonElement).disabled).toBe(false),
    );
  });

  it("treats a school without `permissions` as all allowed", async () => {
    render(<DocumentsPage client={createMockClient()} school={{ code: KAVCI.code, name: KAVCI.name }} uiDefaultLang="cs" />);
    await screen.findByText(WEB_RULES);
    expect(within(openRowMenu(WEB_RULES)).getByRole("menuitem", { name: t.delete })).toBeTruthy();
    expect(screen.getByRole("button", { name: t.addDocuments })).toBeTruthy();
  });

  it("hides what the policy does not allow", async () => {
    const readOnly: School = {
      ...KAVCI,
      permissions: { read: true, publish: false, ingest: false, deactivate: false, delete_permanently: false },
    };
    render(<DocumentsPage client={createMockClient()} school={readOnly} uiDefaultLang="cs" />);
    await screen.findByText(WEB_RULES);
    expect(screen.queryByRole("button", { name: t.deactivate })).toBeNull();
    expect(screen.queryByRole("button", { name: t.addDocuments })).toBeNull();
    // Only the read-only "Detail" and "Stáhnout" (read permission) are left in the menu.
    const menu = openRowMenu(WEB_RULES);
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual([t.detail, t.download]);
  });

  it("shows deactivate but not delete when only deactivation is allowed", async () => {
    const school: School = { ...KAVCI, permissions: { ...ALL, delete_permanently: false } };
    render(<DocumentsPage client={createMockClient()} school={school} uiDefaultLang="cs" />);
    await screen.findByText(WEB_RULES);
    expect(within(row(WEB_RULES)).getByRole("button", { name: t.deactivate })).toBeTruthy();
    const menu = openRowMenu(WEB_RULES);
    expect(within(menu).queryByRole("menuitem", { name: t.delete })).toBeNull();
    expect(within(menu).getByRole("menuitem", { name: t.editValidity })).toBeTruthy();
  });

  it("loads nothing without read permission", async () => {
    const real = createMockClient();
    const listDocuments = vi.fn(real.listDocuments);
    render(
      <DocumentsPage
        client={{ ...real, listDocuments }}
        school={{ ...KAVCI, permissions: { ...ALL, read: false } }}
        uiDefaultLang="cs"
      />,
    );
    expect(screen.getByText(t.noReadPermission)).toBeTruthy();
    await new Promise((r) => setTimeout(r, 50));
    expect(listDocuments).not.toHaveBeenCalled();
  });

  it("deletes a document through the delete dialog and reloads the list", async () => {
    render(<DocumentsPage client={createMockClient()} school={KAVCI} uiDefaultLang="cs" />);
    await screen.findByText(CANTEEN);
    fireEvent.click(within(openRowMenu(CANTEEN)).getByRole("menuitem", { name: t.delete }));

    const dialog = await screen.findByRole("dialog", { name: strings.documentDelete.title });
    await within(dialog).findByText(strings.documentDelete.scopeTemplate(1, 32));
    fireEvent.click(within(dialog).getByRole("button", { name: strings.documentDelete.confirm }));
    expect(await within(dialog).findByText(strings.documentDelete.doneHeading)).toBeTruthy();
    await waitFor(() => expect(screen.queryByText(CANTEEN, { selector: "td" })).toBeNull());
  });
});
