import type { Domain, Entry, EntryPage } from "../api/types";
import type { AsyncState } from "../hooks/useKeyedResource";
import { strings } from "../strings.cs";

type Props = {
  state: AsyncState<EntryPage> | { status: "empty" };
  query: string;
  onQueryChange: (value: string) => void;
  selectedEntryKey: string | null;
  onSelect: (entryKey: string) => void;
  pendingNewEntries: Array<{ key: string; canonical_q: string }>;
  onAddQuestion: () => void;
  /** School's domain catalog, used only to show the human label instead of
   *  the stable code. Entries keep carrying codes; nothing is reassigned. */
  domains: Domain[];
};

/** Left-hand pane: search box + the entries for the currently selected
 *  school/pack. Purely presentational — race-safety and the school/pack key
 *  live in useEntries (App.tsx), not here. `pendingNewEntries` are drafts
 *  opened via "+ Přidat otázku" that do not exist on the server yet — they
 *  have no entry_key, so they render from the draft map, never from a
 *  server-invented identifier. */
export function EntryList({ state, query, onQueryChange, selectedEntryKey, onSelect, pendingNewEntries, onAddQuestion, domains }: Props) {
  // code -> label from the loaded catalog; an unknown code still shows as the
  // code, so a stale catalog never hides what an entry is tagged with.
  const domainLabels = new Map(domains.map((d) => [d.code, d.label]));
  const areaText = (codes: string[]) =>
    codes.map((code) => domainLabels.get(code) ?? code).join(", ");
  return (
    <section className="entry-list" aria-label={strings.list.heading}>
      <h2>{strings.list.heading}</h2>
      <label className="field">
        <span>{strings.list.searchLabel}</span>
        <input
          type="search"
          value={query}
          placeholder={strings.list.searchPlaceholder}
          onChange={(event) => onQueryChange(event.target.value)}
        />
      </label>

      <button type="button" onClick={onAddQuestion}>
        {strings.list.addQuestion}
      </button>

      {pendingNewEntries.length > 0 && (
        <ul className="entry-list-items">
          {pendingNewEntries.map((item) => (
            <li key={item.key}>
              <button
                type="button"
                className={item.key === selectedEntryKey ? "entry-list-item is-selected" : "entry-list-item"}
                aria-pressed={item.key === selectedEntryKey}
                onClick={() => onSelect(item.key)}
              >
                <span className="entry-list-item-q">{item.canonical_q || strings.list.newQuestionLabel}</span>
                <span className="entry-list-item-meta">
                  <span className="badge badge-new">{strings.list.newQuestionLabel}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {state.status === "empty" && <p className="qa-muted">{strings.editor.selectQuestionHint}</p>}
      {state.status === "loading" && <p role="status">{strings.list.loading}</p>}
      {state.status === "error" && <p role="alert">{strings.list.loadError}</p>}
      {state.status === "ready" && (
        <>
          <p className="qa-muted">
            {strings.list.countTemplate(state.value.entries.length, state.value.total ?? state.value.entries.length)}
          </p>
          {state.value.entries.length === 0 ? (
            <p className="qa-muted">{strings.list.empty}</p>
          ) : (
            <ul className="entry-list-items">
              {state.value.entries.map((entry: Entry) => (
                <li key={entry.entry_key}>
                  <button
                    type="button"
                    className={entry.entry_key === selectedEntryKey ? "entry-list-item is-selected" : "entry-list-item"}
                    aria-pressed={entry.entry_key === selectedEntryKey}
                    onClick={() => onSelect(entry.entry_key)}
                  >
                    <span className="entry-list-item-q">{entry.canonical_q}</span>
                    <span className="entry-list-item-meta">
                      {!entry.is_active && <span className="badge badge-inactive">{strings.list.inactiveBadge}</span>}
                      <span className="qa-muted">
                        {entry.domains.length > 0 ? areaText(entry.domains) : strings.list.noAreas}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
