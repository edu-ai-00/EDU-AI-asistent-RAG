import { useState } from "react";
import type { ApiClient } from "../api/client";
import { ApiError, type MetadataOptions, type PackMetadata } from "../api/types";
import { DEFAULT_UI_LANG } from "../config";
import { useKeyedResource } from "../hooks/useKeyedResource";
import { strings } from "../strings.cs";
import { Dialog } from "./Dialog";
import { EMPTY_PACK_METADATA_DRAFT, PackMetadataFields, type PackMetadataDraft } from "./PackMetadataFields";

type Props = {
  client: ApiClient;
  school: string;
  onClose: () => void;
  /** The pack has no server state yet (CONTRACT.md §2.4a): the caller gets
   *  back the confirmed metadata alongside the id so it can attach it to
   *  this pack's first preview call. */
  onCreated: (packId: string, metadata: PackMetadata) => void;
};

type State = { kind: "idle" } | { kind: "creating" } | { kind: "error"; message: string };

/** "+ Nový balíček…": a taken pack_id answers 409 and is reported here, never
 *  swallowed — the dialog stays open with the conflict message so the admin
 *  can pick a different id. Since a brand-new pack has no server state, this
 *  dialog is also where the administrator confirms visibility/audience_roles/
 *  grades (CONTRACT.md §2.4a) — nothing here defaults silently, and the
 *  create button stays disabled until at least one role is picked. */
export function NewPackDialog({ client, school, onClose, onCreated }: Props) {
  const [packId, setPackId] = useState("");
  const [lang, setLang] = useState(DEFAULT_UI_LANG);
  const [state, setState] = useState<State>({ kind: "idle" });

  const [draft, setDraft] = useState<PackMetadataDraft>(EMPTY_PACK_METADATA_DRAFT);

  const { state: optionsState } = useKeyedResource<MetadataOptions>(`${school}::${lang}`, (_key, signal) =>
    client.getMetadataOptions(school, lang, signal),
  );

  const { visibility } = draft;
  const trimmedId = packId.trim();
  const isValidId = /^[a-z0-9._-]+$/.test(trimmedId);
  const hasVisibility = visibility !== "";
  const hasRole = draft.audience_roles.length > 0;
  const canCreate = isValidId && hasVisibility && hasRole && state.kind !== "creating";

  async function handleCreate() {
    if (!canCreate || !hasVisibility) return;
    const metadata: PackMetadata = {
      visibility,
      audience_roles: draft.audience_roles,
      school_grade_codes: draft.school_grade_codes,
    };
    setState({ kind: "creating" });
    try {
      await client.createPack(school, { pack_id: trimmedId, lang, metadata });
      onCreated(trimmedId, metadata);
    } catch (err) {
      if (err instanceof ApiError && err.code === "PACK_ID_TAKEN") {
        setState({ kind: "error", message: strings.newPack.exists });
      } else if (err instanceof ApiError && err.code === "VALIDATION_ERROR") {
        const detail = (err.fields || []).map((f) => `${f.field}: ${f.message}`).join(" ");
        setState({ kind: "error", message: detail || err.message });
      } else {
        setState({ kind: "error", message: err instanceof Error ? err.message : strings.errors.generic });
      }
    }
  }

  const actions = (
    <div className="dialog-actions">
      <button type="button" disabled={!canCreate} onClick={handleCreate}>
        {strings.newPack.create}
      </button>
      <button type="button" onClick={onClose}>
        {strings.actions.cancel}
      </button>
    </div>
  );

  return (
    <Dialog title={strings.newPack.title} onClose={onClose} footer={actions}>
      <div className="field">
        <label htmlFor="new-pack-id">{strings.newPack.packId}</label>
        <input id="new-pack-id" type="text" value={packId} onChange={(event) => setPackId(event.target.value)} />
        <span className="qa-muted">{strings.newPack.packIdHint}</span>
      </div>

      <label className="field">
        <span>{strings.newPack.lang}</span>
        <input type="text" value={lang} onChange={(event) => setLang(event.target.value)} />
      </label>

      <h3>{strings.newPack.metadataHeading}</h3>
      <p className="qa-muted">{strings.newPack.metadataHint}</p>

      {optionsState.status === "loading" && <p role="status">{strings.newPack.optionsLoading}</p>}
      {optionsState.status === "error" && <p role="alert">{strings.newPack.optionsError}</p>}

      {optionsState.status === "ready" && (
        <PackMetadataFields options={optionsState.value} value={draft} onChange={setDraft} idPrefix="new-pack" uiDefaultLang={lang} />
      )}

      {state.kind === "error" && <p role="alert">{state.message}</p>}
    </Dialog>
  );
}
