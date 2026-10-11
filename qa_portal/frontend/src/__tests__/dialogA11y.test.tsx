import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { Dialog } from "../components/Dialog";

afterEach(cleanup);

function Harness() {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        Otevřít dialog
      </button>
      {open && (
        <Dialog title="Testovací dialog" onClose={() => setOpen(false)}>
          <button type="button">Uvnitř A</button>
          <button type="button">Uvnitř B</button>
        </Dialog>
      )}
    </div>
  );
}

describe("Dialog keyboard handling", () => {
  it("moves focus into the dialog, traps Tab inside it, closes on Escape and restores focus to the trigger", async () => {
    render(<Harness />);
    const trigger = screen.getByText("Otevřít dialog");
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = await screen.findByRole("dialog");
    expect(document.activeElement).not.toBe(trigger);
    expect(dialog.contains(document.activeElement)).toBe(true);

    const focusables = within(dialog).getAllByRole("button");
    focusables[0].focus();
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(focusables[focusables.length - 1]);

    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(document.activeElement).toBe(focusables[0]);

    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });
});
