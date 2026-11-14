import { useEffect, useId, useRef, useState, type MouseEvent } from "react";
import type { ApiClient } from "../api/client";
import type { DocumentDetail, DocumentOptions, DocumentSummary } from "../api/types";
import { useKeyedResource } from "../hooks/useKeyedResource";
import { formatDateTime } from "../lib/documentFormat";
import { codeLabel, formatChip, visibilityLabel } from "../lib/documentLabels";
import { downloadErrorMessage, saveStoredFile } from "../lib/download";
import { strings } from "../strings.cs";
import { DocumentContentTab } from "./DocumentContentTab";
import { ValidityBadge } from "./ValidityBadge";

type Props = {
  client: ApiClient;
  school: string;
  /** The list row; shown at once, replaced by the fresher detail when it arrives. */
  doc: DocumentSummary;
  options: DocumentOptions | null;
  onClose: () => void;
};

const t = strings.documentDetail;
const d = strings.documents;
const dl = strings.documentDownload;

function list(values: string[], labelOf: (code: string) => string): string {
  return values.length > 0 ? values.map(labelOf).join(", ") : t.none;
}

/** Non-modal side panel: metadata, validity, versions (GET detail), source link. */
export function DocumentDetailDrawer({ client, school, doc, options, onClose }: Props) {
  const titleId = useId();
  const closeRef = useRef<HTMLButtonElement>(null);
  const { state } = useKeyedResource<DocumentDetail>(`${school}::${doc.file_uuid}`, (_key, signal) =>
    client.getDocument(school, doc.file_uuid, signal),
  );

  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [tab, setTab] = useState<"overview" | "content">("overview");

  async function download(event: MouseEvent<HTMLAnchorElement>, fileUuid: string) {
    // A missing file answers 404 JSON: fetch it, never navigate to it.
    event.preventDefault();
    setDownloadError(null);
    try {
      await saveStoredFile(client, school, fileUuid);
    } catch (err) {
      setDownloadError(downloadErrorMessage(err));
    }
  }

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    return () => previous?.focus();
  }, []);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const shown = state.status === "ready" ? state.value.document : doc;
  const versions = state.status === "ready" ? state.value.versions : [];
  const externalUrl = shown.source_url && /^https?:\/\//i.test(shown.source_url) ? shown.source_url : null;

  return (
    <aside className="drawer" role="dialog" aria-modal="false" aria-labelledby={titleId}>
      <div className="drawer-header">
        <h2 id={titleId}>{t.title}</h2>
        <button ref={closeRef} type="button" className="btn btn--secondary" onClick={onClose}>
          {t.close}
        </button>
      </div>
      <div className="drawer-body">
        <p className="drawer-title">{shown.title}</p>
        <div className="detail-tabs" role="tablist" aria-label={t.tabsLabel}>
          {(["overview", "content"] as const).map((id) => (
            <button
              key={id}
              type="button"
              role="tab"
              id={`detail-tab-${id}`}
              aria-selected={tab === id}
              aria-controls="detail-panel"
              className={tab === id ? "btn btn--secondary is-current" : "btn btn--ghost"}
              onClick={() => setTab(id)}
            >
              {id === "overview" ? t.tabOverview : t.tabContent}
            </button>
          ))}
        </div>
        <div id="detail-panel" role="tabpanel" aria-labelledby={`detail-tab-${tab}`} className="detail-panel">
          {tab === "content" ? (
            <DocumentContentTab client={client} school={school} versions={versions} shownVersion={shown.file_uuid} />
          ) : (
            <>
        <div className="doc-states">
          <span className={shown.is_active ? "badge badge-active" : "badge badge-inactive"}>
            {shown.is_active ? d.stateActive : d.stateInactive}
          </span>
          <ValidityBadge doc={shown} />
        </div>

        <dl className="detail-grid">
          <dt>{t.source}</dt>
          <dd>
            {externalUrl ? (
              <a href={externalUrl} target="_blank" rel="noopener noreferrer">
                {t.openSource}
              </a>
            ) : (
              (shown.original_file_name ?? t.none)
            )}
          </dd>
          <dt>{t.format}</dt>
          <dd>{shown.file_format ? formatChip(shown.file_format) : t.none}</dd>
          <dt>{t.type}</dt>
          <dd>{codeLabel(options?.document_types, shown.document_type)}</dd>
          <dt>{t.language}</dt>
          <dd>{codeLabel(options?.languages, shown.document_language)}</dd>
          <dt>{t.subject}</dt>
          <dd>{codeLabel(options?.school_subjects, shown.school_subject)}</dd>
          <dt>{t.grades}</dt>
          <dd>{list(shown.school_grade_codes, (c) => codeLabel(options?.school_grades, c))}</dd>
          <dt>{t.domains}</dt>
          <dd>{list(shown.domain_codes, (c) => options?.domains.find((x) => x.code === c)?.label ?? c)}</dd>
          <dt>{t.roles}</dt>
          <dd>{list(shown.audience_roles, (c) => codeLabel(options?.audience_roles, c))}</dd>
          <dt>{t.visibility}</dt>
          <dd>{visibilityLabel(shown.visibility)}</dd>
          <dt>{t.validity}</dt>
          <dd>{shown.valid_from || shown.valid_to ? d.validityRange(shown.valid_from, shown.valid_to) : d.validityUnlimited}</dd>
          <dt>{t.uploaded}</dt>
          <dd>
            {formatDateTime(shown.ingested_at)}
            {shown.user_name ? ` · ${shown.user_name}` : ""}
          </dd>
          <dt>{t.chunks}</dt>
          <dd>{shown.chunk_count}</dd>
        </dl>

        <h3>{t.versionsHeading}</h3>
        {state.status === "loading" && <p role="status">{t.loading}</p>}
        {state.status === "error" && <p role="alert">{`${t.loadError} ${state.message}`}</p>}
        {downloadError && (
          <p role="alert" className="import-errors">
            {downloadError}
          </p>
        )}
        {state.status === "ready" && (
          <div className="doc-table-scroll">
            <table className="doc-table">
              <thead>
                <tr>
                  <th scope="col">{t.colVersion}</th>
                  <th scope="col">{t.colProcessing}</th>
                  <th scope="col">{t.colIngested}</th>
                  <th scope="col">{t.colChunks}</th>
                  <th scope="col">{t.colState}</th>
                  <th scope="col">{dl.column}</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.file_uuid}>
                    <td>
                      <code>{v.file_uuid}</code>
                      {v.file_uuid === shown.file_uuid && <span className="doc-sub">{t.shownVersion}</span>}
                      {v.content_sha256 && (
                        <span className="doc-sub">{`${t.contentHash}: ${t.contentHashValue(v.content_sha256.slice(0, 8))}`}</span>
                      )}
                    </td>
                    <td>
                      <code>{v.processing_uuid ?? t.none}</code>
                    </td>
                    <td>{formatDateTime(v.ingested_at)}</td>
                    <td>{v.chunk_count}</td>
                    <td>
                      <span className={v.is_active ? "badge badge-active" : "badge badge-inactive"}>
                        {v.is_active ? d.stateActive : d.stateInactive}
                      </span>
                    </td>
                    <td>
                      <a
                        href={client.documentDownloadUrl(school, v.file_uuid)}
                        download
                        onClick={(event) => void download(event, v.file_uuid)}
                      >
                        {(v.file_format ?? (shown.source_kind === "web" ? "web" : null)) === "web" ? dl.webPage : dl.file}
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
            </>
          )}
        </div>
      </div>
    </aside>
  );
}
