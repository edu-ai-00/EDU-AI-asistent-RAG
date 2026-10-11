import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/types";
import { strings } from "../strings.cs";

const KAVCI_HORY_QUESTION = "Do kdy musím omluvit nepřítomnost dítěte ve škole?";

type PreviewCall = Parameters<ApiClient["previewPackChanges"]>;

let previewCalls: PreviewCall[] = [];
let overrides: { createPack?: ApiClient["createPack"] } = {};

// Production polls every 1.5 s for up to three minutes (config.ts) — shorten
// only the clock here so a full confirm+poll in the acceptance test does not
// need a real-time wait.
vi.mock("../config", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../config")>()),
  PUBLISH_POLL_INTERVAL_MS: 10,
  PUBLISH_POLL_MAX_ATTEMPTS: 40,
}));

// Wraps the real fixture-backed mock client so previewPackChanges/createPack
// are observable and, per test, individually overridable, without touching
// mockClient.ts itself — same technique as publishFlow.test.tsx.
vi.mock("../api/mockClient", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/mockClient")>();
  return {
    ...actual,
    createMockClient: (): ApiClient => {
      const real = actual.createMockClient();
      return {
        ...real,
        createPack: (...args) => (overrides.createPack ?? real.createPack)(...args),
        previewPackChanges: (...args) => {
          previewCalls.push(args);
          return real.previewPackChanges(...args);
        },
      };
    },
  };
});

beforeEach(() => {
  previewCalls = [];
  overrides = {};
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

async function openNewPackDialog() {
  fireEvent.click(await screen.findByRole("button", { name: strings.toolbar.newPack }));
  const packIdInput = (await screen.findByLabelText(strings.newPack.packId)) as HTMLInputElement;
  // Options load asynchronously (GET .../metadata-options) — wait for the
  // fixture roles to actually render before interacting with them.
  await screen.findByRole("checkbox", { name: "Učitel" });
  return packIdInput;
}

describe("new pack metadata dialog — success path", () => {
  it("carries the confirmed metadata through preview with base_etag '' and clears the pending state on publish", async () => {
    const { default: App } = await import("../App");
    render(<App />);
    await screen.findByText(KAVCI_HORY_QUESTION);

    const packIdInput = await openNewPackDialog();
    fireEvent.change(packIdInput, { target: { value: "new-pack-1" } });
    fireEvent.click(screen.getByRole("radio", { name: strings.newPack.visibilityEntity }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Učitel" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "1. ročník" }));

    fireEvent.click(screen.getByRole("button", { name: strings.newPack.create }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    // The new pack has no server state: the entry list is empty and
    // editable, not a 404. Add the first question through the normal flow.
    fireEvent.click(screen.getByRole("button", { name: strings.list.addQuestion }));
    const questionInput = (await screen.findByRole("textbox", { name: strings.editor.question })) as HTMLInputElement;
    fireEvent.change(questionInput, { target: { value: "Kdy začíná zápis do první třídy?" } });
    const answer = (await screen.findByLabelText(strings.editor.answer)) as HTMLTextAreaElement;
    fireEvent.change(answer, { target: { value: "Zápis probíhá v dubnu." } });

    fireEvent.click(screen.getByRole("button", { name: strings.actions.publish }));
    await screen.findByRole("button", { name: strings.actions.confirm });

    expect(previewCalls).toHaveLength(1);
    const previewBody = previewCalls[0][2];
    expect(previewBody.base_etag).toBe("");
    expect(previewBody.pack_metadata).toEqual({
      visibility: "entity",
      audience_roles: ["TEACHER"],
      school_grade_codes: ["1"],
    });

    fireEvent.click(screen.getByRole("button", { name: strings.actions.confirm }));
    await waitFor(() => expect(screen.getAllByText(strings.operation.completed)).toHaveLength(1), { timeout: 5000 });

    // Pending state cleared: the pack now behaves like any other listed
    // pack (its selector option is no longer the ad hoc pending one).
    await waitFor(() => {
      const packSelect = screen.getByLabelText(strings.toolbar.pack) as HTMLSelectElement;
      expect(packSelect.value).toBe("new-pack-1");
    });
  });
});

describe("new pack metadata dialog — failure paths", () => {
  it("keeps the create button disabled until at least one role is selected", async () => {
    const { default: App } = await import("../App");
    render(<App />);
    await screen.findByText(KAVCI_HORY_QUESTION);

    const packIdInput = await openNewPackDialog();
    fireEvent.change(packIdInput, { target: { value: "new-pack-2" } });
    fireEvent.click(screen.getByRole("radio", { name: strings.newPack.visibilityEntity }));

    expect((screen.getByRole("button", { name: strings.newPack.create }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows a 422 pointing at pack_metadata.audience_roles[0] and keeps the dialog open with nothing lost", async () => {
    overrides.createPack = async () => {
      throw new ApiError(422, {
        code: "VALIDATION_ERROR",
        message: "pack_metadata did not pass validation",
        retryable: false,
        fields: [{ field: "pack_metadata.audience_roles[0]", message: "unknown role code 'BOGUS'" }],
      });
    };

    const { default: App } = await import("../App");
    render(<App />);
    await screen.findByText(KAVCI_HORY_QUESTION);

    const packIdInput = await openNewPackDialog();
    fireEvent.change(packIdInput, { target: { value: "new-pack-3" } });
    fireEvent.click(screen.getByRole("radio", { name: strings.newPack.visibilityEntity }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Učitel" }));

    fireEvent.click(screen.getByRole("button", { name: strings.newPack.create }));

    expect(await screen.findByText(/pack_metadata\.audience_roles\[0\]/)).toBeTruthy();
    // Still open, still holding what was typed.
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect((screen.getByLabelText(strings.newPack.packId) as HTMLInputElement).value).toBe("new-pack-3");
    expect((screen.getByRole("checkbox", { name: "Učitel" }) as HTMLInputElement).checked).toBe(true);
  });
});

describe("regression: publishing an existing pack never sends pack_metadata", () => {
  it("omits the pack_metadata key entirely from the preview body", async () => {
    const { default: App } = await import("../App");
    render(<App />);

    fireEvent.click(await screen.findByText(KAVCI_HORY_QUESTION));
    const answer = (await screen.findByLabelText(strings.editor.answer)) as HTMLTextAreaElement;
    fireEvent.change(answer, { target: { value: "Odpověď na existující balíček, beze změny metadat." } });

    fireEvent.click(screen.getByRole("button", { name: strings.actions.publish }));
    await screen.findByRole("button", { name: strings.actions.confirm });

    expect(previewCalls).toHaveLength(1);
    const previewBody = previewCalls[0][2];
    expect(previewBody).not.toHaveProperty("pack_metadata");
    expect(previewBody.base_etag).not.toBe("");
  });
});
