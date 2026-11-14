import { useEffect, useMemo, useRef, useState } from "react";
import type { Domain, Entry, EntryPage } from "../api/types";
import { ENTRIES_PAGE_SIZE } from "../config";
import type { AsyncState } from "../hooks/useKeyedResource";
import { filterEntries, pagerItems } from "../lib/entryList";
import { strings } from "../strings.cs";

type Props = {
  state: AsyncState<EntryPage> | { status: "empty" };
  query: string;
  onQueryChange: (value: string) => void;
  selectedEntryKey: string | null;
  onSelect: (entryKey: string) => void;
  pendingNewEntries: Array<{ key: string; canonical_q: string }>;
  /** School's domain catalog, used only to show the human label instead of
   *  the stable code. Entries keep carrying codes; nothing is reassigned. */
  domains: Domain[];
  /** Entries per page; the whole pack is held client-side (useEntries). */
  pageSize?: number;
};

/** Left pane: search, filter and the entries of the pack, 50 per page; only the list scrolls.
 *  Search and "Jen bez oblasti" run over the whole pack and reset the pager; the parent re-keys
 *  this on a school/pack switch. `pendingNewEntries` are unpublished drafts (no entry_key yet),
 *  untouched by filter and pager. */
export function EntryList({
  state,
  query,
  onQueryChange,
  selectedEntryKey,
  onSelect,
  pendingNewEntries,
  domains,
  pageSize = ENTRIES_PAGE_SIZE,
}: Props) {
  // code -> label from the loaded catalog; an unknown code still shows as the
  // code, so a stale catalog never hides what an entry is tagged with.
  const domainLabels = new Map(domains.map((d) => [d.code, d.label]));
  const areaLabel = (code: string) => domainLabels.get(code) ?? code;

  const [page, setPage] = useState(1);
  const [onlyNoAreas, setOnlyNoAreas] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const all = state.status === "ready" ? state.value.entries : null;
  const filtered = useMemo(() => (all ? filterEntries(all, query, onlyNoAreas) : []), [all, query, onlyNoAreas]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const current = Math.min(page, pageCount);
  const from = (current - 1) * pageSize;
  const visible = filtered.slice(from, from + pageSize);
  const filterActive = query.trim() !== "" || onlyNoAreas;

  // The selection may sit on another page (opened from the debug panel, or after a reload):
  // jump there once, when the selection or the loaded list changes – never on a page click.
  const filteredRef = useRef(filtered);
  filteredRef.current = filtered;
  const ready = state.status === "ready";
  useEffect(() => {
    if (!ready || selectedEntryKey === null) return;
    const index = filteredRef.current.findIndex((e) => e.entry_key === selectedEntryKey);
    if (index >= 0) setPage(Math.floor(index / pageSize) + 1);
  }, [selectedEntryKey, ready, all, pageSize]);

  const visibleSignature = visible.map((e) => e.entry_key).join("|");
  useEffect(() => {
    const el = scrollRef.current?.querySelector<HTMLElement>(".entry-list-item.is-selected");
    el?.scrollIntoView?.({ block: "nearest" });
  }, [selectedEntryKey, visibleSignature]);

  function changeQuery(value: string) {
    setPage(1);
    onQueryChange(value);
  }

  return (
    <section className="entry-list" aria-label={strings.list.heading}>
      <div className="entry-list-top">
        <div className="entry-list-head">
          <h2>{strings.list.heading}</h2>
          {all && (
            <p className="qa-muted">
              {filterActive ? strings.list.countFiltered(filtered.length, all.length) : strings.list.countAll(all.length)}
            </p>
          )}
        </div>
        <label className="field">
          <span>{strings.list.searchLabel}</span>
          <input
            type="search"
            value={query}
            placeholder={strings.list.searchPlaceholder}
            onChange={(event) => changeQuery(event.target.value)}
          />
        </label>

        <div className="entry-list-actions">
          <label className="checkbox-row entry-list-filter">
            <input
              type="checkbox"
              checked={onlyNoAreas}
              onChange={(event) => {
                setPage(1);
                setOnlyNoAreas(event.target.checked);
              }}
            />
            <span>{strings.list.onlyNoAreas}</span>
          </label>
        </div>
      </div>

      <div className="entry-list-scroll" ref={scrollRef} tabIndex={-1}>
        {pendingNewEntries.length > 0 && (
          <ul className="entry-list-items" aria-label={strings.list.draftsHeading}>
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
        {all &&
          (all.length === 0 ? (
            <p className="qa-muted">{strings.list.empty}</p>
          ) : filtered.length === 0 ? (
            <p className="qa-muted">{strings.list.noMatch}</p>
          ) : (
            <ul className="entry-list-items">
              {visible.map((entry: Entry) => (
                <li key={entry.entry_key}>
                  <button
                    type="button"
                    className={entry.entry_key === selectedEntryKey ? "entry-list-item is-selected" : "entry-list-item"}
                    aria-pressed={entry.entry_key === selectedEntryKey}
                    onClick={() => onSelect(entry.entry_key)}
                  >
                    <span className="entry-list-item-q">{entry.canonical_q}</span>
                    {(!entry.is_active || entry.domains.length > 0) && (
                      <span className="entry-list-item-meta">
                        {!entry.is_active && <span className="badge badge-inactive">{strings.list.inactiveBadge}</span>}
                        {entry.domains.map((code) => (
                          <span key={code} className="chip">
                            {areaLabel(code)}
                          </span>
                        ))}
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          ))}
      </div>

      {all && filtered.length > 0 && (
        <nav className="entry-pager" aria-label={strings.list.pagerLabel}>
          <span className="entry-pager-range qa-muted">{strings.list.pagerRange(from + 1, from + visible.length, filtered.length)}</span>
          {pageCount > 1 && (
            <ul className="entry-pager-pages">
              <li>
                <button
                  type="button"
                  className="btn btn--ghost btn--icon"
                  aria-label={strings.list.pagePrev}
                  disabled={current <= 1}
                  onClick={() => setPage(current - 1)}
                >
                  <span aria-hidden="true">‹</span>
                </button>
              </li>
              {pagerItems(current, pageCount).map((item, i) =>
                item === "gap" ? (
                  <li key={`gap-${i}`} aria-hidden="true" className="entry-pager-gap">
                    …
                  </li>
                ) : (
                  <li key={item}>
                    <button
                      type="button"
                      className={item === current ? "btn btn--secondary btn--icon is-current" : "btn btn--ghost btn--icon"}
                      aria-label={strings.list.pageGo(item)}
                      aria-current={item === current ? "page" : undefined}
                      onClick={() => setPage(item)}
                    >
                      {item}
                    </button>
                  </li>
                ),
              )}
              <li>
                <button
                  type="button"
                  className="btn btn--ghost btn--icon"
                  aria-label={strings.list.pageNext}
                  disabled={current >= pageCount}
                  onClick={() => setPage(current + 1)}
                >
                  <span aria-hidden="true">›</span>
                </button>
              </li>
            </ul>
          )}
        </nav>
      )}
    </section>
  );
}
