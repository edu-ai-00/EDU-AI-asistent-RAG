import { useRef, useState } from "react";
import type { ApiClient } from "../api/client";
import type { DocumentSummary } from "../api/types";
import { strings } from "../strings.cs";
import { Dialog } from "./Dialog";

type Props = {
  client: ApiClient;
  school: string;
  schoolName: string;
  documents: DocumentSummary[];
  onClose: () => void;
  /** Fired once after the whole run, even when some items failed. */
  onDone: () => void;
};

type ItemResult = { kind: "pending" } | { kind: "running" } | { kind: "done" } | { kind: "failed"; message: string };

const t = strings.documentBulk;

/** One confirmation for the whole selection, then the per-document delete
 *  endpoint is called one after another and every outcome is listed. */
export function DocumentBulkDeleteDialog({ client, school, schoolName, documents, onClose, onDone }: Props) {
  const [phase, setPhase] = useState<"confirm" | "running" | "finished">("confirm");
  const [results, setResults] = useState<Record<string, ItemResult>>({});
  const closedRef = useRef(false);

  async function handleConfirm() {
    setPhase("running");
    setResults(Object.fromEntries(documents.map((doc) => [doc.file_uuid, { kind: "pending" } as ItemResult])));
    for (const doc of documents) {
      if (closedRef.current) return;
      setResults((prev) => ({ ...prev, [doc.file_uuid]: { kind: "running" } }));
      let result: ItemResult;
      try {
        await client.deleteDocument(school, doc.file_uuid, { confirm_file_uuid: doc.file_uuid });
        result = { kind: "done" };
      } catch (err) {
        result = { kind: "failed", message: err instanceof Error ? err.message : strings.errors.generic };
      }
      setResults((prev) => ({ ...prev, [doc.file_uuid]: result }));
    }
    setPhase("finished");
    onDone();
  }

  function handleClose() {
    if (phase === "running") return;
    closedRef.current = true;
    onClose();
  }

  function stateText(r: ItemResult | undefined): string {
    if (!r || r.kind === "pending") return t.itemPending;
    if (r.kind === "running") return t.itemRunning;
    if (r.kind === "done") return t.itemDone;
    return t.itemFailed(r.message);
  }

  const failed = Object.values(results).filter((r) => r.kind === "failed").length;
  const footer =
    phase === "finished" ? (
      <div className="dialog-actions">
        <button type="button" className="btn btn--secondary" onClick={handleClose}>
          {t.close}
        </button>
      </div>
    ) : (
      <div className="dialog-actions">
        <button type="button" className="btn btn--danger" disabled={phase === "running"} onClick={handleConfirm}>
          {phase === "running" ? t.deleting : t.deleteConfirm(documents.length)}
        </button>
        <button type="button" className="btn btn--secondary" disabled={phase === "running"} onClick={handleClose}>
          {strings.actions.cancel}
        </button>
      </div>
    );

  return (
    <Dialog title={t.deleteTitle} onClose={handleClose} footer={footer}>
      <p>
        <strong>{strings.documentDelete.schoolLabel}</strong> {schoolName} ({school})
      </p>
      <p>{t.deleteIntro(documents.length)}</p>
      <ul className="doc-field-errors" aria-label={t.deleteTitle}>
        {documents.map((doc) => (
          <li key={doc.file_uuid}>
            <span className="doc-cell-title">{doc.title}</span>
            {phase !== "confirm" && <span className="qa-muted">{` – ${stateText(results[doc.file_uuid])}`}</span>}
          </li>
        ))}
      </ul>
      {phase === "confirm" && (
        <p role="alert" className="import-errors">
          {t.deleteWarning}
        </p>
      )}
      {phase === "finished" && (
        <p role="status">{t.resultOk(documents.length - failed, documents.length)}</p>
      )}
    </Dialog>
  );
}
