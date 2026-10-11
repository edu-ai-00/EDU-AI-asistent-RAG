import { useEffect, useId, useRef, type ReactNode } from "react";
import { strings } from "../strings.cs";

type Props = {
  title: string;
  onClose: () => void;
  children: ReactNode;
  /** Optional fixed footer (e.g. confirm/cancel buttons) rendered as its own
   *  grid row, outside the scrolling body — so it stays reachable on a short
   *  viewport regardless of how tall the body's content grows (CONTRACT.md
   *  §10.1). When omitted, the shell renders exactly as before. */
  footer?: ReactNode;
};

const FOCUSABLE_SELECTOR =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Minimal accessible modal shell (role="dialog", aria-modal, labelled by its
 *  own heading). On mount: remembers the element that had focus (the
 *  trigger) and moves focus inside. While open: Escape closes it, Tab/Shift+Tab
 *  never leave it. On unmount: focus returns to the trigger. Every dialog in
 *  this portal (import/export/new pack) renders through this one shell so the
 *  behaviour is identical everywhere — no dialog reimplements it. */
export function Dialog({ title, onClose, children, footer }: Props) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const previouslyFocused = useRef<HTMLElement | null>(null);

  useEffect(() => {
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    const node = dialogRef.current;
    const body = bodyRef.current;
    // The scroll region itself is now focusable (tabindex="0") so it can
    // match FOCUSABLE_SELECTOR too — skip it and land on the first real
    // control, same as before this region existed.
    const candidates = node ? Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)) : [];
    const first = candidates.find((el) => el !== body);
    first?.focus();
    return () => {
      previouslyFocused.current?.focus();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const node = dialogRef.current;
      if (!node) return;
      const focusables = Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <div className="dialog-backdrop">
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} ref={dialogRef}>
        <div className="dialog-header">
          <h2 id={titleId}>{title}</h2>
          <button type="button" className="dialog-close" onClick={onClose} aria-label={strings.actions.close}>
            ×
          </button>
        </div>
        <div
          className="dialog-body"
          ref={bodyRef}
          tabIndex={0}
          role="group"
          aria-label={strings.actions.dialogScrollRegion}
        >
          {children}
        </div>
        {footer && <div className="dialog-footer">{footer}</div>}
      </div>
    </div>
  );
}
