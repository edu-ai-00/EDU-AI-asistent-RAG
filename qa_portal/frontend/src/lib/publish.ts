import type { ApiErrorShape, Operation, OperationStatus } from "../api/types";

export type PublishState =
  | { status: "idle" }
  | { status: "publishing" }
  | { status: "conflict"; error: ApiErrorShape }
  | { status: "metadata_divergent"; error: ApiErrorShape }
  | { status: "uploader_unavailable"; error: ApiErrorShape }
  | { status: "failed"; error: ApiErrorShape }
  | { status: "success"; operation: Operation };

export type PublishAction =
  | { type: "start" }
  | { type: "resolved"; operation: Operation }
  | { type: "rejected"; error: ApiErrorShape };

/**
 * NEVER transitions straight to "success" on a rejected request, and a
 * PACK_VERSION_CONFLICT never falls through to a generic failure that a
 * caller might retry blindly — it gets its own state so the UI can offer
 * reload + compare, per CONTRACT.md §3 ("žádný automatický rebase").
 */
export function publishReducer(_state: PublishState, action: PublishAction): PublishState {
  switch (action.type) {
    case "start":
      return { status: "publishing" };
    case "resolved":
      return { status: "success", operation: action.operation };
    case "rejected":
      if (action.error.code === "PACK_VERSION_CONFLICT") return { status: "conflict", error: action.error };
      if (action.error.code === "PACK_METADATA_DIVERGENT") return { status: "metadata_divergent", error: action.error };
      if (action.error.code === "PUBLISH_UPLOADER_UNAVAILABLE") return { status: "uploader_unavailable", error: action.error };
      return { status: "failed", error: action.error };
  }
}

const TERMINAL: readonly OperationStatus[] = ["completed", "failed", "unknown"];

/**
 * Polls GET /operations/{id} until it reaches a terminal status. `success`
 * for the CALLER is only ever "status === completed" checked against THIS
 * result — never the 202 that started the operation and never a timer
 * (CONTRACT.md §6: "Zavření HTTP požadavku ani animace nejsou důkaz úspěchu").
 * `wait` is injected so tests do not depend on real timers.
 */
export async function pollOperationUntilDone(
  operationId: string,
  getOperation: (id: string) => Promise<Operation>,
  wait: (ms: number) => Promise<void>,
  options: { intervalMs?: number; maxAttempts?: number } = {},
): Promise<Operation> {
  const intervalMs = options.intervalMs ?? 1500;
  const maxAttempts = options.maxAttempts ?? 60;

  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    const operation = await getOperation(operationId);
    if (TERMINAL.includes(operation.status)) return operation;
    if (attempt < maxAttempts - 1) await wait(intervalMs);
  }
  throw new Error("Časový limit pro zjištění stavu publikace vypršel. Stav zůstává otevřený, zkontrolujte jej prosím znovu.");
}
