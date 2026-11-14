import { useRef, useState } from "react";
import type { ApiClient } from "../api/client";
import type { Category, Domain, Entry, Source } from "../api/types";
import type { AsyncState } from "../hooks/useKeyedResource";
import type { EntryDraft } from "../lib/drafts";
import { strings } from "../strings.cs";
import { CategoryManager } from "./CategoryManager";
import { DeleteDialog } from "./DeleteDialog";
import { MarkdownPreview } from "./MarkdownPreview";
import { applyMarkdownFormat, type MarkdownFormat } from "../lib/markdownToolbar";

type Props = {
  entry: Entry | null;
  draft: EntryDraft | null;
  domainsState: AsyncState<Domain[]> | { status: "empty" };
  categoriesState: AsyncState<Category[]> | { status: "empty" };
  onChange: <K extends keyof EntryDraft>(field: K, value: EntryDraft[K]) => void;
  /** Category management (CONTRACT.md §4.3) is only offered when the
   *  context's capabilities say so — `client` is only used inside the
   *  CategoryManager dialog this component opens. */
  client: ApiClient;
  mayManageCategories: boolean;
  uiDefaultLang: string;
  /** Called once the server actually created a category — the caller
   *  refreshes the catalog and this editor selects the new code. */
  onCategoryCreated: (category: Category) => void;
  /** School/pack the currently selected entry belongs to — only used to
   *  address the permanent-delete API (CONTRACT.md §13); a brand-new,
   *  never-published entry has no server row yet so it offers no delete
   *  button (entry === null covers that case below). */
  school: string | null;
  pack: string | null;
  /** Called only once the server actually deleted the entry. */
  onEntryDeleted: () => void;
};

/** One simple editor: question/answer plus a collapsible section for the
 *  less-used fields (CONTRACT.md §10 / brief "jeden jednoduchý editor").
 *  Domains and the category come strictly from the school's/global catalog —
 *  never free text (CONTRACT.md §2.4) — so both render as controlled
 *  checkboxes/select bound to catalog codes, never as text inputs. */
export function EntryEditor({
  entry,
  draft,
  domainsState,
  categoriesState,
  onChange,
  client,
  mayManageCategories,
  uiDefaultLang,
  onCategoryCreated,
  school,
  pack,
  onEntryDeleted,
}: Props) {
  const [answerTab, setAnswerTab] = useState<"text" | "preview">("text");
  const answerRef = useRef<HTMLTextAreaElement>(null);
  const [managerOpen, setManagerOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  if (!draft) {
    return (
      <section className="entry-editor" aria-label={strings.editor.question}>
        <p className="qa-muted">{strings.editor.selectQuestionHint}</p>
      </section>
    );
  }

  // A brand-new, never-published question has no server entry yet — the
  // draft is still fully editable, it just cannot show a server entry_key.
  const isNewEntry = entry === null;

  function toggleDomain(code: string) {
    if (!draft) return;
    const next = draft.domains.includes(code) ? draft.domains.filter((d) => d !== code) : [...draft.domains, code];
    onChange("domains", next);
  }

  function updateSource(index: number, field: keyof Source, value: string) {
    if (!draft) return;
    const next = draft.sources.map((s, i) => (i === index ? { ...s, [field]: value } : s));
    onChange("sources", next);
  }

  function addSource() {
    if (!draft) return;
    onChange("sources", [...draft.sources, { title: "", url: "" }]);
  }

  function removeSource(index: number) {
    if (!draft) return;
    onChange(
      "sources",
      draft.sources.filter((_, i) => i !== index),
    );
  }

  function format(kind: MarkdownFormat) {
    const area = answerRef.current;
    if (!draft || !area) return;
    const placeholders = {
      bold: strings.editor.boldPlaceholder,
      italic: strings.editor.italicPlaceholder,
      bullets: strings.editor.bulletPlaceholder,
      link: strings.editor.linkPlaceholder,
    };
    const edit = applyMarkdownFormat(draft.answer_md, area.selectionStart ?? 0, area.selectionEnd ?? 0, kind, placeholders[kind]);
    onChange("answer_md", edit.value);
    // The textarea is controlled: restore focus and the selection once React wrote the new value.
    window.setTimeout(() => {
      area.focus();
      area.setSelectionRange(edit.selectionStart, edit.selectionEnd);
    }, 0);
  }

  function handleToggleActive() {
    if (!draft) return;
    if (draft.is_active) {
      // Deactivation goes through the normal preview/publish path (never a
      // separate delete call) and is reversible right up until publish.
      const confirmed = window.confirm(`${strings.deactivate.title}\n\n${strings.deactivate.body}`);
      if (!confirmed) return;
      onChange("is_active", false);
    } else {
      onChange("is_active", true);
    }
  }

  return (
    <section className="entry-editor" aria-label={strings.editor.question}>
      {isNewEntry && <p className="badge badge-new">{strings.editor.newEntryBadge}</p>}

      <label className="field">
        <span>{strings.editor.question}</span>
        <input type="text" value={draft.canonical_q} onChange={(event) => onChange("canonical_q", event.target.value)} />
      </label>

      <div className="field entry-editor-status">
        <span>{draft.is_active ? strings.editor.statusActive : strings.editor.statusInactive}</span>
        <button type="button" className="btn btn--secondary btn--sm" onClick={handleToggleActive}>
          {draft.is_active ? strings.actions.deactivate : strings.editor.reactivate}
        </button>
        {/* Permanent delete (CONTRACT.md §13) is a SEPARATE action from
            deactivation above – it needs a server row to delete, so it is
            offered only for an already-published entry, never a local draft. */}
        {!isNewEntry && entry && school !== null && pack !== null && (
          <button type="button" className="btn btn--danger btn--sm" onClick={() => setDeleteOpen(true)}>
            {strings.deletePermanent.deleteEntryButton}
          </button>
        )}
      </div>

      {deleteOpen && entry && school !== null && pack !== null && (
        <DeleteDialog
          client={client}
          target={{ kind: "entry", school, pack, entryKey: entry.entry_key, name: entry.canonical_q }}
          onClose={() => setDeleteOpen(false)}
          onDeleted={() => {
            setDeleteOpen(false);
            onEntryDeleted();
          }}
        />
      )}

      <div className="field">
        <div className="answer-head">
          <label htmlFor="entry-answer">{strings.editor.answer}</label>
          <div className="answer-tabs" role="tablist" aria-label={strings.editor.tabsLabel}>
            {(["text", "preview"] as const).map((tab) => (
              <button
                key={tab}
                type="button"
                role="tab"
                id={`answer-tab-${tab}`}
                aria-selected={answerTab === tab}
                aria-controls="answer-panel"
                className={answerTab === tab ? "btn btn--secondary is-current" : "btn btn--ghost"}
                onClick={() => setAnswerTab(tab)}
              >
                {tab === "text" ? strings.editor.tabText : strings.editor.tabPreview}
              </button>
            ))}
          </div>
        </div>
        <div id="answer-panel" className="answer-panel" role="tabpanel" aria-labelledby={`answer-tab-${answerTab}`}>
          {answerTab === "text" ? (
            <>
              <div className="answer-toolbar" role="toolbar" aria-label={strings.editor.toolbarLabel}>
                <button type="button" className="btn btn--ghost" aria-label={strings.editor.bold} title={strings.editor.bold} onClick={() => format("bold")}>
                  <strong aria-hidden="true">B</strong>
                </button>
                <button type="button" className="btn btn--ghost" aria-label={strings.editor.italic} title={strings.editor.italic} onClick={() => format("italic")}>
                  <em aria-hidden="true">I</em>
                </button>
                <button type="button" className="btn btn--ghost" aria-label={strings.editor.bullets} title={strings.editor.bullets} onClick={() => format("bullets")}>
                  <span aria-hidden="true">•</span>
                </button>
                <button type="button" className="btn btn--ghost" aria-label={strings.editor.link} title={strings.editor.link} onClick={() => format("link")}>
                  <span aria-hidden="true">🔗</span>
                </button>
              </div>
              <textarea
                ref={answerRef}
                id="entry-answer"
                rows={6}
                value={draft.answer_md}
                onChange={(event) => onChange("answer_md", event.target.value)}
              />
            </>
          ) : (
            <MarkdownPreview source={draft.answer_md} />
          )}
        </div>
      </div>

      <fieldset className="field entry-editor-areas">
        <legend>{strings.editor.areas}</legend>
        <p className="qa-muted">{strings.editor.areasHint}</p>
        {domainsState.status === "loading" && <p role="status">{strings.toolbar.loadingPacks}</p>}
        {domainsState.status === "error" && <p role="alert">{strings.toolbar.packsError}</p>}
        {domainsState.status === "ready" && domainsState.value.length === 0 && (
          <p className="qa-muted">{strings.editor.noAreasInSchool}</p>
        )}
        {domainsState.status === "ready" &&
          domainsState.value.map((domain) => (
            <label key={domain.code} className="checkbox-row">
              <input type="checkbox" checked={draft.domains.includes(domain.code)} onChange={() => toggleDomain(domain.code)} />
              <span>
                {domain.label}
                {domain.label_fallback ? ` ${strings.editor.missingTranslationHint(uiDefaultLang)}` : ""}
              </span>
            </label>
          ))}
      </fieldset>

      <p className="qa-muted">{strings.editor.metadataNote}</p>

      <details className="entry-editor-details">
        <summary>{strings.editor.detailsSummary}</summary>

        <label className="field">
          <span>{strings.editor.variants}</span>
          <textarea
            rows={3}
            value={draft.variants.join("\n")}
            onChange={(event) => onChange("variants", event.target.value.split("\n"))}
          />
        </label>

        <div className="field">
          <span>{strings.editor.sources}</span>
          {draft.sources.map((source, index) => (
            <div key={index} className="source-row">
              <input
                type="text"
                placeholder={strings.editor.sourceTitlePlaceholder}
                value={source.title}
                onChange={(event) => updateSource(index, "title", event.target.value)}
              />
              <input
                type="text"
                placeholder={strings.editor.sourceUrlPlaceholder}
                value={source.url}
                onChange={(event) => updateSource(index, "url", event.target.value)}
              />
              <button type="button" className="btn btn--ghost" onClick={() => removeSource(index)} aria-label={strings.editor.removeSource}>
                {strings.editor.removeSource}
              </button>
            </div>
          ))}
          <button type="button" className="btn btn--secondary" onClick={addSource}>
            {strings.editor.addSource}
          </button>
        </div>

        <label className="field">
          <span>{strings.editor.topic}</span>
          {categoriesState.status === "loading" && <span role="status">{strings.toolbar.loadingPacks}</span>}
          {categoriesState.status === "error" && <span role="alert">{strings.toolbar.packsError}</span>}
          {categoriesState.status === "ready" && (
            <select value={draft.category_code ?? ""} onChange={(event) => onChange("category_code", event.target.value || null)}>
              <option value="">{strings.editor.topicNone}</option>
              {categoriesState.value.map((category) => (
                <option key={category.code} value={category.code}>
                  {category.label}
                  {category.label_fallback ? ` ${strings.editor.missingTranslationHint(uiDefaultLang)}` : ""}
                  {!category.is_active ? ` – ${strings.editor.topicInactiveHint}` : ""}
                </option>
              ))}
            </select>
          )}
        </label>

        {mayManageCategories && (
          <button type="button" className="btn btn--secondary" onClick={() => setManagerOpen(true)}>
            {strings.editor.topicManage}
          </button>
        )}
      </details>

      {managerOpen && (
        <CategoryManager
          client={client}
          categoriesState={categoriesState}
          uiDefaultLang={uiDefaultLang}
          onClose={() => setManagerOpen(false)}
          onCreated={(category) => {
            onCategoryCreated(category);
            onChange("category_code", category.code);
            setManagerOpen(false);
          }}
        />
      )}
    </section>
  );
}
