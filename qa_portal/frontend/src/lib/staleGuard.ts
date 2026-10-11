/**
 * Guards against a slow response for a PREVIOUS request key overwriting a
 * newer one — e.g. school A's entries arriving after the user already
 * switched to school B (CONTRACT.md §10). A generation counter, not only
 * AbortController: an aborted fetch can still resolve in some environments,
 * so the generation check is the actual source of truth.
 */
export class LatestOnly<Key, Value> {
  private generation = 0;
  private controller: AbortController | null = null;

  constructor(private readonly load: (key: Key, signal: AbortSignal) => Promise<Value>) {}

  /** Cancels any in-flight request and starts a new one for `key`. Only the
   *  callbacks of the LAST call to `request` may ever fire with a result. */
  request(key: Key, onResult: (key: Key, value: Value) => void, onError: (key: Key, error: unknown) => void): void {
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;
    const mine = ++this.generation;

    this.load(key, controller.signal).then(
      (value) => {
        if (mine !== this.generation) return; // superseded — drop silently
        onResult(key, value);
      },
      (error) => {
        if (mine !== this.generation) return;
        if (error instanceof DOMException && error.name === "AbortError") return;
        onError(key, error);
      },
    );
  }

  /** Stops any in-flight request from ever calling back, without starting a
   *  new one — used on unmount. */
  cancel(): void {
    this.controller?.abort();
    this.generation++;
  }
}
