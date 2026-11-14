import { useState } from "react";
import type { ApiClient } from "../api/client";
import type { DocumentDeleteResult, DocumentDetail } from "../api/types";
import { useKeyedResource } from "../hooks/useKeyedResource";
import { strings } from "../strings.cs";
import { Dialog } from "./Dialog";

type Props = {
  client: ApiClient;
  school: string;
  schoolName: string;
  fileUuid: string;
  onClose: () => void;
  /** Fired only after the server confirmed the delete. */
  onDeleted: () => void;
};

type State =
  | { kind: "idle" }
  | { kind: "deleting" }
  | { kind: "error"; message: string }
  | { kind: "done"; result: DocumentDeleteResult };

/** Permanent delete of a document with ALL its versions (CONTRACT.md §14.3).
 *  Loads the detail first so the admin sees exactly what disappears. */
export function DocumentDeleteDialog({ client, school, schoolName, fileUuid, onClose, onDeleted }: Props) {
  const s = strings.documentDelete;
  const [state, setState] = useState<State>({ kind: "idle" });
  const { state: detail } = useKeyedResource<DocumentDetail>(`${school}::${fileUuid}`, (_key, signal) =>
    client.getDocument(school, fileUuid, signal),
  );

  async function handleConfirm() {
    setState({ kind: "deleting" });
    try {
      const result = await client.deleteDocument(school, fileUuid, { confirm_file_uuid: fileUuid });
      setState({ kind: "done", result });
      onDeleted();
    } catch (err) {
      setState({ kind: "error", message: err instanceof Error ? err.message : strings.errors.generic });
    }
  }

  const ready = detail.status === "ready";
  const chunks = ready ? detail.value.versions.reduce((n, v) => n + v.chunk_count, 0) : 0;
  const footer =
    state.kind === "done" ? (
      <div className="dialog-actions">
        <button type="button" className="btn btn--secondary" onClick={onClose}>
          {strings.actions.close}
        </button>
      </div>
    ) : (
      <div className="dialog-actions">
        <button type="button" className="btn btn--danger" disabled={!ready || state.kind === "deleting"} onClick={handleConfirm}>
          {state.kind === "deleting" ? s.deleting : s.confirm}
        </button>
        <button type="button" className="btn btn--secondary" onClick={onClose}>
          {strings.actions.cancel}
        </button>
      </div>
    );

  return (
    <Dialog title={s.title} onClose={onClose} footer={footer}>
      <p>
        <strong>{s.schoolLabel}</strong> {schoolName} ({school})
      </p>
      {detail.status === "loading" && <p role="status">{s.loading}</p>}
      {detail.status === "error" && <p role="alert">{`${s.loadError} ${detail.message}`}</p>}
      {ready && (
        <>
          <p>
            <strong>{s.documentLabel}</strong> <span className="doc-cell-title">{detail.value.document.title}</span>
          </p>
          <p className="qa-muted">{s.scopeTemplate(detail.value.versions.length, chunks)}</p>
        </>
      )}

      {state.kind !== "done" && (
        <>
          <p role="alert" className="import-errors">
            {s.warning}
          </p>
          <p className="qa-muted">{s.alternative}</p>
        </>
      )}

      {state.kind === "error" && <p role="alert">{state.message}</p>}

      {state.kind === "done" && (
        <div className="export-ready" role="status">
          <strong>{s.doneHeading}</strong>
          <span>
            {s.resultTemplate(
              state.result.deleted.versions,
              state.result.deleted.chunks,
              state.result.deleted.elements,
              state.result.deleted.tables,
              state.result.files_removed,
            )}
          </span>
          {state.result.warnings.length > 0 && (
            <div>
              {s.warningsHeading}
              <ul>
                {state.result.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </Dialog>
  );
}
