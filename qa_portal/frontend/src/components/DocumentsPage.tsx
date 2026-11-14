import { useEffect, useRef, useState } from "react";
import type { ApiClient } from "../api/client";
import type {
  DocumentFormatFilter,
  DocumentOptions,
  DocumentPage,
  DocumentStatusFilter,
  DocumentStats,
  DocumentSummary,
  DocumentValidityFilter,
  School,
  SchoolPermissions,
} from "../api/types";
import { DOCUMENTS_PAGE_SIZE, DOCUMENTS_SEARCH_DEBOUNCE_MS } from "../config";
import { useKeyedResource } from "../hooks/useKeyedResource";
import { formatDateTime, shortTitle } from "../lib/documentFormat";
import { codeLabel, formatChip, visibilityLabel } from "../lib/documentLabels";
import { downloadErrorMessage, saveStoredFile } from "../lib/download";
import { strings } from "../strings.cs";
import { DocumentBulkDeleteDialog } from "./DocumentBulkDeleteDialog";
import { DocumentDeleteDialog } from "./DocumentDeleteDialog";
import { DocumentDetailDrawer } from "./DocumentDetailDrawer";
import { DocumentJobsPanel } from "./DocumentJobsPanel";
import { DocumentUploadDialog } from "./DocumentUploadDialog";
import { DocumentValidityDialog } from "./DocumentValidityDialog";
import { OverflowMenu, type MenuItem } from "./OverflowMenu";
import { ValidityBadge } from "./ValidityBadge";

type Props = {
  client: ApiClient;
  school: School;
  uiDefaultLang: string;
  /** The ask drawer is open: the detail drawer yields the right edge to it. */
  askOpen?: boolean;
  /** The detail drawer is about to open: the app closes the ask drawer. */
  onOpenDetail?: () => void;
  /** The debug panel of "try a question" asks for the detail of this document (a version uuid). */
  openDocumentRequest?: { fileUuid: string; nonce: number } | null;
  /** The request was taken over, the app may forget it. */
  onDocumentRequestHandled?: () => void;
};

// §14.6: an older backend sends no permissions — treat as all allowed.
const ALL_ALLOWED: SchoolPermissions = { read: true, publish: true, ingest: true, deactivate: true, delete_permanently: true };

const t = strings.documents;
const FORMAT_FILTERS: DocumentFormatFilter[] = ["pdf", "docx", "pptx", "xlsx", "web"];
const b = strings.documentBulk;

type BulkResult = { ok: number; total: number; failures: Array<{ title: string; message: string }> };

/** Second portal page (spec.qa-portal-documents §8): the school's ingested
 *  documents with filters, bulk actions, a detail drawer and the upload jobs. */
export function DocumentsPage({ client, school, uiDefaultLang, askOpen = false, onOpenDetail, openDocumentRequest = null, onDocumentRequestHandled }: Props) {
  const perms = school.permissions ?? ALL_ALLOWED;
  const [status, setStatus] = useState<DocumentStatusFilter>("active");
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [roleFilter, setRoleFilter] = useState("");
  const [validityFilter, setValidityFilter] = useState<DocumentValidityFilter>("all");
  const [formatFilter, setFormatFilter] = useState<DocumentFormatFilter | "">("");
  const [more, setMore] = useState<{ key: string; documents: DocumentSummary[]; next_cursor: string | null } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [bulkProgress, setBulkProgress] = useState<{ done: number; total: number } | null>(null);
  const [bulkResult, setBulkResult] = useState<BulkResult | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [jobsRefresh, setJobsRefresh] = useState(0);
  const [deleteTarget, setDeleteTarget] = useState<DocumentSummary | null>(null);
  // A snapshot: the list reloads (and the selection clears) while the dialog still shows results.
  const [bulkDeleteDocs, setBulkDeleteDocs] = useState<DocumentSummary[] | null>(null);
  const [detailTarget, setDetailTarget] = useState<DocumentSummary | null>(null);
  const [versionTarget, setVersionTarget] = useState<DocumentSummary | null>(null);
  const [validityTarget, setValidityTarget] = useState<DocumentSummary | null>(null);
  const selectAllRef = useRef<HTMLInputElement>(null);
  const [stats, setStats] = useState<DocumentStats | null>(null);
  const [statsFailed, setStatsFailed] = useState(false);
  const [statsNonce, setStatsNonce] = useState(0);
  const aliveRef = useRef(true);
  const schoolRef = useRef(school.code);
  schoolRef.current = school.code;
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (askOpen) setDetailTarget(null);
  }, [askOpen]);

  // §14.12 overview tiles. The previous numbers stay while a reload runs, so the tiles do not flicker.
  useEffect(() => {
    setStats(null);
    setStatsFailed(false);
  }, [school.code]);
  useEffect(() => {
    if (!perms.read) return;
    const controller = new AbortController();
    // Promise.resolve().then also turns a client without the method into a plain failure.
    Promise.resolve()
      .then(() => client.getDocumentStats(school.code, controller.signal))
      .then((value) => {
        if (controller.signal.aborted) return;
        setStats(value);
        setStatsFailed(false);
      })
      .catch(() => {
        if (!controller.signal.aborted) setStatsFailed(true);
      });
    return () => controller.abort();
  }, [client, school.code, perms.read, statsNonce]);

  // The debug panel of "try a question" links to a document: open its detail drawer.
  useEffect(() => {
    if (!openDocumentRequest || !perms.read) return;
    const code = school.code;
    onDocumentRequestHandled?.();
    client
      .getDocument(code, openDocumentRequest.fileUuid)
      .then((detail) => {
        if (!aliveRef.current || schoolRef.current !== code) return;
        onOpenDetail?.();
        setDetailTarget(detail.document);
      })
      .catch((err) => {
        if (aliveRef.current && schoolRef.current === code) {
          setActionError(t.openFailed(err instanceof Error ? err.message : strings.errors.generic));
        }
      });
    // Keyed on the request only: it is one-shot, the other values are read at that moment.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openDocumentRequest]);

  useEffect(() => {
    const timer = setTimeout(() => setQuery(queryInput.trim()), DOCUMENTS_SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [queryInput]);

  // School is part of the key, so a late answer for the previous school is dropped.
  const listKey = perms.read
    ? `${school.code}::${status}::${query}::${typeFilter}::${roleFilter}::${validityFilter}::${formatFilter}`
    : null;
  const keyRef = useRef(listKey);
  keyRef.current = listKey;
  const { state, loadedKey, reload } = useKeyedResource<DocumentPage>(listKey, (_key, signal) =>
    client.listDocuments(school.code, listParams(), signal),
  );
  const { state: optionsState } = useKeyedResource<DocumentOptions>(
    perms.read ? `${school.code}::${uiDefaultLang}` : null,
    (_key, signal) => client.getDocumentOptions(school.code, uiDefaultLang, signal),
  );
  const options = optionsState.status === "ready" ? optionsState.value : null;

  function listParams() {
    return {
      status,
      q: query || undefined,
      type: typeFilter || undefined,
      role: roleFilter || undefined,
      validity: validityFilter === "all" ? undefined : validityFilter,
      format: formatFilter || undefined,
      limit: DOCUMENTS_PAGE_SIZE,
    };
  }

  function reloadAll() {
    setMore(null);
    setSelected([]);
    reload();
    setStatsNonce((n) => n + 1);
  }

  // A different list (filters, school) makes the old selection meaningless.
  useEffect(() => {
    setSelected([]);
    setBulkResult(null);
  }, [listKey]);

  const ready = state.status === "ready" && loadedKey === listKey;
  const extra = more !== null && more.key === listKey ? more : null;
  const documents = ready ? [...state.value.documents, ...(extra?.documents ?? [])] : [];
  const nextCursor = ready ? (extra ? extra.next_cursor : state.value.next_cursor ?? null) : null;

  const selectedDocs = documents.filter((d) => selected.includes(d.file_uuid));
  const allSelected = documents.length > 0 && selectedDocs.length === documents.length;
  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = selectedDocs.length > 0 && !allSelected;
  });

  async function loadMore() {
    const key = listKey;
    if (!key || !nextCursor) return;
    setLoadingMore(true);
    try {
      const page = await client.listDocuments(school.code, { ...listParams(), cursor: nextCursor });
      if (keyRef.current !== key) return;
      setMore((prev) => ({
        key,
        documents: [...(prev && prev.key === key ? prev.documents : []), ...page.documents],
        next_cursor: page.next_cursor,
      }));
    } catch (err) {
      if (keyRef.current === key) setActionError(t.actionFailed(err instanceof Error ? err.message : strings.errors.generic));
    } finally {
      setLoadingMore(false);
    }
  }

  async function refreshFromWeb(doc: DocumentSummary) {
    setActionError(null);
    try {
      await client.refreshDocument(school.code, doc.file_uuid);
      // The queued job shows up in "Probíhající nahrávání".
      setJobsRefresh((n) => n + 1);
    } catch (err) {
      setActionError(t.refreshFailed(err instanceof Error ? err.message : strings.errors.generic));
    }
  }

  async function download(fileUuid: string) {
    setActionError(null);
    try {
      await saveStoredFile(client, school.code, fileUuid);
    } catch (err) {
      setActionError(downloadErrorMessage(err));
    }
  }

  async function runAction(doc: DocumentSummary, action: (school: string, fileUuid: string) => Promise<unknown>) {
    setBusy(doc.file_uuid);
    setActionError(null);
    try {
      await action(school.code, doc.file_uuid);
      reloadAll();
    } catch (err) {
      setActionError(t.actionFailed(err instanceof Error ? err.message : strings.errors.generic));
    } finally {
      setBusy(null);
    }
  }

  // Sequential on purpose: the per-document endpoints are the contract, there is no bulk one.
  async function runBulk(targets: DocumentSummary[], action: (school: string, fileUuid: string) => Promise<unknown>) {
    setBusy("bulk");
    setActionError(null);
    setBulkResult(null);
    const failures: BulkResult["failures"] = [];
    for (let i = 0; i < targets.length; i++) {
      setBulkProgress({ done: i, total: targets.length });
      try {
        await action(school.code, targets[i].file_uuid);
      } catch (err) {
        failures.push({ title: targets[i].title, message: err instanceof Error ? err.message : strings.errors.generic });
      }
    }
    setBulkProgress(null);
    setBusy(null);
    setBulkResult({ ok: targets.length - failures.length, total: targets.length, failures });
    reloadAll();
  }

  if (!perms.read) {
    return (
      <section className="documents-page">
        <h2>{t.heading}</h2>
        <p role="alert">{t.noReadPermission}</p>
      </section>
    );
  }

  const filtersActive = typeFilter !== "" || roleFilter !== "" || validityFilter !== "all" || formatFilter !== "" || status !== "active" || queryInput !== "";
  // §14.12: a tile sets the status and validity filters it counts by; pressed again it goes back to the default.
  function setView(nextStatus: DocumentStatusFilter, nextValidity: DocumentValidityFilter) {
    setStatus(nextStatus);
    setValidityFilter(nextValidity);
  }
  const tiles: Array<{
    key: string;
    icon: string;
    label: string;
    stat: keyof DocumentStats;
    pressed: boolean;
    apply: (() => void) | null;
  }> = [
    {
      key: "active",
      icon: "📄",
      label: t.tileActive,
      stat: "active",
      pressed: status === "active" && validityFilter === "all",
      apply: () => setView("active", "all"),
    },
    {
      key: "inactive",
      icon: "⏸️",
      label: t.tileInactive,
      stat: "inactive",
      pressed: status === "inactive" && validityFilter === "all",
      apply: () => (status === "inactive" && validityFilter === "all" ? setView("active", "all") : setView("inactive", "all")),
    },
    {
      key: "expired",
      icon: "⌛",
      label: t.tileExpired,
      stat: "expired",
      // stats.expired counts ACTIVE documents only (search §14.12), so filter the same way.
      pressed: status === "active" && validityFilter === "expired",
      apply: () => (status === "active" && validityFilter === "expired" ? setView("active", "all") : setView("active", "expired")),
    },
    {
      key: "expiring",
      icon: "⏰",
      label: t.tileExpiring,
      stat: "expiring_30",
      pressed: status === "active" && validityFilter === "expiring",
      apply: () => (status === "active" && validityFilter === "expiring" ? setView("active", "all") : setView("active", "expiring")),
    },
  ];
  const toDeactivate = selectedDocs.filter((d) => d.is_active);
  const toActivate = selectedDocs.filter((d) => !d.is_active);

  function rowMenu(doc: DocumentSummary): MenuItem[] {
    const items: MenuItem[] = [
      {
        key: "detail",
        label: t.detail,
        onSelect: () => {
          onOpenDetail?.();
          setDetailTarget(doc);
        },
      },
      { key: "download", label: t.download, onSelect: () => void download(doc.file_uuid) },
    ];
    if (perms.ingest) {
      items.push({ key: "version", label: t.newVersion, disabled: options === null, onSelect: () => setVersionTarget(doc) });
    }
    if (perms.ingest && doc.source_kind === "web") {
      items.push({ key: "refresh", label: t.refresh, onSelect: () => void refreshFromWeb(doc) });
    }
    if (perms.deactivate) {
      items.push({ key: "validity", label: t.editValidity, disabled: options === null, onSelect: () => setValidityTarget(doc) });
    }
    if (perms.delete_permanently) {
      items.push({ key: "delete", label: t.delete, danger: true, onSelect: () => setDeleteTarget(doc) });
    }
    return items;
  }

  return (
    <section className="documents-page" aria-labelledby="documents-heading">
      <div className="documents-head page-head">
        <h2 id="documents-heading">{t.heading}</h2>
        {perms.ingest && (
          <button type="button" className="btn btn--primary" disabled={options === null} onClick={() => setUploadOpen(true)}>
            {t.addDocuments}
          </button>
        )}
      </div>
      {optionsState.status === "loading" && <p className="qa-muted">{t.optionsLoading}</p>}
      {optionsState.status === "error" && perms.ingest && <p role="alert">{t.optionsError}</p>}

      <section className="doc-tiles" aria-label={t.statsLabel}>
        {tiles.map((tile) => {
          const body = (
            <>
              <span className="doc-tile-icon" aria-hidden="true">
                {tile.icon}
              </span>
              <span className="doc-tile-count">{stats ? stats[tile.stat] : "–"}</span>
              <span className="doc-tile-label">{tile.label}</span>
            </>
          );
          return tile.apply ? (
            <button
              key={tile.key}
              type="button"
              className={tile.pressed ? "doc-tile is-pressed" : "doc-tile"}
              aria-pressed={tile.pressed}
              title={t.tileHint(tile.label)}
              onClick={tile.apply}
            >
              {body}
            </button>
          ) : (
            <div key={tile.key} className="doc-tile is-static">
              {body}
            </div>
          );
        })}
        {statsFailed && stats === null && (
          <p role="alert" className="qa-muted">
            {t.statsError}
          </p>
        )}
      </section>

      <DocumentJobsPanel
        key={school.code}
        client={client}
        school={school.code}
        refreshKey={jobsRefresh}
        onSettled={reloadAll}
      />

      <div className="documents-filters">
        <label className="field">
          <span>{t.searchLabel}</span>
          <input
            type="search"
            value={queryInput}
            placeholder={t.searchPlaceholder}
            onChange={(e) => setQueryInput(e.target.value)}
          />
        </label>
        <label className="field">
          <span>{t.filterLabel}</span>
          <select value={status} onChange={(e) => setStatus(e.target.value as DocumentStatusFilter)}>
            <option value="active">{t.filterActive}</option>
            <option value="inactive">{t.filterInactive}</option>
            <option value="all">{t.filterAll}</option>
          </select>
        </label>
        <label className="field">
          <span>{t.filterTypeLabel}</span>
          <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
            <option value="">{t.filterTypeAll}</option>
            {options?.document_types.map((o) => (
              <option key={o.code} value={o.code}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>{t.filterRoleLabel}</span>
          <select value={roleFilter} onChange={(e) => setRoleFilter(e.target.value)}>
            <option value="">{t.filterRoleAll}</option>
            {options?.audience_roles.map((o) => (
              <option key={o.code} value={o.code}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>{t.filterValidityLabel}</span>
          <select value={validityFilter} onChange={(e) => setValidityFilter(e.target.value as DocumentValidityFilter)}>
            <option value="all">{t.filterValidityAll}</option>
            <option value="current">{t.filterValidityCurrent}</option>
            <option value="expired">{t.filterValidityExpired}</option>
            <option value="future">{t.filterValidityFuture}</option>
            <option value="expiring">{t.filterValidityExpiring}</option>
          </select>
        </label>
        <label className="field">
          <span>{t.filterFormatLabel}</span>
          <select value={formatFilter} onChange={(e) => setFormatFilter(e.target.value as DocumentFormatFilter | "")}>
            <option value="">{t.filterFormatAll}</option>
            {FORMAT_FILTERS.map((code) => (
              <option key={code} value={code}>
                {strings.documentFormats[code]}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="btn btn--ghost"
          disabled={!filtersActive}
          onClick={() => {
            setQueryInput("");
            setQuery("");
            setStatus("active");
            setTypeFilter("");
            setRoleFilter("");
            setValidityFilter("all");
            setFormatFilter("");
          }}
        >
          {t.resetFilters}
        </button>
      </div>

      {actionError && <p role="alert" className="import-errors">{actionError}</p>}

      {selectedDocs.length > 0 && (
        <div className="bulk-bar" role="group" aria-label={b.selected(selectedDocs.length)}>
          <strong>{b.selected(selectedDocs.length)}</strong>
          {perms.deactivate && (
            <>
              <button
                type="button"
                className="btn btn--secondary"
                disabled={busy !== null || toDeactivate.length === 0}
                onClick={() => runBulk(toDeactivate, (sc, id) => client.deactivateDocument(sc, id))}
              >
                {b.deactivate(toDeactivate.length)}
              </button>
              <button
                type="button"
                className="btn btn--secondary"
                disabled={busy !== null || toActivate.length === 0}
                onClick={() => runBulk(toActivate, (sc, id) => client.activateDocument(sc, id))}
              >
                {b.activate(toActivate.length)}
              </button>
            </>
          )}
          {perms.delete_permanently && (
            <button type="button" className="btn btn--danger" disabled={busy !== null} onClick={() => setBulkDeleteDocs(selectedDocs)}>
              {b.delete(selectedDocs.length)}
            </button>
          )}
          <button type="button" className="btn btn--ghost" disabled={busy !== null} onClick={() => setSelected([])}>
            {b.clear}
          </button>
        </div>
      )}
      {bulkProgress && (
        <p role="status">{b.running(bulkProgress.done + 1, bulkProgress.total)}</p>
      )}
      {bulkResult && (
        <div className="bulk-result" role="status">
          <p>{b.resultOk(bulkResult.ok, bulkResult.total)}</p>
          {bulkResult.failures.length > 0 && (
            <>
              <p>{b.resultFailedHeading}</p>
              <ul>
                {bulkResult.failures.map((f, i) => (
                  <li key={`${f.title}-${i}`}>{b.resultFailedItem(f.title, f.message)}</li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      {(state.status === "loading" || (state.status === "ready" && !ready)) && <p role="status">{t.loading}</p>}
      {state.status === "error" && (
        <p role="alert">
          {t.loadError} {state.message}{" "}
          <button type="button" className="btn btn--secondary" onClick={reloadAll}>
            {strings.toolbar.retry}
          </button>
        </p>
      )}
      {ready && documents.length === 0 && <p className="qa-muted">{t.empty}</p>}

      {ready && documents.length > 0 && (
        <div className="doc-table-scroll">
          <table className="doc-table">
            <thead>
              <tr>
                <th scope="col" className="col-check">
                  <input
                    ref={selectAllRef}
                    type="checkbox"
                    aria-label={t.selectAll}
                    checked={allSelected}
                    onChange={() => setSelected(allSelected ? [] : documents.map((d) => d.file_uuid))}
                  />
                </th>
                <th scope="col">{t.colTitle}</th>
                <th scope="col">{t.colType}</th>
                <th scope="col">{t.colVisibility}</th>
                <th scope="col">{t.colRoles}</th>
                <th scope="col">{t.colValidity}</th>
                <th scope="col">{t.colUploaded}</th>
                <th scope="col">{t.colChunks}</th>
                <th scope="col">{t.colVersions}</th>
                <th scope="col">{t.colState}</th>
                <th scope="col" className="col-actions">
                  {t.colActions}
                </th>
              </tr>
            </thead>
            <tbody>
              {documents.map((doc) => {
                const isSelected = selected.includes(doc.file_uuid);
                const rowClass = [doc.is_active ? "" : "doc-row-inactive", isSelected ? "doc-row-selected" : ""].filter(Boolean).join(" ");
                return (
                  <tr key={doc.file_uuid} className={rowClass || undefined}>
                    <td className="col-check">
                      <input
                        type="checkbox"
                        aria-label={t.selectRow(doc.title)}
                        checked={isSelected}
                        onChange={() =>
                          setSelected((prev) => (isSelected ? prev.filter((id) => id !== doc.file_uuid) : [...prev, doc.file_uuid]))
                        }
                      />
                    </td>
                    <td className="doc-cell-title">
                      {doc.source_url && /^https?:\/\//i.test(doc.source_url) ? (
                        <a href={doc.source_url} target="_blank" rel="noopener noreferrer" title={doc.source_url}>
                          {shortTitle(doc.title)}
                        </a>
                      ) : (
                        <span className="doc-title-text">{shortTitle(doc.title)}</span>
                      )}
                      {doc.file_format ? (
                        <span className="chip doc-format" title={t.formatChipTitle(doc.file_format)}>
                          {formatChip(doc.file_format)}
                        </span>
                      ) : (
                        <span className="qa-muted doc-kind">{doc.source_kind === "web" ? t.kindWeb : t.kindFile}</span>
                      )}
                    </td>
                    <td>
                      <span>{codeLabel(options?.document_types, doc.document_type)}</span>
                      <span className="doc-sub">{codeLabel(options?.languages, doc.document_language)}</span>
                    </td>
                    <td>{visibilityLabel(doc.visibility)}</td>
                    <td>{doc.audience_roles.map((r) => codeLabel(options?.audience_roles, r)).join(", ") || t.notSet}</td>
                    <td>{doc.valid_from || doc.valid_to ? t.validityRange(doc.valid_from, doc.valid_to) : t.validityUnlimited}</td>
                    <td>
                      {formatDateTime(doc.ingested_at)}
                      {doc.user_name && <span className="qa-muted doc-kind">{t.uploadedBy(doc.user_name)}</span>}
                    </td>
                    <td>{doc.chunk_count}</td>
                    <td>{doc.version_count}</td>
                    <td>
                      <div className="doc-states">
                        <span className={doc.is_active ? "badge badge-active" : "badge badge-inactive"}>
                          {doc.is_active ? t.stateActive : t.stateInactive}
                        </span>
                        <ValidityBadge doc={doc} />
                      </div>
                    </td>
                    <td className="col-actions">
                      <div className="doc-actions" role="group" aria-label={t.rowActionsLabel(doc.title)}>
                        {perms.deactivate && doc.is_active && (
                          <button
                            type="button"
                            className="btn btn--secondary btn--sm"
                            disabled={busy !== null}
                            onClick={() => runAction(doc, (sc, id) => client.deactivateDocument(sc, id))}
                          >
                            {t.deactivate}
                          </button>
                        )}
                        {perms.deactivate && !doc.is_active && (
                          <button
                            type="button"
                            className="btn btn--secondary btn--sm"
                            disabled={busy !== null}
                            onClick={() => runAction(doc, (sc, id) => client.activateDocument(sc, id))}
                          >
                            {t.activate}
                          </button>
                        )}
                        <OverflowMenu label={t.moreActionsFor(doc.title)} items={rowMenu(doc)} />
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {ready && nextCursor && (
        <button type="button" className="btn btn--secondary" disabled={loadingMore} onClick={loadMore}>
          {loadingMore ? t.loadingMore : t.loadMore}
        </button>
      )}

      {detailTarget && !askOpen && (
        <DocumentDetailDrawer
          client={client}
          school={school.code}
          doc={detailTarget}
          options={options}
          onClose={() => setDetailTarget(null)}
        />
      )}

      {uploadOpen && options && (
        <DocumentUploadDialog
          client={client}
          school={school.code}
          options={options}
          uiDefaultLang={uiDefaultLang}
          onClose={(sentAny) => {
            setUploadOpen(false);
            if (sentAny) {
              setJobsRefresh((n) => n + 1);
              reloadAll();
            }
          }}
        />
      )}

      {versionTarget && options && (
        <DocumentUploadDialog
          client={client}
          school={school.code}
          options={options}
          uiDefaultLang={uiDefaultLang}
          newVersionOf={versionTarget}
          onClose={(sentAny) => {
            setVersionTarget(null);
            if (sentAny) {
              setJobsRefresh((n) => n + 1);
              reloadAll();
            }
          }}
        />
      )}

      {validityTarget && options && (
        <DocumentValidityDialog
          client={client}
          school={school.code}
          doc={validityTarget}
          options={options}
          onClose={() => setValidityTarget(null)}
          onSaved={() => {
            setValidityTarget(null);
            reloadAll();
          }}
        />
      )}

      {deleteTarget && (
        <DocumentDeleteDialog
          client={client}
          school={school.code}
          schoolName={school.name}
          fileUuid={deleteTarget.file_uuid}
          onClose={() => setDeleteTarget(null)}
          onDeleted={reloadAll}
        />
      )}

      {bulkDeleteDocs && (
        <DocumentBulkDeleteDialog
          client={client}
          school={school.code}
          schoolName={school.name}
          documents={bulkDeleteDocs}
          onClose={() => setBulkDeleteDocs(null)}
          onDone={reloadAll}
        />
      )}
    </section>
  );
}
