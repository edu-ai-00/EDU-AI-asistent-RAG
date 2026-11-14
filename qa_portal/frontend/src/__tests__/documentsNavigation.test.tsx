import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import App from "../App";
import { strings } from "../strings.cs";

const QUESTION = "Do kdy musím omluvit nepřítomnost dítěte ve škole?";
const WEB_RULES = "https://www.zs-kavcihory.example/skolni-rad";

beforeEach(() => {
  window.history.replaceState(null, "", "/knowledge/");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function link(name: string) {
  return screen.getByRole("link", { name });
}

/** Simulates the browser's back/forward: the URL changes, then popstate fires. */
function popTo(url: string) {
  window.history.pushState(null, "", url);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

describe("page switch QA balíčky | Dokumenty (real paths)", () => {
  it("switches pages with pushState paths, sharing the school selector, and follows the back button", async () => {
    render(<App />);
    expect(await screen.findByText(QUESTION)).toBeTruthy();
    expect(link(strings.nav.qa).getAttribute("aria-current")).toBe("page");
    expect(link(strings.nav.documents).getAttribute("href")).toBe("/knowledge/documents");

    fireEvent.click(link(strings.nav.documents));
    expect(window.location.pathname).toBe("/knowledge/documents");
    expect(window.location.hash).toBe("");
    expect(await screen.findByText(WEB_RULES)).toBeTruthy();
    expect(screen.queryByText(QUESTION)).toBeNull();
    expect(screen.queryByLabelText(strings.toolbar.pack)).toBeNull();
    expect(link(strings.nav.documents).getAttribute("aria-current")).toBe("page");

    // The school selector is shared and drives the documents page too.
    fireEvent.change(screen.getByLabelText(strings.toolbar.school), { target: { value: "ZS_JEDOVNICE" } });
    expect(await screen.findByText("https://www.zsjedovnice.example/druzina")).toBeTruthy();
    fireEvent.change(screen.getByLabelText(strings.toolbar.school), { target: { value: "ZS_KAVCI_HORY" } });
    expect(await screen.findByText(WEB_RULES)).toBeTruthy();

    // The real browser back button.
    window.history.back();
    expect(await screen.findByText(QUESTION)).toBeTruthy();
    expect(window.location.pathname).toBe("/knowledge/");
    expect(screen.getByLabelText(strings.toolbar.pack)).toBeTruthy();

    window.history.forward();
    expect(await screen.findByText(WEB_RULES)).toBeTruthy();
    expect(window.location.pathname).toBe("/knowledge/documents");
  });

  it("pushes /knowledge/qa when going back to the QA page by the link", async () => {
    window.history.replaceState(null, "", "/knowledge/documents");
    render(<App />);
    expect(await screen.findByText(WEB_RULES)).toBeTruthy();
    fireEvent.click(link(strings.nav.qa));
    expect(window.location.pathname).toBe("/knowledge/qa");
    expect(await screen.findByText(QUESTION)).toBeTruthy();
  });

  it("opens the right page directly from its address", async () => {
    window.history.replaceState(null, "", "/knowledge/documents");
    render(<App />);
    expect(await screen.findByText(WEB_RULES)).toBeTruthy();
    expect(screen.queryByLabelText(strings.toolbar.pack)).toBeNull();
    cleanup();

    window.history.replaceState(null, "", "/knowledge/qa");
    render(<App />);
    expect(await screen.findByText(QUESTION)).toBeTruthy();
    cleanup();

    // An unknown path under the base is the QA page.
    window.history.replaceState(null, "", "/knowledge/neco-jineho");
    render(<App />);
    expect(await screen.findByText(QUESTION)).toBeTruthy();
  });

  it("maps a legacy #/documents hash to /knowledge/documents", async () => {
    window.history.replaceState(null, "", "/knowledge/#/documents");
    render(<App />);
    expect(await screen.findByText(WEB_RULES)).toBeTruthy();
    expect(window.location.pathname).toBe("/knowledge/documents");
    expect(window.location.hash).toBe("");
  });

  it("maps a legacy hash typed while the app is open", async () => {
    render(<App />);
    expect(await screen.findByText(QUESTION)).toBeTruthy();
    window.history.pushState(null, "", "/knowledge/#/documents");
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    expect(await screen.findByText(WEB_RULES)).toBeTruthy();
    expect(window.location.pathname).toBe("/knowledge/documents");
    expect(window.location.hash).toBe("");
  });

  it("guards unsaved QA drafts when leaving the QA page", async () => {
    window.history.replaceState(null, "", "/knowledge/qa");
    render(<App />);
    fireEvent.click(await screen.findByText(QUESTION));
    const answer = (await screen.findByLabelText(strings.editor.answer)) as HTMLTextAreaElement;
    fireEvent.change(answer, { target: { value: "Rozepsaná odpověď." } });

    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    fireEvent.click(link(strings.nav.documents));
    expect(confirm).toHaveBeenCalledWith(strings.switchGuard.confirmMessage);
    expect(window.location.pathname).toBe("/knowledge/qa");
    expect((screen.getByLabelText(strings.editor.answer) as HTMLTextAreaElement).value).toBe("Rozepsaná odpověď.");

    // Back/forward is guarded the same way and the QA address comes back.
    confirm.mockReturnValueOnce(false);
    popTo("/knowledge/documents");
    expect(confirm).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(window.location.pathname).toBe("/knowledge/qa"));
    expect((screen.getByLabelText(strings.editor.answer) as HTMLTextAreaElement).value).toBe("Rozepsaná odpověď.");

    confirm.mockReturnValueOnce(true);
    fireEvent.click(link(strings.nav.documents));
    expect(await screen.findByText(WEB_RULES)).toBeTruthy();
    expect(window.location.pathname).toBe("/knowledge/documents");

    // Back on QA the page works as before; the draft was dropped as the guard said.
    fireEvent.click(link(strings.nav.qa));
    fireEvent.click(await screen.findByText(QUESTION));
    const reopened = (await screen.findByLabelText(strings.editor.answer)) as HTMLTextAreaElement;
    expect(reopened.value).not.toBe("Rozepsaná odpověď.");
    expect(confirm).toHaveBeenCalledTimes(3);
  });

  it("shows the new title in the header and the contract line in the footer", async () => {
    render(<App />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Správa znalostní databáze");
    const footer = await waitFor(() => {
      const el = document.querySelector("footer.app-footer") as HTMLElement;
      expect(el.textContent).toMatch(/^Kontrakt .+ · režim vpn_admin$/);
      return el;
    });
    expect(footer.closest("main")).toBeNull();
    expect(document.querySelector("main")?.textContent).not.toMatch(/Kontrakt/);
  });
});
