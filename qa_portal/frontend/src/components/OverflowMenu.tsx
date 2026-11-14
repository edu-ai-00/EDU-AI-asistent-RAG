import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { strings } from "../strings.cs";

export type MenuItem = {
  key: string;
  label: string;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
};

type Props = {
  /** Accessible name of the trigger button. */
  label?: string;
  items: MenuItem[];
};

/** "⋯" overflow menu: aria-haspopup/aria-expanded trigger, arrow keys, Home/End,
 *  Escape and outside click close it. The list is position:fixed so a scrolling
 *  table wrapper cannot clip it; it follows the trigger while the page scrolls. */
export function OverflowMenu({ label = strings.menu.moreActions, items }: Props) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; right: number }>({ top: 0, right: 0 });
  const rootRef = useRef<HTMLSpanElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const pendingFocus = useRef<"first" | "last" | null>(null);
  const menuId = useId();

  function enabledItems(): HTMLButtonElement[] {
    return itemRefs.current.filter((el): el is HTMLButtonElement => el !== null && !el.disabled);
  }

  /** Pins the list under the trigger; false once the trigger has left the viewport. */
  function place(): boolean {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return false;
    setPos({ top: rect.bottom + 4, right: Math.max(8, window.innerWidth - rect.right) });
    return rect.bottom > 0 && rect.top < window.innerHeight && rect.right > 0 && rect.left < window.innerWidth;
  }

  function openMenu(focus: "first" | "last" | null) {
    place();
    pendingFocus.current = focus;
    setOpen(true);
  }

  function close(restoreFocus: boolean) {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  }

  useEffect(() => {
    if (!open) return;
    const enabled = enabledItems();
    if (pendingFocus.current !== null) {
      (pendingFocus.current === "last" ? enabled[enabled.length - 1] : enabled[0])?.focus();
    }
    pendingFocus.current = null;
    function onPointerDown(event: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    }
    // Follow the trigger instead of closing on any scroll: a click made while
    // trackpad momentum or a smooth scroll is still running would otherwise
    // close the menu right after it opened.
    let frame = 0;
    const follow = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (!place()) setOpen(false);
      });
    };
    document.addEventListener("mousedown", onPointerDown);
    window.addEventListener("resize", follow);
    window.addEventListener("scroll", follow, true);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("mousedown", onPointerDown);
      window.removeEventListener("resize", follow);
      window.removeEventListener("scroll", follow, true);
    };
  }, [open]);

  function onTriggerKey(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      openMenu(event.key === "ArrowDown" ? "first" : "last");
    }
  }

  function onMenuKey(event: KeyboardEvent<HTMLUListElement>) {
    const enabled = enabledItems();
    const index = enabled.indexOf(document.activeElement as HTMLButtonElement);
    const focusAt = (i: number) => enabled[(i + enabled.length) % enabled.length]?.focus();
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        focusAt(index + 1);
        break;
      case "ArrowUp":
        event.preventDefault();
        focusAt(index - 1);
        break;
      case "Home":
        event.preventDefault();
        focusAt(0);
        break;
      case "End":
        event.preventDefault();
        focusAt(enabled.length - 1);
        break;
      case "Escape":
        // Must not also close a dialog or drawer the menu lives in.
        event.preventDefault();
        event.nativeEvent.stopImmediatePropagation();
        close(true);
        break;
      case "Tab":
        close(false);
        break;
    }
  }

  if (items.length === 0) return null;

  return (
    <span className="overflow-menu" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className="btn btn--ghost btn--icon"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => (open ? close(false) : openMenu(null))}
        onKeyDown={onTriggerKey}
      >
        <span aria-hidden="true">⋯</span>
      </button>
      {open && (
        <ul
          id={menuId}
          role="menu"
          aria-label={label}
          className="overflow-menu-list"
          style={{ top: pos.top, right: pos.right }}
          onKeyDown={onMenuKey}
        >
          {items.map((item, i) => (
            <li key={item.key} role="none">
              <button
                ref={(el) => {
                  itemRefs.current[i] = el;
                }}
                type="button"
                role="menuitem"
                tabIndex={-1}
                disabled={item.disabled}
                className={item.danger ? "overflow-menu-item is-danger" : "overflow-menu-item"}
                onClick={() => {
                  // Focus goes back to the trigger first, so a dialog opened next restores it on close.
                  close(true);
                  item.onSelect();
                }}
              >
                {item.label}
              </button>
            </li>
          ))}
        </ul>
      )}
    </span>
  );
}
