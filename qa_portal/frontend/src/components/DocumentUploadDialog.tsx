import { useEffect, useRef, useState, type DragEvent, type KeyboardEvent } from "react";
import type { ApiClient } from "../api/client";
import {
  ApiError,
  type DocumentJob,
  type DocumentMetadata,
  type DocumentOptions,
  type DocumentSummary,
  type SourceCheckResult,
  type UrlPreviewFile,
} from "../api/types";
import { DOCUMENT_JOBS_POLL_INTERVAL_MS } from "../config";
import { fileExtension, formatBytes, isHttpUrl } from "../lib/documentFormat";
import { jobNeedsPoll, jobText } from "../lib/documentJobs";
import { JobStatus } from "./JobStatus";
import { strings } from "../strings.cs";
import { Dialog } from "./Dialog";
import {
  DocumentMetadataFields,
  EMPTY_DOCUMENT_METADATA_DRAFT,
  resolveValidity,
  validityDraftFromDates,
  type DocumentMetadataDraft,
} from "./DocumentMetadataFields";
import { EMPTY_PACK_METADATA_DRAFT, PackMetadataFields, type PackMetadataDraft } from "./PackMetadataFields";

type Props = {
  client: ApiClient;
  school: string;
  options: DocumentOptions;
  uiDefaultLang: string;
  /** `sentAny` = at least one item reached the server (list should reload). */
  onClose: (sentAny: boolean) => void;
  /** "Nová verze": metadata and source come from this document. */
  newVersionOf?: DocumentSummary;
};

type ItemState =
  | { kind: "pending" }
  | { kind: "sending" }
  | { kind: "accepted"; job: DocumentJob }
  | { kind: "rejected"; message: string; fields: string[] }
  | { kind: "notSent" };

/** Batch defaults and a per-item override share this shape. */
type ItemMeta = { pack: PackMetadataDraft; doc: DocumentMetadataDraft };

type Source = {
  key: string;
  kind: "file" | "url";
  label: string;
  file?: File;
  url?: string;
  error: string | null;
};

type ProgressItem = {
  id: number;
  key: string;
  label: string;
  file?: File;
  url?: string;
  metadata: DocumentMetadata;
  state: ItemState;
};

type PreviewState =
  | { status: "loading" }
  | { status: "ready"; files: UrlPreviewFile[] }
  | { status: "error"; message: string };

const t = strings.documentUpload;

function itemText(state: ItemState, now: number): string {
  switch (state.kind) {
    case "pending":
      return t.statePending;
    case "sending":
      return t.stateSending;
    case "accepted":
      return jobText(state.job, now);
    case "rejected":
      return t.stateRejected(state.message);
    case "notSent":
      return t.stateNotSent;
  }
}

/** Per-file client-side check against document-options; null = ok. */
function fileError(file: File, index: number, files: File[], options: DocumentOptions, requiredExt: string | null): string | null {
  const ext = fileExtension(file.name);
  if (!options.file_extensions.includes(ext)) return t.errExtension(ext);
  if (requiredExt !== null && ext !== requiredExt) return t.errExtensionMismatch(requiredExt);
  if (file.size === 0) return t.errEmpty;
  if (file.size > options.max_file_bytes) return t.errTooLarge(formatBytes(options.max_file_bytes));
  if (files.findIndex((f) => f.name === file.name) < index) return t.errDuplicateName;
  return null;
}

function parseUrlLines(text: string): Array<{ line: number; url: string; error: string | null }> {
  const seen = new Set<string>();
  const out: Array<{ line: number; url: string; error: string | null }> = [];
  text.split(/\r?\n/).forEach((raw, i) => {
    const url = raw.trim();
    if (!url) return;
    let error: string | null = null;
    if (!isHttpUrl(url)) error = t.errUrlInvalid;
    else if (seen.has(url)) error = t.errUrlDuplicate;
    seen.add(url);
    out.push({ line: i + 1, url, error });
  });
  return out;
}

function metaIncomplete(meta: ItemMeta, options: DocumentOptions): boolean {
  return (
    meta.pack.visibility === "" ||
    meta.pack.audience_roles.length === 0 ||
    meta.doc.validity === "" ||
    resolveValidity(meta.doc, options).error !== null
  );
}

function toMetadata(meta: ItemMeta, options: DocumentOptions): DocumentMetadata | null {
  if (metaIncomplete(meta, options) || meta.pack.visibility === "") return null;
  const validity = resolveValidity(meta.doc, options);
  return {
    visibility: meta.pack.visibility,
    audience_roles: meta.pack.audience_roles,
    document_type: meta.doc.document_type || null,
    document_language: meta.doc.document_language || null,
    school_subject: meta.doc.school_subject || null,
    school_grade_codes: meta.pack.school_grade_codes,
    domain_codes: meta.doc.domain_codes,
    valid_from: validity.valid_from,
    valid_to: validity.valid_to,
  };
}

function checkNote(result: SourceCheckResult | undefined): string | null {
  if (!result || !result.existing) return null;
  return result.supersedes ? t.checkSupersedes : t.checkSameNameOtherAuthor;
}

function prefillFromDocument(doc: DocumentSummary, options: DocumentOptions): ItemMeta {
  return {
    pack: {
      // "private" is not an upload choice, so the administrator picks again.
      visibility: doc.visibility === "private" ? "" : doc.visibility,
      audience_roles: [...doc.audience_roles],
      school_grade_codes: [...doc.school_grade_codes],
    },
    doc: {
      document_type: doc.document_type ?? "",
      document_language: doc.document_language ?? "",
      school_subject: doc.school_subject ?? "",
      domain_codes: [...doc.domain_codes],
      ...validityDraftFromDates(doc.valid_from, doc.valid_to, options),
    },
  };
}

/** "Přidat dokumenty" (spec §8): one batch of files and/or URLs with default
 *  metadata (overridable per item), sent ONE item at a time, then job states
 *  polled per batch_id. With `newVersionOf` it uploads one new version. */
export function DocumentUploadDialog({ client, school, options, uiDefaultLang, onClose, newVersionOf }: Props) {
  const initial = useRef<ItemMeta | null>(null);
  if (initial.current === null) {
    initial.current = newVersionOf
      ? prefillFromDocument(newVersionOf, options)
      : { pack: EMPTY_PACK_METADATA_DRAFT, doc: EMPTY_DOCUMENT_METADATA_DRAFT };
  }
  const isWebVersion = newVersionOf?.source_kind === "web";
  const isFileVersion = newVersionOf?.source_kind === "file";
  const originalName = newVersionOf?.original_file_name ?? null;

  const [tab, setTab] = useState<"files" | "urls">(isWebVersion ? "urls" : "files");
  const [files, setFiles] = useState<File[]>([]);
  const [urlsText, setUrlsText] = useState(isWebVersion ? (newVersionOf?.source_url ?? "") : "");
  const [withChildren, setWithChildren] = useState(false);
  const [childLinks, setChildLinks] = useState(String(Math.min(5, options.max_child_links)));
  const [packDraft, setPackDraft] = useState<PackMetadataDraft>(initial.current.pack);
  const [docDraft, setDocDraft] = useState<DocumentMetadataDraft>(initial.current.doc);
  const [overrides, setOverrides] = useState<Record<string, ItemMeta>>({});
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [checks, setChecks] = useState<Record<string, SourceCheckResult>>({});
  const [previews, setPreviews] = useState<Record<string, PreviewState>>({});

  const [phase, setPhase] = useState<"edit" | "sending" | "sent">("edit");
  const [items, setItems] = useState<ProgressItem[]>([]);
  const [batchId, setBatchId] = useState<string | null>(null);
  const [pollTick, setPollTick] = useState(0);
  const [pollError, setPollError] = useState(false);
  const cancelledRef = useRef(false);
  const checkedKeyRef = useRef<string | null>(null);
  const tabRefs = { files: useRef<HTMLButtonElement>(null), urls: useRef<HTMLButtonElement>(null) };

  const urlLines = parseUrlLines(urlsText);
  const requiredExt = originalName ? fileExtension(originalName) : null;
  const fileErrors = files.map((f, i) => fileError(f, i, files, options, requiredExt));
  const childCount = Number(childLinks);
  const childLinksValid = !withChildren || (Number.isInteger(childCount) && childCount >= 1 && childCount <= options.max_child_links);
  const totalItems = files.length + urlLines.length;

  const sources: Source[] = [
    ...files.map((file, i) => ({ key: `file:${file.name}`, kind: "file" as const, label: file.name, file, error: fileErrors[i] })),
    ...urlLines.map((l) => ({ key: `url:${l.url}`, kind: "url" as const, label: l.url, url: l.url, error: l.error })),
  ];

  const defaults: ItemMeta = { pack: packDraft, doc: docDraft };
  const metaFor = (key: string): ItemMeta => overrides[key] ?? defaults;
  const defaultsNeeded = totalItems === 0 || sources.some((s) => !(s.key in overrides));
  const overridesIncomplete = sources.some((s) => s.key in overrides && metaIncomplete(overrides[s.key], options));

  const missing: string[] = [];
  if (isFileVersion ? files.length !== 1 : totalItems === 0) missing.push(isFileVersion ? t.newVersionOneFile : t.missingItems);
  if (defaultsNeeded) {
    if (packDraft.visibility === "") missing.push(t.missingVisibility);
    if (packDraft.audience_roles.length === 0) missing.push(t.missingRole);
    if (docDraft.validity === "") missing.push(t.missingValidity);
  }
  if (overridesIncomplete) missing.push(t.fixItemMetadata);
  if (fileErrors.some(Boolean) || urlLines.some((l) => l.error)) missing.push(t.fixItems);
  const defaultsValidity = resolveValidity(docDraft, options);
  const batchTooLarge = totalItems > options.max_batch_items;
  const canSubmit =
    phase === "edit" &&
    missing.length === 0 &&
    !batchTooLarge &&
    childLinksValid &&
    (!defaultsNeeded || defaultsValidity.error === null);

  // The browser would drop unsent items on reload/close, so warn while sending.
  useEffect(() => {
    if (phase !== "sending") return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [phase]);

  // Stops the send loop once the dialog unmounts (StrictMode remounts reset it).
  useEffect(() => {
    cancelledRef.current = false;
    return () => {
      cancelledRef.current = true;
    };
  }, []);

  // check-sources: what an upload of the valid items would retire or duplicate. Not blocking.
  const checkList = sources
    .filter((s) => !s.error)
    .slice(0, options.max_batch_items)
    .map((s) => ({ kind: s.kind, value: s.kind === "file" && originalName ? originalName : s.label }));
  const checkKey = JSON.stringify(checkList);

  async function runCheck(signal?: AbortSignal): Promise<void> {
    if (checkList.length === 0) {
      setChecks({});
      checkedKeyRef.current = checkKey;
      return;
    }
    try {
      const results = await client.checkDocumentSources(school, checkList, signal);
      if (signal?.aborted) return;
      setChecks(Object.fromEntries(results.map((r) => [`${r.kind}:${r.value}`, r])));
      checkedKeyRef.current = checkKey;
    } catch {
      if (signal?.aborted) return;
      setChecks({});
    }
  }

  useEffect(() => {
    if (phase !== "edit") return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void runCheck(controller.signal);
    }, 250);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
    // runCheck closes over checkList, which checkKey fully describes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checkKey, phase, client, school]);

  const now = Date.now();
  const needsPoll =
    batchId !== null &&
    items.some((i) => i.state.kind === "accepted" && jobNeedsPoll(i.state.job, now));

  useEffect(() => {
    if (!needsPoll || batchId === null) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const jobs = await client.listDocumentJobs(school, { batch_id: batchId }, controller.signal);
        if (controller.signal.aborted) return;
        const byId = new Map(jobs.map((j) => [j.job_id, j]));
        setItems((prev) =>
          prev.map((it) =>
            it.state.kind === "accepted" && byId.has(it.state.job.job_id)
              ? { ...it, state: { kind: "accepted", job: byId.get(it.state.job.job_id)! } }
              : it,
          ),
        );
        setPollError(false);
      } catch {
        if (controller.signal.aborted) return;
        setPollError(true);
      }
      if (!controller.signal.aborted) setPollTick((n) => n + 1);
    }, DOCUMENT_JOBS_POLL_INTERVAL_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [needsPoll, batchId, pollTick, client, school]);

  function addFiles(list: FileList | null) {
    if (!list || list.length === 0) return;
    // A new version takes exactly one file, so a second pick replaces the first.
    setFiles((prev) => (isFileVersion ? Array.from(list).slice(0, 1) : [...prev, ...Array.from(list)]));
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    if (phase === "edit") addFiles(event.dataTransfer?.files ?? null);
  }

  function handleTabKey(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
    event.preventDefault();
    const next = tab === "files" ? "urls" : "files";
    setTab(next);
    tabRefs[next].current?.focus();
  }

  function setOverride(key: string, next: ItemMeta) {
    setOverrides((prev) => ({ ...prev, [key]: next }));
  }

  function resetOverride(key: string) {
    setOverrides((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
  }

  async function runPreview(src: Source) {
    const metadata = toMetadata(metaFor(src.key), options);
    if (!src.url || metadata === null) return;
    setPreviews((prev) => ({ ...prev, [src.key]: { status: "loading" } }));
    try {
      const found = await client.previewDocumentUrl(school, {
        url: src.url,
        save_level_1_child: withChildren,
        max_child_links: withChildren && childLinksValid ? childCount : Math.min(5, options.max_child_links),
        metadata,
      });
      if (cancelledRef.current) return;
      setPreviews((prev) => ({ ...prev, [src.key]: { status: "ready", files: found } }));
    } catch (err) {
      if (cancelledRef.current) return;
      const message = err instanceof Error ? err.message : strings.errors.generic;
      setPreviews((prev) => ({ ...prev, [src.key]: { status: "error", message } }));
    }
  }

  async function runPreviewAll(list: Source[]) {
    for (const src of list) await runPreview(src);
  }

  async function handleSubmit() {
    if (!canSubmit) return;
    const queue: ProgressItem[] = [];
    for (const src of sources) {
      const metadata = toMetadata(metaFor(src.key), options);
      if (metadata === null) return;
      queue.push({ id: queue.length, key: src.key, label: src.label, file: src.file, url: src.url, metadata, state: { kind: "pending" } });
    }
    // Ask once more when the answers on screen are not for exactly these items.
    if (checkedKeyRef.current !== checkKey) await runCheck();
    if (cancelledRef.current) return;
    const id = crypto.randomUUID();
    setItems(queue);
    setBatchId(id);
    setPhase("sending");
    const update = (itemId: number, state: ItemState) =>
      setItems((prev) => prev.map((it) => (it.id === itemId ? { ...it, state } : it)));

    // Strictly sequential: the next POST starts only after the previous answered.
    for (const item of queue) {
      if (cancelledRef.current) break;
      update(item.id, { kind: "sending" });
      try {
        // A new version of a file document goes out under the document's own name,
        // because that name (plus the uploader) is what ties versions together.
        const file =
          item.file && originalName ? new File([item.file], originalName, { type: item.file.type }) : item.file;
        const job = await client.uploadDocument(school, {
          file,
          url: item.url,
          save_level_1_child: item.url !== undefined ? withChildren : undefined,
          max_child_links: item.url !== undefined && withChildren ? childCount : undefined,
          batch_id: id,
          metadata: item.metadata,
        });
        update(item.id, { kind: "accepted", job });
      } catch (err) {
        const message = err instanceof Error ? err.message : strings.errors.generic;
        const fields = err instanceof ApiError ? (err.fields ?? []).map((f) => `${f.field}: ${f.message}`) : [];
        update(item.id, { kind: "rejected", message, fields });
      }
    }
    if (!cancelledRef.current) setPhase("sent");
  }

  function handleClose() {
    const unsent = items.filter((i) => i.state.kind === "pending");
    if (phase === "sending" && unsent.length > 0) {
      if (!window.confirm(t.confirmCloseUnsent(unsent.map((i) => i.label)))) return;
      cancelledRef.current = true;
      setItems((prev) => prev.map((i) => (i.state.kind === "pending" ? { ...i, state: { kind: "notSent" } } : i)));
    }
    onClose(items.some((i) => i.state.kind === "accepted" || i.state.kind === "sending"));
  }

  const footer =
    phase === "edit" ? (
      <div className="dialog-actions">
        <button type="button" className="btn btn--primary" disabled={!canSubmit} onClick={handleSubmit}>
          {t.submit}
        </button>
        <button type="button" className="btn btn--secondary" onClick={handleClose}>
          {strings.actions.cancel}
        </button>
      </div>
    ) : (
      <div className="dialog-actions">
        <button type="button" className="btn btn--secondary" onClick={handleClose}>
          {t.close}
        </button>
      </div>
    );

  const dialogTitle = newVersionOf ? t.newVersionTitle : t.title;

  if (phase !== "edit") {
    return (
      <Dialog title={dialogTitle} onClose={handleClose} footer={footer} wide>
        <h3>{t.progressHeading}</h3>
        <p className="qa-muted">{t.progressNote}</p>
        <table className="doc-table doc-progress">
          <thead>
            <tr>
              <th scope="col">{t.colItem}</th>
              <th scope="col">{t.colState}</th>
            </tr>
          </thead>
          <tbody aria-live="polite">
            {items.map((item) => {
              const note = checkNote(checks[`${item.file ? "file" : "url"}:${item.file && originalName ? originalName : item.label}`]);
              return (
                <tr key={item.id} className={item.state.kind === "rejected" ? "doc-row-error" : undefined}>
                  <td className="doc-cell-title">
                    {item.label}
                    {note && <span className="doc-sub">{note}</span>}
                  </td>
                  <td>
                    {item.state.kind === "accepted" ? <JobStatus job={item.state.job} now={now} /> : itemText(item.state, now)}
                    {item.state.kind === "rejected" && item.state.fields.length > 0 && (
                      <ul className="doc-field-errors">
                        {item.state.fields.map((f) => (
                          <li key={f}>{f}</li>
                        ))}
                      </ul>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {pollError && <p role="alert">{t.pollError}</p>}
      </Dialog>
    );
  }

  function renderItem(src: Source, index: number) {
    const overridden = src.key in overrides;
    const meta = metaFor(src.key);
    const editing = editingKey === src.key;
    const note = checkNote(checks[`${src.kind}:${src.kind === "file" && originalName ? originalName : src.label}`]);
    const preview = previews[src.key];
    const idPrefix = `doc-upload-item-${src.kind}-${index}`;
    return (
      <li key={src.key} className={src.error ? "doc-file is-invalid" : "doc-file"}>
        <div className="doc-file-main">
          <span className="doc-cell-title">{src.label}</span>
          {src.file && <span className="qa-muted">{formatBytes(src.file.size)}</span>}
          {overridden && <span className="chip chip--accent">{t.itemOverrideChip}</span>}
          {src.kind === "url" && (
            <button
              type="button"
              className="btn btn--secondary"
              aria-label={t.previewLabel(src.label)}
              disabled={src.error !== null || metaIncomplete(meta, options) || preview?.status === "loading"}
              onClick={() => void runPreview(src)}
            >
              {t.previewButton}
            </button>
          )}
          {!newVersionOf && (
            <button
              type="button"
              className="btn btn--secondary"
              aria-label={t.itemEditLabel(src.label)}
              aria-expanded={editing}
              onClick={() => setEditingKey(editing ? null : src.key)}
            >
              {t.itemEdit}
            </button>
          )}
          {src.kind === "file" && (
            <button
              type="button"
              className="btn btn--ghost btn--icon"
              aria-label={t.removeFile(src.label)}
              onClick={() => {
                setFiles((prev) => prev.filter((_, j) => j !== index));
                resetOverride(src.key);
              }}
            >
              ×
            </button>
          )}
        </div>
        {src.error && <span className="doc-file-error">{src.error}</span>}
        {note && <span className="doc-file-note">{note}</span>}
        {preview?.status === "loading" && <span role="status" className="qa-muted">{t.previewLoading}</span>}
        {preview?.status === "error" && <span role="alert" className="doc-file-error">{t.previewError(preview.message)}</span>}
        {preview?.status === "ready" && (
          <div>
            <strong>{t.previewHeading(preview.files.length)}</strong>
            <ul className="doc-preview">
              {preview.files.map((f, i) => (
                <li key={`${f.source_url ?? f.source_type}-${i}`}>
                  {f.page_role === "parent" ? t.previewParent : t.previewChild}
                  {" – "}
                  {f.source_url ?? f.source_type}
                </li>
              ))}
            </ul>
          </div>
        )}
        {editing && (
          <div className="doc-item-editor" role="group" aria-label={t.itemEditorHeading(src.label)}>
            <div className="doc-item-editor-head">
              <h4>{t.itemEditorHeading(src.label)}</h4>
              <div className="dialog-actions">
                <button type="button" className="btn btn--secondary" disabled={!overridden} onClick={() => resetOverride(src.key)}>
                  {t.itemResetDefaults}
                </button>
                <button type="button" className="btn btn--primary" onClick={() => setEditingKey(null)}>
                  {t.itemEditClose}
                </button>
              </div>
            </div>
            <PackMetadataFields
              options={options}
              value={meta.pack}
              onChange={(pack) => setOverride(src.key, { ...meta, pack })}
              idPrefix={idPrefix}
              uiDefaultLang={uiDefaultLang}
              audienceRolesLabel={t.audienceRoles}
              selectAllGradesLabel={t.selectAllGrades}
            />
            <DocumentMetadataFields
              options={options}
              value={meta.doc}
              onChange={(doc) => setOverride(src.key, { ...meta, doc })}
              idPrefix={idPrefix}
              uiDefaultLang={uiDefaultLang}
            />
            {overridden && metaIncomplete(meta, options) && <p className="import-errors">{t.itemMetaIncomplete}</p>}
          </div>
        )}
      </li>
    );
  }

  const fileSources = sources.filter((s) => s.kind === "file");
  const urlSources = sources.filter((s) => s.kind === "url");
  const previewable = urlSources.filter((s) => !s.error && !metaIncomplete(metaFor(s.key), options));
  const showPreviewHint = urlSources.some((s) => !s.error) && urlSources.some((s) => !s.error && metaIncomplete(metaFor(s.key), options));

  return (
    <Dialog title={dialogTitle} onClose={handleClose} footer={footer} wide>
      {newVersionOf && (
        <div className="export-ready">
          <span>
            <strong>{t.newVersionFor}</strong> <span className="doc-cell-title">{newVersionOf.title}</span>
          </span>
          <span className="qa-muted">{t.newVersionHint}</span>
          {originalName && <span className="qa-muted">{t.newVersionFileName(originalName)}</span>}
        </div>
      )}

      {!newVersionOf && (
        <div role="tablist" aria-label={t.tabsLabel} className="doc-tabs">
          {(["files", "urls"] as const).map((key) => (
            <button
              key={key}
              ref={tabRefs[key]}
              type="button"
              role="tab"
              id={`doc-upload-tab-${key}`}
              aria-selected={tab === key}
              aria-controls={`doc-upload-panel-${key}`}
              tabIndex={tab === key ? 0 : -1}
              className={tab === key ? "doc-tab is-selected" : "doc-tab"}
              onClick={() => setTab(key)}
              onKeyDown={handleTabKey}
            >
              {key === "files" ? `${t.tabFiles} (${files.length})` : `${t.tabUrls} (${urlLines.length})`}
            </button>
          ))}
        </div>
      )}

      {!isWebVersion && (
        <div role="tabpanel" id="doc-upload-panel-files" aria-labelledby="doc-upload-tab-files" hidden={tab !== "files"}>
          <div className="doc-dropzone" onDragOver={(e) => e.preventDefault()} onDrop={handleDrop}>
            <label className="field">
              <span>{t.filesLabel}</span>
              <input
                type="file"
                multiple={!isFileVersion}
                accept={options.file_extensions.join(",")}
                onChange={(event) => {
                  addFiles(event.target.files);
                  event.target.value = "";
                }}
              />
            </label>
            <p className="qa-muted">{t.dropHint}</p>
            <p className="qa-muted">{t.allowedTemplate(options.file_extensions.join(", "), formatBytes(options.max_file_bytes))}</p>
          </div>
          {fileSources.length > 0 && <ul className="doc-file-list">{fileSources.map((src, i) => renderItem(src, i))}</ul>}
        </div>
      )}

      {!isFileVersion && (
        <div role="tabpanel" id="doc-upload-panel-urls" aria-labelledby="doc-upload-tab-urls" hidden={tab !== "urls"}>
          <div className="field">
            <label htmlFor="doc-upload-urls">{t.urlsLabel}</label>
            <textarea
              id="doc-upload-urls"
              rows={4}
              value={urlsText}
              readOnly={isWebVersion}
              placeholder={t.urlsPlaceholder}
              onChange={(e) => setUrlsText(e.target.value)}
            />
          </div>
          {urlLines.some((l) => l.error) && (
            <ul className="import-errors doc-field-errors">
              {urlLines
                .filter((l) => l.error)
                .map((l) => (
                  <li key={l.line}>{t.urlLineTemplate(l.line, l.url, l.error!)}</li>
                ))}
            </ul>
          )}
          <label className="checkbox-row">
            <input type="checkbox" checked={withChildren} onChange={(e) => setWithChildren(e.target.checked)} />
            <span>{t.childLinks}</span>
          </label>
          {withChildren && (
            <label className="field">
              <span>{t.childLinksCount(options.max_child_links)}</span>
              <input
                type="number"
                min={1}
                max={options.max_child_links}
                value={childLinks}
                onChange={(e) => setChildLinks(e.target.value)}
                aria-invalid={!childLinksValid}
              />
            </label>
          )}
          {!childLinksValid && <p className="import-errors">{t.errChildLinks(options.max_child_links)}</p>}
          {urlSources.length > 0 && (
            <>
              <div className="dialog-actions">
                <button
                  type="button"
                  className="btn btn--secondary"
                  disabled={previewable.length === 0}
                  onClick={() => void runPreviewAll(previewable)}
                >
                  {t.previewAll}
                </button>
              </div>
              {showPreviewHint && <p className="qa-muted">{t.previewNeedsMetadata}</p>}
              <ul className="doc-file-list">{urlSources.map((src, i) => renderItem(src, i))}</ul>
            </>
          )}
        </div>
      )}

      <h3>{t.metadataHeading}</h3>
      <p className="qa-muted">{t.metadataHint}</p>
      <PackMetadataFields
        options={options}
        value={packDraft}
        onChange={setPackDraft}
        idPrefix="doc-upload"
        uiDefaultLang={uiDefaultLang}
        audienceRolesLabel={t.audienceRoles}
        selectAllGradesLabel={t.selectAllGrades}
      />
      <DocumentMetadataFields
        options={options}
        value={docDraft}
        onChange={setDocDraft}
        idPrefix="doc-upload"
        uiDefaultLang={uiDefaultLang}
      />

      <p className="qa-muted">{t.summary(files.length, urlLines.length)}</p>
      {batchTooLarge && <p role="alert">{t.batchTooLarge(options.max_batch_items)}</p>}
      {missing.length > 0 && (
        <div className="qa-muted doc-missing">
          {t.missingHeading}
          <ul>
            {missing.map((m) => (
              <li key={m}>{m}</li>
            ))}
          </ul>
        </div>
      )}
    </Dialog>
  );
}
