import type { Entry } from "../api/types";

/** Search over question, variants and answer, case-insensitive (Czech locale); an empty query keeps all. */
export function filterEntries(entries: Entry[], query: string, onlyNoAreas: boolean): Entry[] {
  const needle = query.trim().toLocaleLowerCase("cs");
  return entries.filter((entry) => {
    if (onlyNoAreas && entry.domains.length > 0) return false;
    if (needle === "") return true;
    const haystack = [entry.canonical_q, entry.answer_md, ...(entry.variants ?? [])].join("\n").toLocaleLowerCase("cs");
    return haystack.includes(needle);
  });
}

export type PagerItem = number | "gap";

/** Page numbers for `‹ 1 2 3 … 5 ›`: first, last and the current page with its neighbours. */
export function pagerItems(current: number, total: number): PagerItem[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const wanted = new Set([1, total, current - 1, current, current + 1].filter((n) => n >= 1 && n <= total));
  const sorted = [...wanted].sort((a, b) => a - b);
  const items: PagerItem[] = [];
  sorted.forEach((n, i) => {
    if (i > 0 && n - sorted[i - 1] > 1) items.push("gap");
    items.push(n);
  });
  return items;
}
