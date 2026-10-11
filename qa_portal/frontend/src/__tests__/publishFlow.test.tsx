import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ApiClient } from "../api/client";
import { ApiError, type EntryWrite, type Operation } from "../api/types";
import { strings } from "../strings.cs";

const KAVCI_HORY_QUESTION = "Do kdy musím omluvit nepřítomnost dítěte ve škole?";
const SECOND_QUESTION = "Kolik stojí školní družina a jak se platí?";

type PreviewCall = Parameters<ApiClient["previewPackChanges"]>;
type PublishCall = Parameters<ApiClient["publishPackChanges"]>;

let previewCalls: PreviewCall[] = [];
let publishCalls: PublishCall[] = [];
let listEntriesCallCount = 0;
let overrides: {
  publishPackChanges?: ApiClient["publishPackChanges"];
  getOperation?: ApiClient["getOperation"];
} = {};

// Production polls every 1.5 s for up to three minutes (config.ts). The test
// only shortens the clock here; it must never be the reason the shipped build
// hammers GET /operations/{id}.
vi.mock("../config", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../config")>()),
  PUBLISH_POLL_INTERVAL_MS: 10,
  PUBLISH_POLL_MAX_ATTEMPTS: 40,
}));

// Wraps the REAL mock client (the fixture-backed one every other test uses)
// so preview/publish calls are observable and, per test, individually
// overridable — without touching mockClient.ts itself. Only App.tsx's own
// `createMockClient()` call goes through this wrapper.
vi.mock("../api/mockClient", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/mockClient")>();
  return {
    ...actual,
    createMockClient: (): ApiClient => {
      const real = actual.createMockClient();
      return {
        ...real,
        listEntries: (...args) => {
          listEntriesCallCount += 1;
          return real.listEntries(...args);
        },
        previewPackChanges: (...args) => {
          previewCalls.push(args);
          return real.previewPackChanges(...args);
        },
        publishPackChanges: (...args) => {
          publishCalls.push(args);
          return (overrides.publishPackChanges ?? real.publishPackChanges)(...args);
        },
        getOperation: (...args) => (overrides.getOperation ?? real.getOperation)(...args),
      };
    },
  };
});

beforeEach(() => {
  previewCalls = [];
  publishCalls = [];
  listEntriesCallCount = 0;
  overrides = {};
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

async function selectAndEdit(question: string, nextAnswer: string) {
  fireEvent.click(await screen.findByText(question));
  const answer = (await screen.findByLabelText(strings.editor.answer)) as HTMLTextAreaElement;
  fireEvent.change(answer, { target: { value: nextAnswer } });
  return answer;
}

async function openPreview() {
  const publishButton = screen.getByRole("button", { name: strings.actions.publish });
  fireEvent.click(publishButton);
  await screen.findByRole("button", { name: strings.actions.confirm });
}

describe("publish a pack change (success)", () => {
  it("previews, confirms, polls to completed, reloads the list once, and sends the original entry_key", async () => {
    const { default: App } = await import("../App");
    render(<App />);

    await selectAndEdit(KAVCI_HORY_QUESTION, "Nejpozději do 3 pracovních dnů od začátku nepřítomnosti.");
    const listEntriesBefore = listEntriesCallCount;

    await openPreview();
    expect(previewCalls).toHaveLength(1);
    const previewBody = previewCalls[0][2];
    expect(previewBody.entries).toHaveLength(1);
    const write: EntryWrite = previewBody.entries[0];
    expect(write.entry_key).toBe("omluva-lhuta");
    expect(write.answer_md).toBe("Nejpozději do 3 pracovních dnů od začátku nepřítomnosti.");
    expect(Object.keys(write).sort()).toEqual(
      ["answer_md", "canonical_q", "category_code", "domains", "entry_key", "is_active", "sources", "variants"].sort(),
    );

    fireEvent.click(screen.getByRole("button", { name: strings.actions.confirm }));

    // Never reports success while the operation is still validating/accepted/processing.
    expect(screen.queryByText(strings.operation.completed)).toBeNull();

    // A single waitFor around every post-success assertion: reload (a
    // separate state update reacting to the same completion) can land a
    // tick after the success text appears, so asserting them apart is racy.
    await waitFor(
      () => {
        expect(screen.getAllByText(strings.operation.completed)).toHaveLength(1);
        expect(listEntriesCallCount).toBeGreaterThan(listEntriesBefore);
      },
      { timeout: 5000 },
    );
    expect(publishCalls).toHaveLength(1);
  });
});

describe("publish rejected with a version conflict", () => {
  it("shows the conflict state, offers reload, never shows success, never re-sends, and keeps the typed text", async () => {
    overrides.publishPackChanges = async () => {
      throw new ApiError(409, {
        code: "PACK_VERSION_CONFLICT",
        message: "Balíček od načtení změnil jiný uživatel.",
        retryable: false,
      });
    };

    const { default: App } = await import("../App");
    render(<App />);

    const answer = await selectAndEdit(KAVCI_HORY_QUESTION, "Rozepsaná odpověď před konfliktem.");
    await openPreview();
    fireEvent.click(screen.getByRole("button", { name: strings.actions.confirm }));

    expect(await screen.findByText(strings.conflict.title)).toBeTruthy();
    expect(screen.getByRole("button", { name: strings.conflict.reload })).toBeTruthy();
    expect(screen.queryByText(strings.operation.completed)).toBeNull();
    expect((screen.getByLabelText(strings.editor.answer) as HTMLTextAreaElement).value).toBe(
      "Rozepsaná odpověď před konfliktem.",
    );
    expect(answer.value).toBe("Rozepsaná odpověď před konfliktem.");

    // No silent retry: still exactly one publish attempt.
    expect(publishCalls).toHaveLength(1);
  });
});

describe("operation ends failed or unknown", () => {
  it("renders a failed operation as failure, not success", async () => {
    overrides.getOperation = async (operationId: string): Promise<Operation> => ({
      operation_id: operationId,
      status: "failed",
      school: "ZS_KAVCI_HORY",
      pack_id: "faq-provoz",
      // Flat, exactly as the server writes it into the operation record.
      error: { code: "UPSTREAM_ERROR", message: "Ingest selhal.", retryable: true },
    });

    const { default: App } = await import("../App");
    render(<App />);

    await selectAndEdit(KAVCI_HORY_QUESTION, "Odpověď, jejíž zveřejnění selže.");
    await openPreview();
    fireEvent.click(screen.getByRole("button", { name: strings.actions.confirm }));

    expect(await screen.findByText(new RegExp(strings.operation.failed))).toBeTruthy();
    expect(screen.queryByText(strings.operation.completed)).toBeNull();
    // The upstream message must actually reach the screen. Reading it through
    // the wrong shape (error.error.message) crashed the live SPA while the
    // mock happily supplied the extra level — hence the assertion on the text.
    expect(await screen.findByText(/Ingest selhal\./)).toBeTruthy();
  });

  it("renders an unknown terminal status as 'stav nejistý', never as success and never silently retried", async () => {
    let calls = 0;
    overrides.getOperation = async (operationId: string): Promise<Operation> => {
      calls += 1;
      return { operation_id: operationId, status: "unknown", school: "ZS_KAVCI_HORY", pack_id: "faq-provoz" };
    };

    const { default: App } = await import("../App");
    render(<App />);

    await selectAndEdit(KAVCI_HORY_QUESTION, "Odpověď se stavem nejistý.");
    await openPreview();
    fireEvent.click(screen.getByRole("button", { name: strings.actions.confirm }));

    expect(await screen.findByText(strings.operation.unknown)).toBeTruthy();
    expect(screen.queryByText(strings.operation.completed)).toBeNull();
    expect(screen.queryByText(new RegExp(strings.operation.failed))).toBeNull();
    // Terminal on the first poll: pollOperationUntilDone must not keep polling.
    expect(calls).toBe(1);
  });
});

describe("new question and reversible deactivation", () => {
  it("sends no client-invented entry_key for a new question, and a deactivated entry is reversible before publishing", async () => {
    const { default: App } = await import("../App");
    render(<App />);
    await screen.findByText(KAVCI_HORY_QUESTION);

    fireEvent.click(screen.getByRole("button", { name: strings.list.addQuestion }));
    // Not findByLabelText: the editor's own <section> carries the SAME text
    // as an aria-label (for the whole panel), so scope to the textbox role.
    const questionInput = (await screen.findByRole("textbox", { name: strings.editor.question })) as HTMLInputElement;
    fireEvent.change(questionInput, { target: { value: "Kolik stojí školní obědy?" } });
    const answer = (await screen.findByLabelText(strings.editor.answer)) as HTMLTextAreaElement;
    fireEvent.change(answer, { target: { value: "Cena obědu je 45 Kč." } });

    await openPreview();
    const addedBody = previewCalls[previewCalls.length - 1][2];
    expect(addedBody.entries).toHaveLength(1);
    expect(addedBody.entries[0].entry_key).toBeNull();
    // Scoped to the diff itself: the same question text also appears in the
    // (still open) draft list item, so an unscoped query would be ambiguous.
    const diffSummary = document.querySelector(".diff-summary") as HTMLElement;
    expect(diffSummary.textContent).toContain("Kolik stojí školní obědy?");

    fireEvent.click(screen.getByRole("button", { name: strings.actions.cancel }));

    // Deactivate an existing entry, confirm dialog accepted.
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fireEvent.click(await screen.findByText(SECOND_QUESTION));
    fireEvent.click(await screen.findByText(strings.actions.deactivate));
    expect(await screen.findByText(strings.editor.statusInactive)).toBeTruthy();

    await openPreview();
    const deactivateBody = previewCalls[previewCalls.length - 1][2];
    const deactivateWrite = deactivateBody.entries.find((e) => e.entry_key === "druzina-poplatek");
    expect(deactivateWrite?.is_active).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: strings.actions.cancel }));

    // Reversible: re-activate before publishing, then the preview no longer
    // carries a deactivation for this entry.
    fireEvent.click(screen.getByText(strings.editor.reactivate));
    expect(await screen.findByText(strings.editor.statusActive)).toBeTruthy();

    // Reversal brought the draft back in line with its baseline, so it is
    // no longer a pending change at all — nothing to deactivate is sent.
    await openPreview();
    const reactivatedBody = previewCalls[previewCalls.length - 1][2];
    expect(reactivatedBody.entries.some((e) => e.entry_key === "druzina-poplatek")).toBe(false);
  });
});
