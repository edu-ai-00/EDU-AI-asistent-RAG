import { useEffect, useRef, useState, useCallback } from "react";
import { LatestOnly } from "../lib/staleGuard";

export type AsyncState<T> =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; value: T };

/**
 * Shared building block for every school/pack-scoped fetch (schools, packs,
 * domains, entries). `key` must be a plain string so it can serve directly
 * as a useEffect dependency; callers compose it (e.g. `${school}::${pack}`).
 * A `null` key means "nothing selected yet" and loads nothing.
 */
export function useKeyedResource<Value>(
  key: string | null,
  load: (key: string, signal: AbortSignal) => Promise<Value>,
): { state: AsyncState<Value> | { status: "empty" }; loadedKey: string | null; reload: () => void } {
  const [state, setState] = useState<AsyncState<Value> | { status: "empty" }>({ status: "empty" });
  // Which key the current `state` belongs to. A caller's effect can otherwise
  // read a still-"ready" value from the PREVIOUS key in the render right after
  // the key changed, before this hook's own effect has run.
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  // `load` may close over values (e.g. a search query) that change without
  // changing `key` — dispatch through a ref so LatestOnly (constructed once)
  // always calls the LATEST closure rather than the one from first render.
  const loadRef = useRef(load);
  loadRef.current = load;
  const guardRef = useRef<LatestOnly<string, Value> | null>(null);
  if (!guardRef.current) guardRef.current = new LatestOnly((k, signal) => loadRef.current(k, signal));

  useEffect(() => {
    if (key === null) {
      setState({ status: "empty" });
      setLoadedKey(null);
      return;
    }
    setState({ status: "loading" });
    setLoadedKey(null);
    guardRef.current!.request(
      key,
      (k, value) => {
        setState({ status: "ready", value });
        setLoadedKey(k);
      },
      (k, error) => {
        setState({ status: "error", message: error instanceof Error ? error.message : "Nepodařilo se načíst data." });
        setLoadedKey(k);
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, nonce]);

  useEffect(() => () => guardRef.current?.cancel(), []);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { state, loadedKey, reload };
}
