import { useState } from "react";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/types";
import { strings } from "../strings.cs";
import { Dialog } from "./Dialog";

type EntryTarget = {
  kind: "entry";
  school: string;
  pack: string;
  entryKey: string;
  /** The question text, shown so the admin knows exactly what is gone. */
  name: string;
};

type PackTarget = {
  kind: "pack";
  school: string;
  pack: string;
  /** Number of questions currently in the pack (CONTRACT.md §13.2 scope). */
  entryCount: number;
};

type Props = {
  client: ApiClient;
  target: EntryTarget | PackTarget;
  onClose: () => void;
  /** Fired only once the server actually deleted the row(s) — never on a
   *  merely-attempted delete (CONTRACT.md §13: no fake success). */
  onDeleted: () => void;
};

type State = { kind: "idle" } | { kind: "deleting" } | { kind: "error"; message: string };

/** Permanent delete (CONTRACT.md §13) — irreversible, no undo, drops
 *  variants/embeddings and (for a whole pack) its manifests along with the
 *  row(s). Shared shell for both scopes so the confirmation rules (typed
 *  pack id, disabled button until it matches) live in exactly one place. */
export function DeleteDialog({ client, target, onClose, onDeleted }: Props) {
  const [typedId, setTypedId] = useState("");
  const [state, setState] = useState<State>({ kind: "idle" });

  const isPack = target.kind === "pack";
  const idMatches = !isPack || typedId === target.pack;
  const canConfirm = idMatches && state.kind !== "deleting";

  async function handleConfirm() {
    if (!canConfirm) return;
    setState({ kind: "deleting" });
    try {
      if (target.kind === "entry") {
        await client.deleteEntry(target.school, target.pack, target.entryKey);
      } else {
        await client.deletePack(target.school, target.pack, { confirm_pack_id: typedId });
      }
      onDeleted();
    } catch (err) {
      if (err instanceof ApiError) {
        setState({ kind: "error", message: err.message });
      } else {
        setState({ kind: "error", message: err instanceof Error ? err.message : strings.errors.generic });
      }
    }
  }

  return (
    <Dialog title={strings.deletePermanent.title} onClose={onClose}>
      <p>
        <strong>{strings.deletePermanent.schoolLabel}</strong> {target.school}
      </p>
      <p>
        <strong>{target.kind === "entry" ? strings.deletePermanent.questionLabel : strings.deletePermanent.packLabel}</strong>{" "}
        {target.kind === "entry" ? target.name : target.pack}
      </p>
      {target.kind === "pack" && (
        <p className="qa-muted">{strings.deletePermanent.packScopeTemplate(target.entryCount)}</p>
      )}

      <p role="alert" className="import-errors">
        {target.kind === "pack" ? strings.deletePermanent.warningPack : strings.deletePermanent.warningEntry}
      </p>

      {target.kind === "pack" && (
        <label className="field">
          <span>{strings.deletePermanent.typeToConfirmTemplate(target.pack)}</span>
          <input
            type="text"
            value={typedId}
            onChange={(event) => setTypedId(event.target.value)}
            aria-invalid={typedId.length > 0 && !idMatches}
          />
        </label>
      )}

      {state.kind === "error" && <p role="alert">{state.message}</p>}

      <div className="dialog-actions">
        <button type="button" disabled={!canConfirm} onClick={handleConfirm}>
          {state.kind === "deleting" ? strings.deletePermanent.deleting : strings.deletePermanent.confirm}
        </button>
        <button type="button" onClick={onClose}>
          {strings.actions.cancel}
        </button>
      </div>
    </Dialog>
  );
}
