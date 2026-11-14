import { useLayoutEffect, useRef } from "react";

/**
 * Sizes the QA work area to the viewport: sets `--qa-list-offset` on the element to the
 * space the page needs above it (header, pack bar) and below it (main padding, footer), so
 * `calc(100dvh - var(--qa-list-offset))` is exactly the room left. The header and the pack bar
 * wrap at different widths, which is why a fixed number in CSS cannot do this.
 */
export function useWorkAreaOffset<T extends HTMLElement>(active: boolean) {
  const ref = useRef<T>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!active || !el) return;
    const update = () => {
      const top = el.getBoundingClientRect().top + window.scrollY;
      const footer = document.querySelector<HTMLElement>(".app-footer")?.offsetHeight ?? 0;
      const main = document.querySelector(".app-main");
      const pad = main ? parseFloat(getComputedStyle(main).paddingBottom) || 0 : 0;
      el.style.setProperty("--qa-list-offset", `${Math.round(top + footer + pad)}px`);
    };
    update();
    window.addEventListener("resize", update);
    // Converges: a new offset changes the heights below, which measures to the same offset again.
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    observer?.observe(document.body);
    return () => {
      window.removeEventListener("resize", update);
      observer?.disconnect();
    };
  }, [active]);
  return ref;
}
