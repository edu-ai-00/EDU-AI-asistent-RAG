import { useMemo } from "react";
import type { ApiClient } from "../api/client";
import type { EntryPage } from "../api/types";
import { useKeyedResource } from "./useKeyedResource";

/** Largest page the portal API allows (qa_portal list_entries: limit 1..200). */
export const ENTRIES_FETCH_LIMIT = 200;
/** A pack this large would be a bug, not data: stop following cursors. */
const MAX_PAGES = 100;

/**
 * Loads EVERY entry of one (school, pack) by following `next_cursor`: the API filters `q` only
 * inside one page, so search runs client-side (EntryList). The school::pack key drops a slow
 * answer for a pack that is no longer selected (useKeyedResource/LatestOnly).
 */
export function useEntries(client: ApiClient, school: string | null, pack: string | null) {
  const key = school && pack ? `${school}::${pack}` : null;

  const { state, reload } = useKeyedResource<EntryPage>(key, async (_k, signal) => {
    if (!school || !pack) throw new Error("Škola nebo balíček nejsou vybrané.");
    let page = await client.listEntries(school, pack, { limit: ENTRIES_FETCH_LIMIT }, signal);
    const entries = [...page.entries];
    const first = page;
    for (let i = 1; i < MAX_PAGES && page.next_cursor; i++) {
      page = await client.listEntries(school, pack, { limit: ENTRIES_FETCH_LIMIT, cursor: page.next_cursor }, signal);
      entries.push(...page.entries);
    }
    // etag and version describe the pack as of the first page; total is what we really hold.
    return { ...first, entries, next_cursor: null, total: entries.length };
  });

  return useMemo(() => ({ state, reload }), [state, reload]);
}
