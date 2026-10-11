import { useState } from "react";
import type { ApiClient } from "../api/client";
import { ApiError, type Category } from "../api/types";
import type { AsyncState } from "../hooks/useKeyedResource";
import { strings } from "../strings.cs";
import { Dialog } from "./Dialog";

type LangRow = { lang: string; label: string };

type Props = {
  client: ApiClient;
  /** The catalog as the caller already has it loaded — used only to decide
   *  whether to show the "catalog is empty" explanation; this dialog never
   *  re-fetches on its own (CONTRACT.md §4: the caller owns the list). */
  categoriesState: AsyncState<Category[]> | { status: "empty" };
  uiDefaultLang: string;
  onClose: () => void;
  /** Fired once the server actually created the category. The caller is
   *  responsible for refreshing the catalog and selecting the new code —
   *  this dialog never assumes success beyond what the server returned. */
  onCreated: (category: Category) => void;
};

const CODE_PATTERN = /^[A-Z0-9_]+$/;

type State = { kind: "idle" } | { kind: "creating" } | { kind: "error"; message: string };

/** CONTRACT.md §4.3: categories are a GLOBAL catalog created explicitly by an
 *  administrator — never backfilled from the legacy `tags.category` text and
 *  never invented client-side. This dialog only calls the existing
 *  listCategories/createCategory API; it does not touch category_siblings
 *  or any per-entry assignment. */
export function CategoryManager({ client, categoriesState, uiDefaultLang, onClose, onCreated }: Props) {
  const [code, setCode] = useState("");
  const [notes, setNotes] = useState("");
  const [rows, setRows] = useState<LangRow[]>([{ lang: uiDefaultLang, label: "" }]);
  const [state, setState] = useState<State>({ kind: "idle" });

  const isEmptyCatalog = categoriesState.status === "ready" && categoriesState.value.length === 0;

  const trimmedCode = code.trim().toUpperCase();
  const isValidCode = CODE_PATTERN.test(trimmedCode);
  const filledRows = rows.filter((row) => row.lang.trim() && row.label.trim());
  const canCreate = isValidCode && filledRows.length > 0 && state.kind !== "creating";

  function updateRow(index: number, field: keyof LangRow, value: string) {
    setRows((prev) => prev.map((row, i) => (i === index ? { ...row, [field]: value } : row)));
  }

  function addRow() {
    setRows((prev) => [...prev, { lang: "", label: "" }]);
  }

  function removeRow(index: number) {
    setRows((prev) => prev.filter((_, i) => i !== index));
  }

  async function handleCreate() {
    if (!canCreate) return;
    const labels: Record<string, string> = {};
    for (const row of filledRows) labels[row.lang.trim()] = row.label.trim();
    setState({ kind: "creating" });
    try {
      const created = await client.createCategory({
        code: trimmedCode,
        labels,
        notes: notes.trim() || undefined,
      });
      onCreated(created);
    } catch (err) {
      if (err instanceof ApiError) {
        setState({ kind: "error", message: err.message });
      } else {
        setState({ kind: "error", message: err instanceof Error ? err.message : strings.errors.generic });
      }
    }
  }

  return (
    <Dialog title={strings.newCategory.title} onClose={onClose}>
      {isEmptyCatalog && <p className="qa-muted">{strings.newCategory.emptyExplanation}</p>}

      <label className="field">
        <span>{strings.newCategory.code}</span>
        <input id="new-category-code" type="text" value={code} onChange={(event) => setCode(event.target.value)} />
      </label>
      {/* Outside the label: inside it, the hint becomes part of the field's
          accessible name and screen readers read the whole rule as the name. */}
      <span className="qa-muted">{strings.newCategory.codeHint}</span>

      <fieldset className="field">
        <legend>{strings.newCategory.translationsHeading}</legend>
        {rows.map((row, index) => (
          <div key={index} className="source-row">
            <label className="field">
              <span>{strings.newCategory.langLabel}</span>
              <input
                type="text"
                placeholder={strings.newCategory.langPlaceholder}
                value={row.lang}
                onChange={(event) => updateRow(index, "lang", event.target.value)}
              />
            </label>
            <label className="field">
              <span>{strings.newCategory.textLabel}</span>
              <input
                type="text"
                placeholder={strings.newCategory.textPlaceholder}
                value={row.label}
                onChange={(event) => updateRow(index, "label", event.target.value)}
              />
            </label>
            <button type="button" onClick={() => removeRow(index)} aria-label={strings.newCategory.removeLanguage}>
              {strings.newCategory.removeLanguage}
            </button>
          </div>
        ))}
        <button type="button" onClick={addRow}>
          {strings.newCategory.addLanguage}
        </button>
      </fieldset>

      <label className="field">
        <span>{strings.newCategory.notes}</span>
        <input type="text" value={notes} onChange={(event) => setNotes(event.target.value)} />
      </label>

      {state.kind === "error" && <p role="alert">{state.message}</p>}

      <div className="dialog-actions">
        <button type="button" disabled={!canCreate} onClick={handleCreate}>
          {state.kind === "creating" ? strings.newCategory.creating : strings.newCategory.create}
        </button>
        <button type="button" onClick={onClose}>
          {strings.actions.cancel}
        </button>
      </div>
    </Dialog>
  );
}
