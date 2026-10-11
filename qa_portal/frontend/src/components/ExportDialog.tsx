import { useState } from "react";
import type { ApiClient } from "../api/client";
import { ApiError, type ExportResult, type Pack } from "../api/types";
import { DEFAULT_UI_LANG } from "../config";
import { strings } from "../strings.cs";
import { Dialog } from "./Dialog";

type Props = {
  client: ApiClient;
  school: string;
  packs: Pack[];
  initialPackId: string | null;
  onClose: () => void;
};

type State = { kind: "idle" } | { kind: "preparing" } | { kind: "ready"; result: ExportResult } | { kind: "error"; message: string };

/** Pack selection -> createExport -> download link. `format: "csv"` is only
 *  offered while exactly one pack is selected — picking a second pack while
 *  csv is active switches the format to zip rather than letting an invalid
 *  combination reach the server. Once an export is ready, the packs it
 *  actually contains are listed by name and entry count so the admin can
 *  check the contents before downloading, not only after. */
export function ExportDialog({ client, school, packs, initialPackId, onClose }: Props) {
  const [selected, setSelected] = useState<Set<string>>(new Set(initialPackId ? [initialPackId] : []));
  const [format, setFormat] = useState<"csv" | "zip">("csv");
  const [lang, setLang] = useState(DEFAULT_UI_LANG);
  const [state, setState] = useState<State>({ kind: "idle" });

  const selectedIds = Array.from(selected);
  const csvAllowed = selectedIds.length === 1;

  function toggle(packId: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(packId)) next.delete(packId);
      else next.add(packId);
      if (next.size !== 1 && format === "csv") setFormat("zip");
      return next;
    });
  }

  async function handlePrepare() {
    if (selectedIds.length === 0) return;
    setState({ kind: "preparing" });
    try {
      const result = await client.createExport(school, { pack_ids: selectedIds, format, lang });
      setState({ kind: "ready", result });
    } catch (err) {
      setState({ kind: "error", message: err instanceof ApiError ? err.message : strings.errors.generic });
    }
  }

  const selectedPacks = packs.filter((p) => selected.has(p.pack_id));

  return (
    <Dialog title={strings.exportDialog.title} onClose={onClose}>
      <fieldset className="field">
        <legend>{strings.exportDialog.packsHeading}</legend>
        {packs.map((pack) => (
          <label key={pack.pack_id} className="checkbox-row">
            <input type="checkbox" checked={selected.has(pack.pack_id)} onChange={() => toggle(pack.pack_id)} />
            <span>
              {pack.pack_id} · v{pack.version} · {strings.exportDialog.entryCountTemplate(pack.entry_count)}
            </span>
          </label>
        ))}
      </fieldset>

      <fieldset className="field">
        <legend>{strings.exportDialog.formatLabel}</legend>
        <label className="checkbox-row">
          <input type="radio" name="export-format" checked={format === "csv"} disabled={!csvAllowed} onChange={() => setFormat("csv")} />
          <span>{strings.exportDialog.formatCsv}</span>
        </label>
        <label className="checkbox-row">
          <input type="radio" name="export-format" checked={format === "zip"} onChange={() => setFormat("zip")} />
          <span>{strings.exportDialog.formatZip}</span>
        </label>
        {!csvAllowed && <p className="qa-muted">{strings.exportDialog.formatCsvSinglePackHint}</p>}
      </fieldset>

      <p className="qa-muted">{strings.exportDialog.note}</p>

      {state.kind === "error" && <p role="alert">{state.message}</p>}

      {state.kind !== "ready" && (
        <div className="dialog-actions">
          <button type="button" disabled={selectedIds.length === 0 || state.kind === "preparing"} onClick={handlePrepare}>
            {state.kind === "preparing" ? strings.exportDialog.preparing : strings.exportDialog.prepare}
          </button>
          <button type="button" onClick={onClose}>
            {strings.actions.cancel}
          </button>
        </div>
      )}

      {state.kind === "ready" && (
        <div className="export-ready">
          <p role="status">{strings.exportDialog.ready}</p>
          <h4>{strings.exportDialog.contentsHeading}</h4>
          <ul>
            {selectedPacks.map((pack) => (
              <li key={pack.pack_id}>
                {pack.pack_id} · {strings.exportDialog.entryCountTemplate(pack.entry_count)}
              </li>
            ))}
          </ul>
          {state.result.note && <p className="qa-muted">{state.result.note}</p>}
          <a href={client.downloadExportUrl(state.result.export_id)} download={state.result.filename}>
            {strings.actions.download}
          </a>
        </div>
      )}
    </Dialog>
  );
}
