import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { strings } from "../strings.cs";

// ZS_JEDOVNICE has no pack at all, to reach "no pack selected"; everything else is the fixture mock.
vi.mock("../api/mockClient", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/mockClient")>();
  return {
    ...original,
    createMockClient: () => {
      const real = original.createMockClient();
      return {
        ...real,
        listPacks: async (school: string, signal?: AbortSignal) => (school === "ZS_JEDOVNICE" ? [] : real.listPacks(school, signal)),
      };
    },
  };
});

import App from "../App";

const t = strings.ask;
const d = strings.askDebug;
const KAVCI_Q = "Do kdy musím omluvit nepřítomnost dítěte ve škole?";

beforeEach(() => window.history.replaceState(null, "", "/knowledge/"));
afterEach(() => cleanup());

async function openAsk(question: string) {
  fireEvent.click(await screen.findByRole("button", { name: t.open }));
  const drawer = await screen.findByRole("dialog", { name: t.title });
  await within(drawer).findByRole("option", { name: "Rodič" });
  fireEvent.change(within(drawer).getByLabelText(t.question), { target: { value: question } });
  fireEvent.click(within(drawer).getByRole("button", { name: t.submit }));
  const panel = await within(drawer).findByRole("region", { name: d.heading });
  return { drawer, panel };
}

describe("the debug panel opens the rest of the portal", () => {
  it("'Otevřít v QA' switches to the QA page and selects the pack and the winning entry", async () => {
    window.history.replaceState(null, "", "/knowledge/documents");
    render(<App />);
    const { panel } = await openAsk("Kolik stojí družina?");
    fireEvent.click(await within(panel).findByRole("button", { name: d.qa.open }));

    await waitFor(() => expect(window.location.pathname).toBe("/knowledge/qa"));
    expect(screen.queryByRole("dialog", { name: t.title })).toBeNull();
    const question = (await screen.findByRole("textbox", { name: strings.editor.question })) as HTMLInputElement;
    expect(question.value).toBe(KAVCI_Q);
    expect(document.querySelector(".entry-list-item.is-selected")?.textContent).toContain(KAVCI_Q);
    expect((screen.getByLabelText(strings.toolbar.pack) as HTMLSelectElement).value).toBe("faq-provoz");
  });

  it("'Vytvořit QA otázku z tohoto dotazu' opens a new entry in the selected pack, prefilled with the question", async () => {
    window.history.replaceState(null, "", "/knowledge/documents");
    render(<App />);
    const { panel } = await openAsk("Kdy začíná školní rok?");
    fireEvent.click(within(panel).getByRole("button", { name: d.actions.createQa }));

    await waitFor(() => expect(window.location.pathname).toBe("/knowledge/qa"));
    expect(screen.queryByRole("dialog", { name: t.title })).toBeNull();
    const question = (await screen.findByRole("textbox", { name: strings.editor.question })) as HTMLInputElement;
    expect(question.value).toBe("Kdy začíná školní rok?");
    expect(screen.getByText(strings.editor.newEntryBadge)).toBeTruthy();
    const draft = document.querySelector(".entry-list-item.is-selected") as HTMLElement;
    expect(draft.textContent).toContain("Kdy začíná školní rok?");
    expect(draft.textContent).toContain(strings.list.newQuestionLabel);
  });

  it("asks to choose a pack when the school has none and leaves the drawer and the page as they are", async () => {
    render(<App />);
    await screen.findByText(KAVCI_Q);
    fireEvent.change(screen.getByLabelText(strings.toolbar.school), { target: { value: "ZS_JEDOVNICE" } });
    await waitFor(() => expect((screen.getByLabelText(strings.toolbar.school) as HTMLSelectElement).value).toBe("ZS_JEDOVNICE"));
    const { drawer, panel } = await openAsk("Kdy začíná školní rok?");
    fireEvent.click(within(panel).getByRole("button", { name: d.actions.createQa }));
    expect(within(drawer).getByText(d.actions.createQaNoPack)).toBeTruthy();
    expect(screen.getByRole("dialog", { name: t.title })).toBeTruthy();
    expect(document.querySelector(".entry-list-item")).toBeNull();
  });

  it("a document link opens the Documents page with that document's detail drawer", async () => {
    render(<App />);
    await screen.findByText(KAVCI_Q);
    const { panel } = await openAsk("Co říká školní řád?");
    const link = await within(panel).findByRole("button", { name: `${d.docs.open}: Školní řád` });
    fireEvent.click(link);

    await waitFor(() => expect(window.location.pathname).toBe("/knowledge/documents"));
    const detail = await screen.findByRole("dialog", { name: strings.documentDetail.title });
    expect(within(detail).getByText("https://www.zs-kavcihory.example/skolni-rad", { selector: ".drawer-title" })).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: t.title })).toBeNull();

    // The one-shot request is gone: leaving and coming back does not reopen it.
    fireEvent.click(within(detail).getByRole("button", { name: strings.documentDetail.close }));
    fireEvent.click(screen.getByRole("link", { name: strings.nav.qa }));
    fireEvent.click(await screen.findByRole("link", { name: strings.nav.documents }));
    await screen.findByRole("table");
    expect(screen.queryByRole("dialog", { name: strings.documentDetail.title })).toBeNull();
  });
});
