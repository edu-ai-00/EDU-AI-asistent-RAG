import { useState } from "react";
import type { ApiClient } from "../api/client";
import { ApiError, type ApiErrorShape, type Operation, type PackMetadata, type Preview } from "../api/types";
import { NEW_ENTRY_PREFIX, draftEqualsBaseline, type DraftMap, type EntryDraft } from "../lib/drafts";
import { buildEntryWrite } from "../lib/entryWrite";
import { publishReducer, pollOperationUntilDone, type PublishState } from "../lib/publish";
import { PUBLISH_POLL_INTERVAL_MS, PUBLISH_POLL_MAX_ATTEMPTS } from "../config";
import { strings } from "../strings.cs";
import { DiffSummary } from "./DiffSummary";

type Props = {
  client: ApiClient;
  school: string | null;
  pack: string | null;
  packEtag: string | null;
  drafts: DraftMap;
  baselines: Record<string, EntryDraft>;
  onReload: () => void;
  onPublished: (publishedDraftKeys: string[]) => void;
  /** Set only for a pack with no server state yet (CONTRACT.md §2.4a): the
   *  confirmed metadata from NewPackDialog, carried as `pack_metadata` on
   *  THIS pack's preview call with `base_etag: ""`. An existing pack must
   *  never send pack_metadata — the backend rejects it with 422 on purpose,
   *  since one metadata set applies to the whole pack. */
  pendingPackMetadata?: PackMetadata;
};

type PendingWrite = { key: string; entryKey: string | null; draft: EntryDraft };

type Stage =
  | { kind: "idle" }
  | { kind: "previewing" }
  | { kind: "ready"; preview: Preview; writeKeys: string[] };

type PollResult =
  | { kind: "idle" }
  | { kind: "polling" }
  | { kind: "completed"; operation: Operation }
  | { kind: "failed"; operation: Operation }
  | { kind: "unknown" };

/** Any draft that still differs from its baseline, or was never published at
 *  all (a `__new__:` key), is a candidate write. Untouched open drafts are
 *  left out entirely — the preview call only ever carries what changed. */
function collectPendingWrites(drafts: DraftMap, baselines: Record<string, EntryDraft>): PendingWrite[] {
  const result: PendingWrite[] = [];
  for (const [key, draft] of Object.entries(drafts)) {
    if (key.startsWith(NEW_ENTRY_PREFIX)) {
      result.push({ key, entryKey: null, draft });
      continue;
    }
    const base = baselines[key];
    if (!base || !draftEqualsBaseline(draft, base)) {
      result.push({ key, entryKey: key, draft });
    }
  }
  return result;
}

function toErrorShape(err: unknown): ApiErrorShape {
  if (err instanceof ApiError) {
    return { code: err.code, message: err.message, retryable: err.retryable, fields: err.fields };
  }
  return {
    code: "UNKNOWN",
    message: err instanceof Error ? err.message : strings.errors.generic,
    retryable: false,
  };
}

/**
 * Wires preview -> confirm -> publish -> poll for one pack's pending drafts.
 * Never invents success: the only path to "completed" is an actual
 * `operation.status === "completed"` returned by pollOperationUntilDone.
 * A rejected publish (conflict / metadata divergent / uploader unavailable /
 * generic failure) never clears drafts and never re-sends automatically.
 */
export function PublishPanel({
  client,
  school,
  pack,
  packEtag,
  drafts,
  baselines,
  onReload,
  onPublished,
  pendingPackMetadata,
}: Props) {
  const [stage, setStage] = useState<Stage>({ kind: "idle" });
  const [publishState, setPublishState] = useState<PublishState>({ status: "idle" });
  const [pollResult, setPollResult] = useState<PollResult>({ kind: "idle" });

  if (!school || !pack || packEtag === null) return null;

  const pending = collectPendingWrites(drafts, baselines);
  const hasPending = pending.length > 0;

  async function handlePreview() {
    if (!hasPending) return;
    setStage({ kind: "previewing" });
    setPublishState({ status: "idle" });
    setPollResult({ kind: "idle" });
    try {
      const writes = pending.map((p) => buildEntryWrite(p.entryKey, p.draft));
      // Byte-for-byte the same call as before when there is no pending
      // metadata: no `pack_metadata` key at all, `base_etag` unchanged.
      const body = pendingPackMetadata
        ? { base_etag: "", entries: writes, pack_metadata: pendingPackMetadata }
        : { base_etag: packEtag!, entries: writes };
      const preview = await client.previewPackChanges(school!, pack!, body);
      setStage({ kind: "ready", preview, writeKeys: pending.map((p) => p.key) });
    } catch (err) {
      setPublishState(publishReducer(publishState, { type: "rejected", error: toErrorShape(err) }));
      setStage({ kind: "idle" });
    }
  }

  function handleCancelPreview() {
    setStage({ kind: "idle" });
    setPublishState({ status: "idle" });
  }

  function handleReloadAndCompare() {
    // Deliberately does NOT touch drafts: the conflict contract forbids a
    // silent rebase, so the admin's in-progress text must survive this.
    setStage({ kind: "idle" });
    setPublishState({ status: "idle" });
    setPollResult({ kind: "idle" });
    onReload();
  }

  async function handleConfirmPublish() {
    if (stage.kind !== "ready") return;
    const { preview, writeKeys } = stage;
    setPublishState({ status: "publishing" });
    try {
      const operation = await client.publishPackChanges(school!, pack!, {
        preview_id: preview.preview_id,
        expected_etag: preview.base_etag,
      });
      setPublishState((prev) => publishReducer(prev, { type: "resolved", operation }));
      setPollResult({ kind: "polling" });

      const final = await pollOperationUntilDone(
        operation.operation_id,
        (id) => client.getOperation(id),
        (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
        { intervalMs: PUBLISH_POLL_INTERVAL_MS, maxAttempts: PUBLISH_POLL_MAX_ATTEMPTS },
      );

      if (final.status === "completed") {
        setPollResult({ kind: "completed", operation: final });
        onPublished(writeKeys);
        onReload();
        setStage({ kind: "idle" });
      } else if (final.status === "failed") {
        setPollResult({ kind: "failed", operation: final });
      } else {
        // "unknown" — never rendered as success and never silently retried.
        setPollResult({ kind: "unknown" });
      }
    } catch (err) {
      setPublishState(publishReducer({ status: "idle" }, { type: "rejected", error: toErrorShape(err) }));
      setPollResult({ kind: "idle" });
    }
  }

  const isBusy = publishState.status === "publishing" || pollResult.kind === "polling";

  return (
    <section className="publish-panel" aria-label={strings.actions.publish}>
      {stage.kind === "idle" && (
        <>
          <p className="qa-muted">{hasPending ? strings.footer.draftNote : strings.footer.idleNote}</p>
          <button type="button" disabled={!hasPending} onClick={handlePreview}>
            {strings.actions.publish}
          </button>
        </>
      )}

      {stage.kind === "previewing" && <p role="status">{strings.operation.validating}</p>}

      {stage.kind === "ready" && (
        <div className="publish-preview">
          <h3>{strings.publish.previewHeading}</h3>
          <DiffSummary diff={stage.preview.diff} />
          {isBusy ? (
            <p role="status">{strings.operation.processing}</p>
          ) : (
            <div className="publish-preview-actions">
              <button type="button" onClick={handleConfirmPublish}>
                {strings.actions.confirm}
              </button>
              <button type="button" onClick={handleCancelPreview}>
                {strings.actions.cancel}
              </button>
            </div>
          )}
        </div>
      )}

      {publishState.status === "conflict" && (
        <div role="alert" className="publish-conflict">
          <h3>{strings.conflict.title}</h3>
          <p>{strings.conflict.body}</p>
          <button type="button" onClick={handleReloadAndCompare}>
            {strings.conflict.reload}
          </button>
        </div>
      )}

      {publishState.status === "metadata_divergent" && (
        <div role="alert">
          <h3>{strings.metadataDivergent.title}</h3>
          <p>{strings.metadataDivergent.body}</p>
        </div>
      )}

      {publishState.status === "uploader_unavailable" && (
        <div role="alert">
          <h3>{strings.uploaderUnavailable.title}</h3>
          <p>{strings.uploaderUnavailable.body}</p>
        </div>
      )}

      {publishState.status === "failed" && (
        <div role="alert">
          <p>
            {strings.status.error}: {publishState.error.message}
          </p>
        </div>
      )}

      {pollResult.kind === "failed" && (
        <div role="alert">
          <p>
            {strings.operation.failed}
            {pollResult.operation.error?.message ? `: ${pollResult.operation.error.message}` : ""}
          </p>
        </div>
      )}

      {pollResult.kind === "unknown" && <p role="status">{strings.operation.unknown}</p>}

      {pollResult.kind === "completed" && <p role="status">{strings.operation.completed}</p>}
    </section>
  );
}
