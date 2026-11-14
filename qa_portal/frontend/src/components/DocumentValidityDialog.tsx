import { useState } from "react";
import type { ApiClient } from "../api/client";
import type { DocumentOptions, DocumentSummary } from "../api/types";
import { strings } from "../strings.cs";
import { Dialog } from "./Dialog";
import {
  resolveValidity,
  validityDraftFromDates,
  ValidityFields,
  EMPTY_DOCUMENT_METADATA_DRAFT,
  type DocumentMetadataDraft,
} from "./DocumentMetadataFields";

type Props = {
  client: ApiClient;
  school: string;
  doc: DocumentSummary;
  options: DocumentOptions;
  onClose: () => void;
  /** Fired only after the server accepted the change. */
  onSaved: (doc: DocumentSummary) => void;
};

const t = strings.documentValidity;

/** "Upravit platnost": the one metadata edit that needs no new ingest (§14.8). */
export function DocumentValidityDialog({ client, school, doc, options, onClose, onSaved }: Props) {
  const [draft, setDraft] = useState<DocumentMetadataDraft>({
    ...EMPTY_DOCUMENT_METADATA_DRAFT,
    ...validityDraftFromDates(doc.valid_from, doc.valid_to, options),
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const resolved = resolveValidity(draft, options);
  const canSave = !saving && draft.validity !== "" && resolved.error === null;

  async function handleSave() {
    if (!canSave) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await client.updateDocumentValidity(school, doc.file_uuid, {
        valid_from: resolved.valid_from,
        valid_to: resolved.valid_to,
      });
      onSaved(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : strings.errors.generic);
      setSaving(false);
    }
  }

  const footer = (
    <div className="dialog-actions">
      <button type="button" className="btn btn--primary" disabled={!canSave} onClick={handleSave}>
        {saving ? t.saving : t.save}
      </button>
      <button type="button" className="btn btn--secondary" onClick={onClose}>
        {strings.actions.cancel}
      </button>
    </div>
  );

  return (
    <Dialog title={t.title} onClose={onClose} footer={footer}>
      <p>
        <strong>{t.documentLabel}</strong> <span className="doc-cell-title">{doc.title}</span>
      </p>
      <p className="qa-muted">{t.note}</p>
      <ValidityFields options={options} value={draft} onChange={setDraft} idPrefix="doc-validity" />
      {error && <p role="alert" className="import-errors">{error}</p>}
    </Dialog>
  );
}
