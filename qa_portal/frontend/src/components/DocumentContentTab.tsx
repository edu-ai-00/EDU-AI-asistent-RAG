import { useState } from "react";
import type { ApiClient } from "../api/client";
import type { DocumentChunkPage, DocumentVersion } from "../api/types";
import { useKeyedResource } from "../hooks/useKeyedResource";
import { formatDateTime } from "../lib/documentFormat";
import { strings } from "../strings.cs";

type Props = {
  client: ApiClient;
  school: string;
  /** Versions of the document, newest first; may still be empty while the detail loads. */
  versions: DocumentVersion[];
  /** The version the list shows: the select starts here. */
  shownVersion: string;
};

const t = strings.documentDetail;
export const CHUNKS_PAGE_SIZE = 20;
export const CHUNK_CLIP = 400;

/** "Obsah" tab (CONTRACT.md §14.13): the chunks of one version with heading path, index and
 *  length, long text clipped, 20 per page. The cursor of page N+1 is the `next_cursor` of page N. */
export function DocumentContentTab({ client, school, versions, shownVersion }: Props) {
  const [version, setVersion] = useState(shownVersion);
  const [page, setPage] = useState(0);
  const [cursors, setCursors] = useState<Array<string | undefined>>([undefined]);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const cursor = cursors[page];
  const key = `${school}::${version}::${page}::${cursor ?? ""}`;
  const { state } = useKeyedResource<DocumentChunkPage>(key, (_k, signal) =>
    client.listDocumentChunks(school, version, { limit: CHUNKS_PAGE_SIZE, cursor }, signal),
  );

  function pickVersion(next: string) {
    setVersion(next);
    setPage(0);
    setCursors([undefined]);
    setExpanded(new Set());
  }

  function go(delta: 1 | -1) {
    if (delta === 1 && state.status === "ready") {
      const next = state.value.next_cursor ?? undefined;
      setCursors((prev) => {
        const copy = prev.slice(0, page + 1);
        copy[page + 1] = next;
        return copy;
      });
    }
    setExpanded(new Set());
    setPage((p) => p + delta);
  }

  function toggle(index: number) {
    setExpanded((prev) => {
      const copy = new Set(prev);
      if (copy.has(index)) copy.delete(index);
      else copy.add(index);
      return copy;
    });
  }

  const options = versions.some((v) => v.file_uuid === shownVersion) ? versions : [{ file_uuid: shownVersion } as DocumentVersion, ...versions];

  return (
    <div className="chunk-tab">
      <label className="field">
        <span>{t.contentVersion}</span>
        <select value={version} onChange={(event) => pickVersion(event.target.value)}>
          {options.map((v) => (
            <option key={v.file_uuid} value={v.file_uuid}>
              {[v.file_uuid.slice(0, 8), v.ingested_at ? formatDateTime(v.ingested_at) : null, v.file_uuid === shownVersion ? t.shownVersion : null]
                .filter(Boolean)
                .join(" · ")}
            </option>
          ))}
        </select>
      </label>

      {state.status === "loading" && <p role="status">{t.contentLoading}</p>}
      {state.status === "error" && <p role="alert">{`${t.contentError} ${state.message}`}</p>}
      {state.status === "ready" && state.value.chunks.length === 0 && <p className="qa-muted">{t.contentEmpty}</p>}
      {state.status === "ready" && state.value.chunks.length > 0 && (
        <>
          <ol className="chunk-list" start={state.value.chunks[0].chunk_index}>
            {state.value.chunks.map((chunk) => {
              const long = chunk.text.length > CHUNK_CLIP;
              const open = expanded.has(chunk.chunk_index);
              const heading = chunk.heading_path || chunk.section_heading;
              return (
                <li key={chunk.chunk_index} className="chunk">
                  <div className="chunk-head">
                    <strong>{t.chunkTitle(chunk.chunk_index)}</strong>
                    <span className="chip">{t.chunkChars(chunk.char_count)}</span>
                    {heading && <span className="chunk-path qa-muted">{heading}</span>}
                  </div>
                  <p className="chunk-text">{long && !open ? `${chunk.text.slice(0, CHUNK_CLIP).trimEnd()}…` : chunk.text}</p>
                  {long && (
                    <button type="button" className="btn btn--ghost" aria-expanded={open} onClick={() => toggle(chunk.chunk_index)}>
                      {open ? t.showLess : t.showFull}
                    </button>
                  )}
                </li>
              );
            })}
          </ol>
          <nav className="entry-pager" aria-label={t.pagerLabel}>
            <span className="entry-pager-range qa-muted">
              {t.pagerRange(page * CHUNKS_PAGE_SIZE + 1, page * CHUNKS_PAGE_SIZE + state.value.chunks.length, state.value.total)}
            </span>
            <span className="chunk-pager-buttons">
              <button type="button" className="btn btn--secondary" disabled={page === 0} onClick={() => go(-1)}>
                {t.pagePrev}
              </button>
              <button type="button" className="btn btn--secondary" disabled={!state.value.next_cursor} onClick={() => go(1)}>
                {t.pageNext}
              </button>
            </span>
          </nav>
        </>
      )}
    </div>
  );
}
