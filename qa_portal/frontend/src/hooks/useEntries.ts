import { useMemo } from "react";
import type { ApiClient } from "../api/client";
import type { EntryPage } from "../api/types";
import { useKeyedResource } from "./useKeyedResource";

/**
 * Loads the entries page for one (school, pack, search query). The composite
 * string key is what makes the race guard work: a slow response for
 * "SCHOOL_A::pack" can only ever win if it is still the CURRENT key when it
 * resolves — switching school or pack changes the key immediately, and
 * useKeyedResource/LatestOnly then drops the stale result silently.
 */
export function useEntries(client: ApiClient, school: string | null, pack: string | null, query: string) {
  const key = school && pack ? `${school}::${pack}::${query}` : null;

  const { state, reload } = useKeyedResource<EntryPage>(key, (_k, signal) => {
    if (!school || !pack) return Promise.reject(new Error("Škola nebo balíček nejsou vybrané."));
    return client.listEntries(school, pack, { q: query || undefined }, signal);
  });

  return useMemo(() => ({ state, reload }), [state, reload]);
}
