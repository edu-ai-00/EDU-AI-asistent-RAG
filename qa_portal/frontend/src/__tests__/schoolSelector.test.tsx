import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import App from "../App";
import { strings } from "../strings.cs";

const KEY = "edu-ai.qa-portal.school";
const KAVCI_Q = "Do kdy musím omluvit nepřítomnost dítěte ve škole?";
const JEDOVNICE_Q = "Do kolika hodin je otevřená družina?";

beforeEach(() => {
  window.history.replaceState(null, "", "/knowledge/");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("school selector in the header", () => {
  it("lives in the header next to the role badge, not in a row of its own", async () => {
    render(<App />);
    const select = (await screen.findByLabelText(strings.toolbar.school)) as HTMLSelectElement;
    const header = screen.getByRole("banner");
    expect(header.contains(select)).toBe(true);
    expect(within(header).getByText(strings.header.adminBadge)).toBeTruthy();
    expect(document.querySelector(".toolbar")).toBeNull();
  });

  it("remembers the chosen school across reloads", async () => {
    render(<App />);
    const select = (await screen.findByLabelText(strings.toolbar.school)) as HTMLSelectElement;
    await screen.findByText(KAVCI_Q);
    fireEvent.change(select, { target: { value: "ZS_JEDOVNICE" } });
    await screen.findByText(JEDOVNICE_Q);
    expect(window.localStorage.getItem(KEY)).toBe("ZS_JEDOVNICE");
    cleanup();

    render(<App />);
    expect(await screen.findByText(JEDOVNICE_Q)).toBeTruthy();
    expect((screen.getByLabelText(strings.toolbar.school) as HTMLSelectElement).value).toBe("ZS_JEDOVNICE");
  });

  it("falls back to the first school when the remembered one no longer exists", async () => {
    window.localStorage.setItem(KEY, "ZS_ZRUSENA");
    render(<App />);
    expect(await screen.findByText(KAVCI_Q)).toBeTruthy();
    expect((screen.getByLabelText(strings.toolbar.school) as HTMLSelectElement).value).toBe("ZS_KAVCI_HORY");
  });

  it("works when localStorage throws", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    render(<App />);
    expect(await screen.findByText(KAVCI_Q)).toBeTruthy();
    const select = screen.getByLabelText(strings.toolbar.school) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "ZS_JEDOVNICE" } });
    await waitFor(() => expect(screen.getByText(JEDOVNICE_Q)).toBeTruthy());
  });
});

describe("pack bar", () => {
  it("keeps the permanent pack delete out of the main actions, in the overflow menu", async () => {
    render(<App />);
    await screen.findByText(KAVCI_Q);
    const direct = [strings.toolbar.newPack, strings.toolbar.import, strings.toolbar.export];
    for (const name of direct) expect(screen.getByRole("button", { name })).toBeTruthy();
    expect(screen.queryByRole("button", { name: strings.deletePermanent.deletePackButton })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: strings.toolbar.moreActions }));
    const item = await screen.findByRole("menuitem", { name: strings.deletePermanent.deletePackButton });
    await waitFor(() => expect((item as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(item);
    expect(await screen.findByRole("dialog", { name: strings.deletePermanent.title })).toBeTruthy();
  });

  it("shows the pending-change count next to the sticky publish button", async () => {
    render(<App />);
    expect(await screen.findByText(strings.publish.pendingCount(0))).toBeTruthy();
    fireEvent.click(await screen.findByText(KAVCI_Q));
    const answer = (await screen.findByLabelText(strings.editor.answer)) as HTMLTextAreaElement;
    fireEvent.change(answer, { target: { value: "Změněná odpověď." } });
    expect(screen.getByText(strings.publish.pendingCount(1))).toBeTruthy();
    expect((screen.getByRole("button", { name: strings.actions.publish }) as HTMLButtonElement).disabled).toBe(false);
    expect(strings.publish.pendingCount(2)).toContain("změny");
    expect(strings.publish.pendingCount(5)).toContain("změn");
  });
});
