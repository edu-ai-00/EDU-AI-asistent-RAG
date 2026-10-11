import type { EntryWrite } from "../api/types";
import type { EntryDraft } from "./drafts";

/**
 * Builds the wire shape for one edited question. Two rules the contract is
 * explicit about and that are easy to get wrong with a naive spread:
 *  - `domains` is ALWAYS an explicit array, even when empty — an empty
 *    selection means "explicitly no areas", never "field omitted".
 *  - `category_code` is always present as either a code or explicit `null`,
 *    never omitted and never an empty string standing in for "none".
 */
export function buildEntryWrite(entryKey: string | null, draft: EntryDraft): EntryWrite {
  return {
    entry_key: entryKey,
    canonical_q: draft.canonical_q,
    answer_md: draft.answer_md,
    variants: [...draft.variants],
    sources: draft.sources.map((s) => ({ title: s.title, url: s.url })),
    domains: [...draft.domains],
    category_code: draft.category_code === "" ? null : draft.category_code,
    is_active: draft.is_active,
  };
}
