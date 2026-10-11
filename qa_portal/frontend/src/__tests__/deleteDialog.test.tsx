import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/types";
import { strings } from "../strings.cs";
import { DeleteDialog } from "../components/DeleteDialog";

afterEach(() => {
  cleanup();
});

function makeClient(overrides: Partial<ApiClient>): ApiClient {
  return { ...overrides } as unknown as ApiClient;
}

const PACK = "faq-provoz";
const SCHOOL = "ZAKLADNI_SKOLA_BOROVA";

describe("DeleteDialog — a whole pack (CONTRACT.md §13.2)", () => {
  it("shows school and scope, and keeps confirm disabled until the id is typed EXACTLY", async () => {
    const deletePack = vi.fn(async () => ({
      deleted: { entries: 3, variants: 6, embeddings: 9, manifests: 1 },
    }));
    render(
      <DeleteDialog
        client={makeClient({ deletePack })}
        target={{ kind: "pack", school: SCHOOL, pack: PACK, entryCount: 3 }}
        onClose={() => {}}
        onDeleted={() => {}}
      />,
    );

    // The admin must see WHOSE data and HOW MUCH of it disappears.
    expect(screen.getByText(new RegExp(SCHOOL))).toBeTruthy();
    expect(screen.getAllByText(new RegExp(PACK)).length).toBeGreaterThan(0);
    expect(screen.getByText(/3/)).toBeTruthy();

    const confirm = screen.getByRole("button", { name: strings.deletePermanent.confirm }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);

    const field = screen.getByLabelText(strings.deletePermanent.typeToConfirmTemplate(PACK));
    // A near miss must not unlock it.
    fireEvent.change(field, { target: { value: PACK.slice(0, -1) } });
    expect((screen.getByRole("button", { name: strings.deletePermanent.confirm }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(field, { target: { value: PACK.toUpperCase() } });
    expect((screen.getByRole("button", { name: strings.deletePermanent.confirm }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(field, { target: { value: PACK } });
    const enabled = screen.getByRole("button", { name: strings.deletePermanent.confirm }) as HTMLButtonElement;
    expect(enabled.disabled).toBe(false);

    fireEvent.click(enabled);
    await waitFor(() => expect(deletePack).toHaveBeenCalledTimes(1));
    expect(deletePack).toHaveBeenCalledWith(SCHOOL, PACK, { confirm_pack_id: PACK });
  });

  it("shows the server's refusal instead of reporting success", async () => {
    const deletePack = vi.fn(async () => {
      throw new ApiError(409, {
        code: "PUBLISH_IN_PROGRESS",
        message: "Pro balíček právě probíhá jiná operace.",
        retryable: true,
      });
    });
    const onDeleted = vi.fn();
    render(
      <DeleteDialog
        client={makeClient({ deletePack })}
        target={{ kind: "pack", school: SCHOOL, pack: PACK, entryCount: 1 }}
        onClose={() => {}}
        onDeleted={onDeleted}
      />,
    );

    fireEvent.change(screen.getByLabelText(strings.deletePermanent.typeToConfirmTemplate(PACK)), { target: { value: PACK } });
    fireEvent.click(screen.getByRole("button", { name: strings.deletePermanent.confirm }));

    expect(await screen.findByText("Pro balíček právě probíhá jiná operace.")).toBeTruthy();
    expect(onDeleted).not.toHaveBeenCalled();
  });
});

describe("DeleteDialog — a single question", () => {
  it("names the question and deletes it without asking for a typed confirmation", async () => {
    const deleteEntry = vi.fn(async () => ({
      deleted: { entries: 1, variants: 2, embeddings: 3, manifests: 0 },
    }));
    const onDeleted = vi.fn();
    render(
      <DeleteDialog
        client={makeClient({ deleteEntry })}
        target={{ kind: "entry", school: SCHOOL, pack: PACK, entryKey: "obedy", name: "Kdy se platí obědy?" }}
        onClose={() => {}}
        onDeleted={onDeleted}
      />,
    );

    expect(screen.getByText(/Kdy se platí obědy\?/)).toBeTruthy();
    expect(screen.queryByLabelText(strings.deletePermanent.typeToConfirmTemplate(PACK))).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: strings.deletePermanent.confirm }));
    await waitFor(() => expect(deleteEntry).toHaveBeenCalledTimes(1));
    expect(deleteEntry).toHaveBeenCalledWith(SCHOOL, PACK, "obedy");
    await waitFor(() => expect(onDeleted).toHaveBeenCalledTimes(1));
  });
});
