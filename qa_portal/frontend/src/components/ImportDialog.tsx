import { useEffect, useState } from "react";
import type { ApiClient } from "../api/client";
import {
  ApiError,
  type ApiErrorShape,
  type Diff,
  type ImportPackResult,
  type ImportPreview,
  type MetadataOptions,
  type Operation,
  type Pack,
  type PackMetadata,
} from "../api/types";
import { DEFAULT_UI_LANG, PUBLISH_POLL_INTERVAL_MS, PUBLISH_POLL_MAX_ATTEMPTS } from "../config";
import { useKeyedResource } from "../hooks/useKeyedResource";
import { pollOperationUntilDone } from "../lib/publish";
import { strings } from "../strings.cs";
import { Dialog } from "./Dialog";
import { DiffSummary } from "./DiffSummary";
import { EMPTY_PACK_METADATA_DRAFT, PackMetadataFields, type PackMetadataDraft } from "./PackMetadataFields";

type Props = {
  client: ApiClient;
  school: string;
  packs: Pack[];
  initialPackId: string | null;
  onClose: () => void;
  onImported: () => void;
};

type PreviewStage =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "ready"; preview: ImportPreview }
  | { kind: "invalid"; fields: NonNullable<ApiErrorShape["fields"]> }
  | { kind: "error"; message: string };

type PublishStage =
  | { kind: "idle" }
  | { kind: "publishing" }
  | { kind: "polling" }
  | { kind: "completed"; operation: Operation }
  | { kind: "failed"; operation: Operation }
  | { kind: "unknown" }
  | { kind: "error"; message: string };

/** Defense in depth for CONTRACT.md §6: a failed pack must never read as
 *  success even if the top-level status were somehow inconsistent. */
function anyPackFailed(operation: Operation): boolean {
  return (operation.packs ?? []).some((p) => p.status === "failed");
}

function packStatusLabel(status: "pending" | "ok" | "failed" | "skipped"): string {
  switch (status) {
    case "ok":
      return strings.importDialog.ok;
    case "failed":
      return strings.importDialog.failed;
    case "skipped":
      return strings.importDialog.skipped;
    default:
      return strings.importDialog.pending;
  }
}

/** PACK_METADATA_REQUIRED is the one code the server sends as a bare pack
 *  message today; render it as a readable sentence instead of a raw code. */
function packMessageText(message?: string): string | undefined {
  if (!message) return undefined;
  if (message === "PACK_METADATA_REQUIRED") return strings.importDialog.packMetadataRequiredMessage;
  return message;
}

function rowCountOf(pack: ImportPackResult): number {
  return pack.row_count ?? pack.diff.added.length + pack.diff.changed.length + pack.diff.unchanged.length;
}

function diffIsAllZero(diff: Diff): boolean {
  return diff.added.length === 0 && diff.changed.length === 0 && diff.deactivated.length === 0;
}

/** The line a preview always needs above its diff counts (F-C4a-1): a file
 *  with rows that changed nothing must never read the same as an empty or
 *  failed upload, and a genuinely empty file (row_count 0) must read
 *  differently again from both. */
function rowCountMessage(pack: ImportPackResult): string {
  const rowCount = rowCountOf(pack);
  if (rowCount === 0) return strings.importDialog.emptyFile;
  if (diffIsAllZero(pack.diff)) return strings.importDialog.noRowsChangeTemplate(rowCount);
  return strings.importDialog.rowCountTemplate(rowCount);
}

function toMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  return err instanceof Error ? err.message : strings.errors.generic;
}

/**
 * File (CSV or ZIP) -> previewImport -> render per-pack preview -> explicit
 * publish -> poll exactly like PublishPanel (same pollOperationUntilDone,
 * same config intervals). Never invents a "completed" state: the only path
 * there is operation.status === "completed" from the poll.
 */
export function ImportDialog({ client, school, packs, initialPackId, onClose, onImported }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [packId, setPackId] = useState(initialPackId ?? packs[0]?.pack_id ?? "");
  const [mode, setMode] = useState<"merge" | "replace">("merge");
  const [confirmReplace, setConfirmReplace] = useState(false);
  const [previewStage, setPreviewStage] = useState<PreviewStage>({ kind: "idle" });
  const [publishStage, setPublishStage] = useState<PublishStage>({ kind: "idle" });
  // CONTRACT.md §2.4c: one draft per NEW pack in this import that the server
  // says still needs confirmed metadata, keyed by pack_id so a ZIP's packs
  // can never swap metadata with each other.
  const [metadataDrafts, setMetadataDrafts] = useState<Record<string, PackMetadataDraft>>({});
  const [confirmMetadataStage, setConfirmMetadataStage] = useState<{ kind: "idle" } | { kind: "confirming" }>({ kind: "idle" });

  const isZip = file !== null && file.name.toLowerCase().endsWith(".zip");
  const isBusy = publishStage.kind === "publishing" || publishStage.kind === "polling";

  function handleFileChange(nextFile: File | null) {
    setFile(nextFile);
    setPreviewStage({ kind: "idle" });
    setPublishStage({ kind: "idle" });
    setConfirmReplace(false);
    setMetadataDrafts({});
    setConfirmMetadataStage({ kind: "idle" });
  }

  // Seed a draft (prefilled from suggested_metadata when the server echoed
  // one, still just a suggestion — CONTRACT.md §2.4c) the first time a pack
  // shows up needing metadata; never overwrites what the admin already typed.
  useEffect(() => {
    if (previewStage.kind !== "ready") return;
    setMetadataDrafts((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const pack of previewStage.preview.packs) {
        if (pack.ok && pack.metadata_required && !(pack.pack_id in next)) {
          const suggested = pack.suggested_metadata;
          next[pack.pack_id] = suggested
            ? {
                visibility: suggested.visibility,
                audience_roles: [...suggested.audience_roles],
                school_grade_codes: [...suggested.school_grade_codes],
              }
            : EMPTY_PACK_METADATA_DRAFT;
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [previewStage]);

  async function runPreview(packMetadataByPack?: Record<string, PackMetadata>) {
    if (!file) return;
    try {
      // Regression guard (CONTRACT.md §2.4c): an import into an existing
      // pack must never even carry this key, so it is omitted entirely
      // rather than sent as `undefined`.
      const preview = await client.previewImport(school, file, {
        pack_id: isZip ? undefined : packId,
        mode,
        ...(packMetadataByPack ? { pack_metadata_by_pack: packMetadataByPack } : {}),
      });
      setPreviewStage({ kind: "ready", preview });
    } catch (err) {
      if (err instanceof ApiError && err.status === 422 && err.fields) {
        setPreviewStage({ kind: "invalid", fields: err.fields });
      } else {
        setPreviewStage({ kind: "error", message: toMessage(err) });
      }
    }
  }

  async function handleCheck() {
    if (!file) return;
    setPreviewStage({ kind: "checking" });
    setPublishStage({ kind: "idle" });
    await runPreview();
  }

  const okPacks: ImportPackResult[] =
    previewStage.kind === "ready" ? previewStage.preview.packs.filter((p) => p.ok) : [];
  // Packs the server says are new AND still lack confirmed metadata: not
  // publishable yet even though the row-level diff is otherwise ok.
  const pendingMetadataPacks: ImportPackResult[] =
    previewStage.kind === "ready" ? previewStage.preview.packs.filter((p) => p.ok && p.metadata_required) : [];
  const deactivatedTotal = okPacks.reduce((sum, p) => sum + p.diff.deactivated.length, 0);
  const canPublish =
    previewStage.kind === "ready" &&
    okPacks.length > 0 &&
    pendingMetadataPacks.length === 0 &&
    (mode !== "replace" || confirmReplace);

  const needsMetadataOptions = pendingMetadataPacks.length > 0;
  const { state: metadataOptionsState } = useKeyedResource<MetadataOptions>(
    needsMetadataOptions ? `${school}::${DEFAULT_UI_LANG}` : null,
    (_key, signal) => client.getMetadataOptions(school, DEFAULT_UI_LANG, signal),
  );

  const canConfirmMetadata =
    pendingMetadataPacks.length > 0 &&
    pendingMetadataPacks.every((p) => {
      const draft = metadataDrafts[p.pack_id];
      return !!draft && draft.visibility !== "" && draft.audience_roles.length > 0;
    });

  async function handleConfirmMetadata() {
    if (previewStage.kind !== "ready" || !canConfirmMetadata) return;
    const packMetadataByPack: Record<string, PackMetadata> = {};
    for (const pack of pendingMetadataPacks) {
      const draft = metadataDrafts[pack.pack_id];
      if (!draft || draft.visibility === "") continue;
      packMetadataByPack[pack.pack_id] = {
        visibility: draft.visibility,
        audience_roles: draft.audience_roles,
        school_grade_codes: draft.school_grade_codes,
      };
    }
    setConfirmMetadataStage({ kind: "confirming" });
    setPreviewStage({ kind: "checking" });
    await runPreview(packMetadataByPack);
    setConfirmMetadataStage({ kind: "idle" });
  }

  async function handlePublish() {
    if (previewStage.kind !== "ready" || !canPublish) return;
    const preview = previewStage.preview;
    const expectedEtags = Object.fromEntries(okPacks.map((p) => [p.pack_id, p.base_etag]));
    setPublishStage({ kind: "publishing" });
    try {
      const operation = await client.publishImport(school, preview.preview_id, {
        expected_etags: expectedEtags,
        confirm_replace: mode === "replace" ? true : undefined,
      });
      setPublishStage({ kind: "polling" });
      const final = await pollOperationUntilDone(
        operation.operation_id,
        (id) => client.getOperation(id),
        (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
        { intervalMs: PUBLISH_POLL_INTERVAL_MS, maxAttempts: PUBLISH_POLL_MAX_ATTEMPTS },
      );
      if (final.status === "completed") {
        setPublishStage({ kind: "completed", operation: final });
        onImported();
      } else if (final.status === "failed") {
        setPublishStage({ kind: "failed", operation: final });
      } else {
        setPublishStage({ kind: "unknown" });
      }
    } catch (err) {
      setPublishStage({ kind: "error", message: toMessage(err) });
    }
  }

  return (
    <Dialog title={strings.importDialog.title} onClose={onClose}>
      <label className="field">
        <span>{strings.importDialog.fileLabel}</span>
        <input
          type="file"
          accept=".csv,.zip"
          disabled={isBusy}
          onChange={(event) => handleFileChange(event.target.files?.[0] ?? null)}
        />
      </label>

      {!isZip && (
        <label className="field">
          <span>{strings.importDialog.packIdLabel}</span>
          <input type="text" value={packId} onChange={(event) => setPackId(event.target.value)} disabled={isBusy} />
        </label>
      )}

      <fieldset className="field">
        <legend>{strings.importDialog.modeLabel}</legend>
        <label className="checkbox-row">
          <input type="radio" name="import-mode" checked={mode === "merge"} disabled={isBusy} onChange={() => setMode("merge")} />
          <span>{strings.importDialog.modeMerge}</span>
        </label>
        <p className="qa-muted">{strings.importDialog.modeMergeHint}</p>
        <label className="checkbox-row">
          <input type="radio" name="import-mode" checked={mode === "replace"} disabled={isBusy} onChange={() => setMode("replace")} />
          <span>{strings.importDialog.modeReplace}</span>
        </label>
        <p className="qa-muted">{strings.importDialog.modeReplaceHint}</p>
      </fieldset>

      <div className="dialog-actions">
        <button type="button" disabled={!file || previewStage.kind === "checking" || isBusy} onClick={handleCheck}>
          {previewStage.kind === "checking" ? strings.importDialog.checking : strings.importDialog.check}
        </button>
      </div>

      {!file && <p className="qa-muted">{strings.importDialog.noFile}</p>}

      {previewStage.kind === "invalid" && (
        <div role="alert" className="import-invalid">
          <h4>{strings.importDialog.invalidHeading}</h4>
          <p>{strings.importDialog.invalidHint}</p>
          <ul>
            {previewStage.fields.map((f, i) => (
              <li key={i}>
                {[f.file, f.row != null ? `řádek ${f.row}` : null, f.field].filter(Boolean).join(" · ")}: {f.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      {previewStage.kind === "error" && <p role="alert">{previewStage.message}</p>}

      {previewStage.kind === "ready" && (
        <div className="import-preview">
          {previewStage.preview.packs.map((pack) => {
            return (
              <section key={pack.pack_id} className="import-pack-result">
                <h4>
                  {pack.pack_id}
                  {pack.is_new_pack ? ` · ${strings.newPack.title}` : ""}
                </h4>
                <p className="qa-muted">{pack.file}</p>
                <p>{rowCountMessage(pack)}</p>
                <DiffSummary diff={pack.diff} />

                {pack.ok ? (
                  <p role="status">{strings.importDialog.ok}</p>
                ) : (
                  <div role="alert">
                    <p>{strings.importDialog.failed}</p>
                    <p className="qa-muted">{strings.importDialog.retryHint}</p>
                  </div>
                )}

                {pack.errors && pack.errors.length > 0 && (
                  <div className="import-errors">
                    <h5>{strings.importDialog.errorsHeading}</h5>
                    <ul>
                      {pack.errors.map((error, index) => (
                        <li key={index}>
                          {[error.file, error.row != null ? `řádek ${error.row}` : null, error.field].filter(Boolean).join(" · ")}:{" "}
                          {error.message}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {pack.legacy_cat_unresolved && pack.legacy_cat_unresolved.length > 0 && (
                  <div className="import-legacy-cat">
                    <h5>{strings.importDialog.legacyCatHeading}</h5>
                    <ul>
                      {pack.legacy_cat_unresolved.map((item) => (
                        <li key={item.entry_key || item.cat}>
                          {item.entry_key ? `${item.entry_key} — ` : ""}
                          {item.cat}
                        </li>
                      ))}
                    </ul>
                    <p className="qa-muted">{strings.importDialog.legacyCatHint}</p>
                  </div>
                )}
              </section>
            );
          })}

          {pendingMetadataPacks.length > 0 && (
            <div className="import-metadata-required">
              {metadataOptionsState.status === "loading" && <p role="status">{strings.newPack.optionsLoading}</p>}
              {metadataOptionsState.status === "error" && <p role="alert">{strings.newPack.optionsError}</p>}
              {metadataOptionsState.status === "ready" &&
                pendingMetadataPacks.map((pack) => (
                  <section key={pack.pack_id} className="import-pack-metadata">
                    <h4>{strings.importDialog.metadataRequiredHeadingTemplate(pack.pack_id)}</h4>
                    <p className="qa-muted">{strings.importDialog.metadataRequiredHint}</p>
                    {pack.suggested_metadata && <p className="qa-muted">{strings.importDialog.metadataSuggestedNote}</p>}
                    <PackMetadataFields
                      options={metadataOptionsState.value}
                      value={metadataDrafts[pack.pack_id] ?? EMPTY_PACK_METADATA_DRAFT}
                      onChange={(next) => setMetadataDrafts((prev) => ({ ...prev, [pack.pack_id]: next }))}
                      idPrefix={`import-${pack.pack_id}`}
                      uiDefaultLang={DEFAULT_UI_LANG}
                    />
                  </section>
                ))}
              <div className="dialog-actions">
                <button type="button" disabled={!canConfirmMetadata || confirmMetadataStage.kind === "confirming"} onClick={handleConfirmMetadata}>
                  {confirmMetadataStage.kind === "confirming" ? strings.importDialog.confirmingMetadata : strings.importDialog.confirmMetadata}
                </button>
              </div>
            </div>
          )}

          {mode === "replace" && (
            <div className="import-replace-confirm">
              <p>{strings.importDialog.replaceDeactivateCountTemplate(deactivatedTotal)}</p>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={confirmReplace}
                  disabled={isBusy}
                  onChange={(event) => setConfirmReplace(event.target.checked)}
                />
                <span>{strings.importDialog.confirmReplace}</span>
              </label>
            </div>
          )}

          {okPacks.length === 0 && <p role="alert">{strings.importDialog.nothingPublishable}</p>}

          {publishStage.kind === "idle" && (
            <div className="dialog-actions">
              <button type="button" disabled={!canPublish} onClick={handlePublish}>
                {strings.importDialog.publishSelected}
              </button>
              <button type="button" onClick={onClose}>
                {strings.actions.cancel}
              </button>
            </div>
          )}

          {publishStage.kind === "publishing" && <p role="status">{strings.operation.accepted}</p>}
          {publishStage.kind === "polling" && <p role="status">{strings.operation.processing}</p>}

          {(publishStage.kind === "completed" || publishStage.kind === "failed") &&
            (() => {
              const { operation } = publishStage;
              // CONTRACT.md §6: `packs[]` is authoritative — a failed pack
              // must flip the heading even if the top-level status did not.
              const failed = publishStage.kind === "failed" || anyPackFailed(operation);
              const failedCount = (operation.packs ?? []).filter((p) => p.status === "failed").length;
              return (
                <>
                  <p role={failed ? "alert" : "status"}>
                    {failed ? strings.importDialog.publishFailedHeading : strings.operation.completed}
                  </p>
                  {failed && operation.error?.message && <p className="qa-muted">{operation.error.message}</p>}
                  {failed && (operation.packs?.length ?? 0) > 0 && (
                    <p className="qa-muted">
                      {strings.importDialog.publishFailedSomePacksTemplate(failedCount, operation.packs!.length)}
                    </p>
                  )}
                  {operation.packs && operation.packs.length > 0 && (
                    <>
                      <h5>{strings.importDialog.perPackStatus}</h5>
                      <ul className="import-publish-packs">
                        {operation.packs.map((pack) => (
                          <li
                            key={pack.pack_id}
                            role={pack.status === "failed" ? "alert" : "status"}
                            className={`import-publish-pack${pack.status === "failed" ? " import-publish-pack-failed" : ""}`}
                          >
                            <strong>{pack.pack_id}</strong> — {packStatusLabel(pack.status)}
                            {packMessageText(pack.message) ? `: ${packMessageText(pack.message)}` : ""}
                          </li>
                        ))}
                      </ul>
                    </>
                  )}
                </>
              );
            })()}

          {publishStage.kind === "unknown" && <p role="status">{strings.operation.unknown}</p>}
          {publishStage.kind === "error" && <p role="alert">{publishStage.message}</p>}
        </div>
      )}
    </Dialog>
  );
}
