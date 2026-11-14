import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { strings } from "../strings.cs";

// ZS_JEDOVNICE loses the publish permission; everything else is the fixture mock.
vi.mock("../api/mockClient", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/mockClient")>();
  return {
    ...original,
    createMockClient: () => {
      const real = original.createMockClient();
      return {
        ...real,
        listSchools: async (signal?: AbortSignal) =>
          (await real.listSchools(signal)).map((school) =>
            school.code === "ZS_JEDOVNICE" && school.permissions
              ? { ...school, permissions: { ...school.permissions, publish: false } }
              : school,
          ),
      };
    },
  };
});

import App from "../App";

const t = strings.ask;
const KAVCI_Q = "Do kdy musím omluvit nepřítomnost dítěte ve škole?";

beforeEach(() => {
  window.history.replaceState(null, "", "/knowledge/");
});

afterEach(() => cleanup());

const openButton = () => screen.queryByRole("button", { name: t.open });

describe("try a question in the app", () => {
  it("sits in the header next to the school selector, on both pages", async () => {
    render(<App />);
    await screen.findByText(KAVCI_Q);
    const header = screen.getByRole("banner");
    const button = await within(header).findByRole("button", { name: t.open });
    expect(button.className).toContain("btn--secondary");
    expect(button.parentElement).toBe(screen.getByLabelText(strings.toolbar.school).closest(".app-header-tools"));

    fireEvent.click(screen.getByRole("link", { name: strings.nav.documents }));
    expect(await screen.findByRole("button", { name: t.open })).toBeTruthy();
  });

  it("opens the drawer with focus in the question, answers, and Escape returns focus to the button", async () => {
    render(<App />);
    const button = await screen.findByRole("button", { name: t.open });
    expect(button.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(button);

    const drawer = await screen.findByRole("dialog", { name: t.title });
    expect(button.getAttribute("aria-expanded")).toBe("true");
    const question = within(drawer).getByLabelText(t.question) as HTMLTextAreaElement;
    expect(document.activeElement).toBe(question);

    await within(drawer).findByRole("option", { name: "Rodič" });
    fireEvent.change(question, { target: { value: "Kdy začíná školní rok?" } });
    fireEvent.keyDown(question, { key: "Enter", ctrlKey: true });
    expect(await within(drawer).findByText(t.chipOutcome("odpověď"))).toBeTruthy();
    expect(within(drawer).getByText(t.chipSource("QA, přesná shoda"))).toBeTruthy();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: t.title })).toBeNull();
    expect(document.activeElement).toBe(button);
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });

  it("answers with sources from documents and with no text in retrieval-only mode (mock)", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: t.open }));
    const drawer = await screen.findByRole("dialog", { name: t.title });
    await within(drawer).findByRole("option", { name: "Rodič" });
    fireEvent.change(within(drawer).getByLabelText(t.question), { target: { value: "Co říká školní řád?" } });
    fireEvent.click(within(drawer).getByLabelText(t.retrievalOnly));
    fireEvent.click(within(drawer).getByRole("button", { name: t.submit }));
    expect(await within(drawer).findByText(t.answerSkipped)).toBeTruthy();
    expect(within(drawer).getByRole("link", { name: "Školní řád" })).toBeTruthy();
  });

  it("hides the button, and closes the drawer, for a school without the publish permission", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: t.open }));
    await screen.findByRole("dialog", { name: t.title });

    fireEvent.change(screen.getByLabelText(strings.toolbar.school), { target: { value: "ZS_JEDOVNICE" } });
    await waitFor(() => expect(openButton()).toBeNull());
    expect(screen.queryByRole("dialog", { name: t.title })).toBeNull();

    fireEvent.change(screen.getByLabelText(strings.toolbar.school), { target: { value: "ZS_KAVCI_HORY" } });
    expect(await screen.findByRole("button", { name: t.open })).toBeTruthy();
    // It does not pop open by itself when permission comes back.
    expect(screen.queryByRole("dialog", { name: t.title })).toBeNull();
  });

  it("closes the document detail drawer when the ask drawer opens, and the other way round", async () => {
    window.history.replaceState(null, "", "/knowledge/documents");
    render(<App />);
    const rules = "https://www.zs-kavcihory.example/skolni-rad";
    const detailName = strings.documentDetail.title;
    const openDetail = async () => {
      const row = (await screen.findByText(rules)).closest("tr") as HTMLElement;
      fireEvent.click(within(row).getByRole("button", { name: strings.documents.moreActionsFor(rules) }));
      fireEvent.click(screen.getByRole("menuitem", { name: strings.documents.detail }));
      return screen.findByRole("dialog", { name: detailName });
    };

    await openDetail();
    fireEvent.click(await screen.findByRole("button", { name: t.open }));
    const ask = await screen.findByRole("dialog", { name: t.title });
    expect(screen.queryByRole("dialog", { name: detailName })).toBeNull();
    expect(document.activeElement).toBe(within(ask).getByLabelText(t.question));

    await openDetail();
    expect(screen.queryByRole("dialog", { name: t.title })).toBeNull();
    expect(screen.getAllByRole("dialog")).toHaveLength(1);

    // Reopening the ask drawer must not bring the closed detail back afterwards.
    fireEvent.click(screen.getByRole("button", { name: t.open }));
    await screen.findByRole("dialog", { name: t.title });
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
