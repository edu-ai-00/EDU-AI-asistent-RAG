import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { OverflowMenu } from "../components/OverflowMenu";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function setup(over: { disabledSecond?: boolean } = {}) {
  const first = vi.fn();
  const second = vi.fn();
  const third = vi.fn();
  render(
    <div>
      <button type="button">Mimo menu</button>
      <OverflowMenu
        label="Další akce"
        items={[
          { key: "a", label: "Detail", onSelect: first },
          { key: "b", label: "Upravit", onSelect: second, disabled: over.disabledSecond },
          { key: "c", label: "Smazat…", onSelect: third, danger: true },
        ]}
      />
    </div>,
  );
  return { first, second, third, trigger: screen.getByRole("button", { name: "Další akce" }) };
}

describe("OverflowMenu accessibility", () => {
  it("exposes aria-haspopup and aria-expanded and opens a menu of menuitems", () => {
    const { trigger } = setup();
    expect(trigger.getAttribute("aria-haspopup")).toBe("menu");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("menu")).toBeNull();

    fireEvent.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    const menu = screen.getByRole("menu", { name: "Další akce" });
    expect(trigger.getAttribute("aria-controls")).toBe(menu.id);
    expect(screen.getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["Detail", "Upravit", "Smazat…"]);
  });

  it("ArrowDown/ArrowUp on the trigger open it and focus the first/last item", () => {
    const { trigger } = setup();
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Detail" }));
    cleanup();

    const again = setup();
    again.trigger.focus();
    fireEvent.keyDown(again.trigger, { key: "ArrowUp" });
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Smazat…" }));
  });

  it("arrow keys move and wrap, Home/End jump, a disabled item is skipped", () => {
    const { trigger } = setup({ disabledSecond: true });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    const menu = screen.getByRole("menu");
    const detail = screen.getByRole("menuitem", { name: "Detail" });
    const remove = screen.getByRole("menuitem", { name: "Smazat…" });

    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(remove); // "Upravit" is disabled
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(detail); // wraps
    fireEvent.keyDown(menu, { key: "ArrowUp" });
    expect(document.activeElement).toBe(remove);
    fireEvent.keyDown(menu, { key: "Home" });
    expect(document.activeElement).toBe(detail);
    fireEvent.keyDown(menu, { key: "End" });
    expect(document.activeElement).toBe(remove);
  });

  it("Escape closes the menu and returns focus to the trigger", () => {
    const { trigger } = setup();
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(trigger);
  });

  it("an outside click closes it without moving focus back", () => {
    const { trigger } = setup();
    fireEvent.click(trigger);
    expect(screen.getByRole("menu")).toBeTruthy();
    fireEvent.mouseDown(screen.getByRole("button", { name: "Mimo menu" }));
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("choosing an item runs it once and closes the menu", () => {
    const { trigger, second, first } = setup();
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("menuitem", { name: "Upravit" }));
    expect(second).toHaveBeenCalledTimes(1);
    expect(first).not.toHaveBeenCalled();
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });
  it("stays open and follows the trigger while the page scrolls", async () => {
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect");
    rect.mockReturnValue(new DOMRect(500, 100, 32, 32));
    const { trigger } = setup();
    fireEvent.click(trigger);
    rect.mockReturnValue(new DOMRect(500, 60, 32, 32));
    await act(async () => {
      fireEvent.scroll(window);
      await new Promise((r) => requestAnimationFrame(() => r(null)));
    });
    expect(screen.getByRole("menu").style.top).toBe("96px");
  });

  it("closes once the trigger has scrolled out of view", async () => {
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect");
    rect.mockReturnValue(new DOMRect(500, 100, 32, 32));
    const { trigger } = setup();
    fireEvent.click(trigger);
    rect.mockReturnValue(new DOMRect(500, -80, 32, 32));
    await act(async () => {
      fireEvent.scroll(window);
      await new Promise((r) => requestAnimationFrame(() => r(null)));
    });
    expect(screen.queryByRole("menu")).toBeNull();
  });
});
