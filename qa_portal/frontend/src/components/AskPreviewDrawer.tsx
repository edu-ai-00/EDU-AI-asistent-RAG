import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/types";
import type { AskPreviewResult, AskSource, DocumentOptions, School } from "../api/types";
import { useKeyedResource } from "../hooks/useKeyedResource";
import { strings } from "../strings.cs";
import { AskDebugPanel, type AskDebugPanelProps } from "./AskDebugPanel";
import { MarkdownPreview } from "./MarkdownPreview";

type Props = {
  client: ApiClient;
  school: School;
  open: boolean;
  onClose: () => void;
  /** Hooks of the debug panel into the rest of the portal; a panel action without its hook is hidden. */
  onOpenEntry?: AskDebugPanelProps["onOpenEntry"];
  onOpenDocument?: AskDebugPanelProps["onOpenDocument"];
  onCreateQa?: AskDebugPanelProps["onCreateQa"];
};

type FormState = { question: string; role: string; grade: string; retrievalOnly: boolean; debug: boolean };
type Run =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "done"; result: AskPreviewResult; form: FormState };
type HistoryItem = { id: number; form: FormState; result: AskPreviewResult };

const t = strings.ask;
const MAX_QUESTION = 2000;
const HISTORY_SIZE = 5;
const EMPTY_FORM: FormState = { question: "", role: "", grade: "", retrievalOnly: false, debug: true };

function sourceHref(source: AskSource): string | null {
  const raw = source.url ?? source.source_url ?? null;
  return raw && /^https?:\/\//i.test(raw) ? raw : null;
}

function errorText(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : t.errorHeading;
  const details = (error.fields ?? []).map((f) => f.message).filter(Boolean);
  return details.length > 0 ? `${error.message} (${details.join("; ")})` : error.message;
}

/** Right-side, non-modal drawer that asks the school's assistant (§14.9). The
 *  component stays mounted while closed, so the draft and the history survive. */
export function AskPreviewDrawer({ client, school, open, onClose, onOpenEntry, onOpenDocument, onCreateQa }: Props) {
  const titleId = useId();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [run, setRun] = useState<Run>({ status: "idle" });
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const requestRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const nextId = useRef(1);

  const { state: optionsState } = useKeyedResource<DocumentOptions>(open ? school.code : null, (code, signal) =>
    client.getDocumentOptions(code, undefined, signal),
  );
  const options = optionsState.status === "ready" ? optionsState.value : null;

  // Another school: drop the result and history, and ignore an answer still on its way.
  useEffect(() => {
    requestRef.current += 1;
    abortRef.current?.abort();
    setRun({ status: "idle" });
    setHistory([]);
    setForm((f) => ({ ...f, role: "", grade: "" }));
  }, [school.code]);

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    if (open) textareaRef.current?.focus();
  }, [open]);

  // The role must be one of the school's: default to its first once the options arrive.
  useEffect(() => {
    if (!options) return;
    setForm((f) => (options.audience_roles.some((r) => r.code === f.role) ? f : { ...f, role: options.audience_roles[0]?.code ?? "" }));
  }, [options]);

  useEffect(() => {
    if (!open) return;
    function onKey(event: globalThis.KeyboardEvent) {
      if (event.key !== "Escape") return;
      // A modal dialog on top owns Escape.
      if (document.querySelector('[role="dialog"][aria-modal="true"]')) return;
      event.preventDefault();
      onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Functional update: an effect (role default, school reset) may have changed the form since this render.
  function update(patch: Partial<FormState>) {
    setForm((f) => ({ ...f, ...patch }));
  }

  const loading = run.status === "loading";
  const canSubmit = form.question.trim() !== "" && form.role !== "" && !loading;

  async function submit() {
    if (!canSubmit) return;
    const id = ++requestRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const sent = form;
    setRun({ status: "loading" });
    try {
      const result = await client.askPreview(
        school.code,
        {
          question: sent.question.trim(),
          user_role: sent.role,
          user_school_grade: sent.grade === "" ? null : sent.grade,
          retrieval_only: sent.retrievalOnly,
          debug: sent.debug,
        },
        controller.signal,
      );
      if (id !== requestRef.current) return;
      setRun({ status: "done", result, form: sent });
      setHistory((prev) => [{ id: nextId.current++, form: sent, result }, ...prev].slice(0, HISTORY_SIZE));
    } catch (error) {
      if (id !== requestRef.current) return;
      setRun({ status: "error", message: errorText(error) });
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void submit();
  }

  function onQuestionKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      void submit();
    }
  }

  if (!open) return null;

  return (
    <aside className="drawer ask-drawer" role="dialog" aria-modal="false" aria-labelledby={titleId}>
      <div className="drawer-header">
        <h2 id={titleId}>{t.title}</h2>
        <button type="button" className="btn btn--secondary" onClick={onClose}>
          {t.close}
        </button>
      </div>
      <div className="drawer-body">
        <form className="ask-form" onSubmit={onSubmit}>
          <label className="field">
            <span>{t.question}</span>
            <textarea
              ref={textareaRef}
              rows={4}
              maxLength={MAX_QUESTION}
              value={form.question}
              placeholder={t.questionPlaceholder}
              onChange={(event) => update({ question: event.target.value })}
              onKeyDown={onQuestionKey}
            />
          </label>
          <span className="ask-count qa-muted" aria-hidden="true">
            {t.charCount(form.question.length, MAX_QUESTION)}
          </span>

          {optionsState.status === "loading" && <p role="status">{t.optionsLoading}</p>}
          {optionsState.status === "error" && <p role="alert">{`${t.optionsError} ${optionsState.message}`}</p>}
          <div className="ask-selects">
            <label className="field">
              <span>{t.role}</span>
              <select value={form.role} onChange={(event) => update({ role: event.target.value })}>
                {(options?.audience_roles ?? []).map((role) => (
                  <option key={role.code} value={role.code}>
                    {role.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t.grade}</span>
              <select value={form.grade} onChange={(event) => update({ grade: event.target.value })}>
                <option value="">{t.gradeNone}</option>
                {(options?.school_grades ?? []).map((grade) => (
                  <option key={grade.code} value={grade.code}>
                    {grade.label}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={form.retrievalOnly}
              onChange={(event) => update({ retrievalOnly: event.target.checked })}
            />
            {t.retrievalOnly}
          </label>
          <label className="checkbox-row">
            <input type="checkbox" checked={form.debug} onChange={(event) => update({ debug: event.target.checked })} />
            {strings.askDebug.toggle}
          </label>
          <p className="ask-note qa-muted">{strings.askDebug.toggleHint}</p>
          <p className="ask-note qa-muted">{t.costNote}</p>

          <div className="ask-actions">
            <button type="submit" className="btn btn--primary" disabled={!canSubmit} aria-busy={loading}>
              {loading ? t.submitting : t.submit}
            </button>
            <span className="qa-muted">{t.shortcutHint}</span>
          </div>
        </form>

        <div aria-live="polite">
          {run.status === "idle" && <p className="qa-muted">{t.empty}</p>}
          {run.status === "loading" && <p role="status">{t.loading}</p>}
          {run.status === "error" && (
            <div role="alert" className="ask-error">
              <strong>{t.errorHeading}</strong> {run.message}
            </div>
          )}
          {run.status === "done" && (
            <AskResult
              result={run.result}
              form={run.form}
              client={client}
              school={school.code}
              roles={options?.audience_roles ?? []}
              onOpenEntry={onOpenEntry}
              onOpenDocument={onOpenDocument}
              onCreateQa={onCreateQa}
            />
          )}
        </div>

        {history.length > 0 && (
          <section className="ask-history" aria-labelledby={`${titleId}-history`}>
            <h3 id={`${titleId}-history`}>{t.historyHeading}</h3>
            <p className="qa-muted">{t.historyHint}</p>
            <ul>
              {history.map((item) => (
                <li key={item.id}>
                  <button
                    type="button"
                    className="btn btn--ghost ask-history-item"
                    aria-label={t.historyItem(item.form.question)}
                    onClick={() => {
                      setForm(item.form);
                      setRun({ status: "done", result: item.result, form: item.form });
                    }}
                  >
                    <span className="ask-history-q">{item.form.question}</span>
                    <span className="chip">{label(t.outcomes, item.result.outcome)}</span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </aside>
  );
}

function label(map: Record<string, string>, key: string | null): string {
  return key === null ? "–" : (map[key] ?? key);
}

type AskResultProps = {
  result: AskPreviewResult;
  form: FormState;
  client: ApiClient;
  school: string;
  roles: NonNullable<AskDebugPanelProps["roles"]>;
} & Pick<Props, "onOpenEntry" | "onOpenDocument" | "onCreateQa">;

function AskResult({ result, form, client, school, roles, onOpenEntry, onOpenDocument, onCreateQa }: AskResultProps) {
  const hasDebug = result.trace != null || result.explain != null || result.effective_scope != null;
  const matched = t.matched(result.matched_entry_id, result.match_score);
  return (
    <div className="ask-result">
      <h3>{t.resultHeading}</h3>
      <div className="ask-chips">
        <span className="chip chip--accent">{t.chipOutcome(label(t.outcomes, result.outcome))}</span>
        <span className="chip">{t.chipSource(label(t.answerSources, result.answer_source))}</span>
        {result.delivered_by !== null && <span className="chip">{t.chipDelivered(result.delivered_by)}</span>}
        {result.retrieval_only && <span className="chip">{t.chipRetrievalOnly}</span>}
        {result.duration_ms !== null && <span className="chip">{t.duration(result.duration_ms)}</span>}
      </div>

      <h4>{t.answerHeading}</h4>
      {result.answer ? (
        <MarkdownPreview source={result.answer} />
      ) : (
        <p className="qa-muted">{result.retrieval_only ? t.answerSkipped : t.answerEmpty}</p>
      )}
      {matched !== "" && <p className="ask-match qa-muted">{matched}</p>}

      <h4>{t.sourcesHeading}</h4>
      {result.sources.length === 0 ? (
        <p className="qa-muted">{t.sourcesNone}</p>
      ) : (
        <ul className="ask-sources">
          {result.sources.map((source, index) => {
            const href = sourceHref(source);
            return (
              <li key={`${index}-${source.file_uuid ?? source.title ?? ""}`}>
                {href ? (
                  <a href={href} target="_blank" rel="noopener noreferrer">
                    {source.title || source.original_file_name || href}
                  </a>
                ) : (
                  <span>{source.original_file_name || source.title || "–"}</span>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {result.suggestions.length > 0 && (
        <>
          <h4>{t.suggestionsHeading}</h4>
          <ul className="ask-suggestions">
            {result.suggestions.map((suggestion, index) => (
              <li key={`${index}-${suggestion.question}`}>{suggestion.question}</li>
            ))}
          </ul>
        </>
      )}

      {hasDebug && (
        <AskDebugPanel
          client={client}
          school={school}
          result={result}
          asked={{ question: form.question.trim(), role: form.role, grade: form.grade }}
          roles={roles}
          onOpenEntry={onOpenEntry}
          onOpenDocument={onOpenDocument}
          onCreateQa={onCreateQa}
        />
      )}
    </div>
  );
}
