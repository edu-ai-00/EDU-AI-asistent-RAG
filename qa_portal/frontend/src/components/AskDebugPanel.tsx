import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import type { ApiClient } from "../api/client";
import type { AskPreviewResult, AskSource, CodeOption } from "../api/types";
import { useKeyedResource } from "../hooks/useKeyedResource";
import {
  OUTCOME_ICONS,
  SOURCE_ICONS,
  STATUS_ICONS,
  cascadeRows,
  fmtScore,
  formatFromSource,
  qaLexicalDetail,
  scopeDiff,
  sourceKindOf,
  verdictOf,
} from "../lib/askDebug";
import { formatChip } from "../lib/documentLabels";
import { strings } from "../strings.cs";
import { ValidityBadge } from "./ValidityBadge";

const t = strings.askDebug;

export type DebugAsked = { question: string; role: string; grade: string };

export type AskDebugPanelProps = {
  client: ApiClient;
  school: string;
  result: AskPreviewResult;
  asked: DebugAsked;
  /** The school's roles; "Porovnat role" asks once per role. */
  roles: CodeOption[];
  gradeLabels?: CodeOption[];
  /** "Otevřít v QA": switch to the QA page with this pack and entry selected. */
  onOpenEntry?: (packId: string, entryKey: string) => void;
  /** Open the document detail drawer for this file_uuid (a version). */
  onOpenDocument?: (fileUuid: string) => void;
  /** "Vytvořit QA otázku z tohoto dotazu": returns "no-pack" when no pack is selected. */
  onCreateQa?: (question: string) => "ok" | "no-pack";
};

/** ⓘ button: a one-sentence explanation as an inline note that toggles on click or Enter. */
export function InfoButton({ phrase, text }: { phrase: string; text: string }) {
  const [open, setOpen] = useState(false);
  const noteId = useId();
  function onKey(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === "Escape" && open) {
      event.stopPropagation();
      event.nativeEvent.stopImmediatePropagation();
      setOpen(false);
    }
  }
  return (
    <span className="dbg-info">
      <button
        type="button"
        className="dbg-info-btn"
        aria-label={t.infoLabel(phrase)}
        aria-expanded={open}
        aria-describedby={open ? noteId : undefined}
        title={text}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={onKey}
      >
        <span aria-hidden="true">ⓘ</span>
      </button>
      {open && (
        <span id={noteId} role="note" className="dbg-info-note">
          {text}
        </span>
      )}
    </span>
  );
}

function Row({ icon, phrase, info, children }: { icon: string; phrase: ReactNode; info: string; children?: ReactNode }) {
  const label = typeof phrase === "string" ? phrase : info;
  return (
    <li className="dbg-row">
      <div className="dbg-row-line">
        <span className="dbg-icon" aria-hidden="true">
          {icon}
        </span>
        <span className="dbg-phrase">{phrase}</span>
        <InfoButton phrase={label} text={info} />
      </div>
      {children}
    </li>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  const [open, setOpen] = useState(true);
  const bodyId = useId();
  return (
    <section className="dbg-section">
      <h4 className="dbg-section-head">
        <button type="button" className="dbg-section-toggle" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen((v) => !v)}>
          <span aria-hidden="true">{open ? "▾" : "▸"}</span> {title}
        </button>
      </h4>
      <div id={bodyId} hidden={!open} className="dbg-section-body">
        {children}
      </div>
    </section>
  );
}

function Legend() {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <div className="dbg-legend">
      <button type="button" className="btn btn--ghost" aria-expanded={open} aria-controls={id} onClick={() => setOpen((v) => !v)}>
        {open ? t.legendHide : t.legend}
      </button>
      {open && (
        <div id={id} className="dbg-legend-box" role="group" aria-label={t.legendHeading}>
          <strong>{t.legendHeading}</strong>
          <ul>
            {t.legendItems.map(([icon, text]) => (
              <li key={`${icon}-${text}`}>
                <span aria-hidden="true">{icon}</span> {text}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function pct(value: number): string {
  return `${Math.min(100, Math.max(0, value * 100)).toFixed(1)}%`;
}

function SummarySection({ result, asked, roles, school }: Pick<AskDebugPanelProps, "result" | "asked" | "roles" | "school">) {
  const kind = sourceKindOf(result);
  const outcome = result.outcome ?? null;
  const outcomeLabel = outcome === null ? "–" : (t.outcomes[outcome] ?? strings.ask.outcomes[outcome] ?? outcome);
  const detail = qaLexicalDetail(result.trace);
  const score = result.match_score;
  const verdict = verdictOf(score, detail.t_answer, detail.t_suggest, outcome);
  const scope = result.effective_scope ?? null;
  const roleLabel = (code: string | null | undefined) => roles.find((r) => r.code.toLowerCase() === (code ?? "").toLowerCase())?.label ?? code ?? t.scopeNone;
  const diff = scope ? scopeDiff(scope, { school, role: asked.role, grade: asked.grade }) : null;

  return (
    <ul className="dbg-rows">
      <Row icon={outcome ? (OUTCOME_ICONS[outcome] ?? "ℹ️") : "–"} phrase={t.rowOutcome(outcomeLabel)} info={t.info.outcome} />
      <Row icon={SOURCE_ICONS[kind]} phrase={t.rowSource(t.sourceKinds[kind] ?? result.answer_source ?? "–")} info={t.info.source} />
      {score !== null && (
        <Row icon="🎯" phrase={t.rowScore(fmtScore(score))} info={t.info.score}>
          <div
            className="dbg-bar"
            role="img"
            aria-label={t.scoreBarLabel(
              fmtScore(score),
              detail.t_suggest !== null ? fmtScore(detail.t_suggest) : null,
              detail.t_answer !== null ? fmtScore(detail.t_answer) : null,
            )}
          >
            <div className="dbg-bar-fill" style={{ width: pct(score) }} />
            {detail.t_suggest !== null && <span className="dbg-bar-marker is-suggest" style={{ left: pct(detail.t_suggest) }} />}
            {detail.t_answer !== null && <span className="dbg-bar-marker is-answer" style={{ left: pct(detail.t_answer) }} />}
          </div>
          {(detail.t_suggest !== null || detail.t_answer !== null) && (
            <p className="dbg-bar-legend qa-muted">
              {detail.t_suggest !== null && <span>{`${t.markerSuggest} ${fmtScore(detail.t_suggest)}`}</span>}
              {detail.t_answer !== null && <span>{`${t.markerAnswer} ${fmtScore(detail.t_answer)}`}</span>}
            </p>
          )}
          {verdict && (
            <p className="dbg-verdict">
              {verdict === "answer"
                ? t.verdictAnswer
                : verdict === "suggest"
                  ? t.verdictSuggest
                  : verdict === "belowAnswer"
                    ? t.verdictBelowAnswer
                    : t.verdictBelowSuggest}
            </p>
          )}
        </Row>
      )}
      {result.duration_ms != null && <Row icon="⏱" phrase={t.rowDuration(result.duration_ms)} info={t.info.duration} />}
      {result.language && <Row icon="🌐" phrase={t.rowLanguage(result.language)} info={t.info.language} />}
      {scope && diff && (
        <Row icon="🏫" phrase={t.rowScope} info={t.info.scope}>
          <ul className="dbg-scope" aria-label={t.rowScope}>
            <li className={diff.school ? "is-diff" : undefined}>
              <span aria-hidden="true">🏫</span> {scope.legal_entity ?? t.scopeNone}
              {diff.school && <em>{` (${t.scopeDiffers(t.scopeSchool)}; ${t.scopeAsked(school)})`}</em>}
            </li>
            <li className={diff.role ? "is-diff" : undefined}>
              <span aria-hidden="true">👤</span> {roleLabel(scope.role)}
              {diff.role && <em>{` (${t.scopeDiffers(t.scopeRole)}; ${t.scopeAsked(roleLabel(asked.role))})`}</em>}
            </li>
            <li className={diff.grade ? "is-diff" : undefined}>
              <span aria-hidden="true">🎓</span> {scope.school_grade ?? t.scopeNone}
              {diff.grade && <em>{` (${t.scopeDiffers(t.scopeGrade)}; ${t.scopeAsked(asked.grade || t.scopeNone)})`}</em>}
            </li>
          </ul>
        </Row>
      )}
    </ul>
  );
}

function UnderstandingSection({ result }: { result: AskPreviewResult }) {
  const explain = result.explain ?? null;
  const detail = qaLexicalDetail(result.trace);
  const tokens = explain?.query_lemma_tokens ?? [];
  const absent = explain?.query_absent_concepts ?? [];
  const keywords = result.key_words_used ?? [];
  const u = t.understanding;
  return (
    <>
      {explain === null && <p className="qa-muted">{u.explainMissing}</p>}
      <ul className="dbg-rows">
        <Row icon="🔑" phrase={u.tokens} info={t.info.tokens}>
          {tokens.length === 0 ? (
            <p className="qa-muted">{u.tokensNone}</p>
          ) : (
            <ul className="dbg-chips">
              {tokens.map((token, i) => (
                <li key={`${token}-${i}`} className="chip">
                  {token}
                </li>
              ))}
            </ul>
          )}
        </Row>
        <Row icon="🧩" phrase={u.concepts} info={t.info.concepts}>
          {detail.concepts.length === 0 ? (
            <p className="qa-muted">{u.conceptsNone}</p>
          ) : (
            <ul className="dbg-concepts">
              {detail.concepts.map((concept, i) => (
                <li key={`${concept.c}-${i}`} className={concept.hit ? "is-hit" : "is-miss"}>
                  <span role="img" aria-label={concept.hit ? u.conceptHit : u.conceptMiss}>
                    {concept.hit ? "✔" : "✖"}
                  </span>{" "}
                  <span className="dbg-concept">{concept.c.split("|").join(" / ")}</span>
                  {concept.w !== null && <span className="qa-muted">{` · ${u.conceptWeight(fmtScore(concept.w))}`}</span>}
                </li>
              ))}
            </ul>
          )}
        </Row>
        <Row icon="❓" phrase={u.absent} info={t.info.absent}>
          {explain !== null && absent.length === 0 ? (
            <p className="qa-muted">{u.absentNone}</p>
          ) : (
            <ul className="dbg-chips">
              {absent.map((word, i) => (
                <li key={`${word}-${i}`} className="chip chip--warn">
                  {word}
                </li>
              ))}
            </ul>
          )}
          {explain?.query_in_corpus != null && (
            <p className="qa-muted">
              {explain.query_in_corpus ? u.inCorpusYes : u.inCorpusNo}
              <InfoButton phrase={u.absent} text={t.info.inCorpus} />
            </p>
          )}
        </Row>
        {keywords.length > 0 && (
          <Row icon="🗝️" phrase={u.keywords} info={t.info.keywords}>
            <ul className="dbg-chips">
              {keywords.map((word, i) => (
                <li key={`${word}-${i}`} className="chip">
                  {word}
                </li>
              ))}
            </ul>
          </Row>
        )}
      </ul>
    </>
  );
}

function CascadeSection({ result }: { result: AskPreviewResult }) {
  const [showAll, setShowAll] = useState(false);
  const c = t.cascade;
  const { rows, hiddenCount } = cascadeRows(result.trace, showAll);
  const total = rows.length + hiddenCount;
  return (
    <>
      <p className="dbg-hint">
        <span className="qa-muted">{c.heading}</span>
        <InfoButton phrase={c.heading} text={t.info.cascade} />
      </p>
      {total === 0 && <p className="qa-muted">{c.none}</p>}
      <ol className="dbg-timeline">
        {rows.map((row, i) => {
          const status = row.status ?? "unknown";
          const decision = row.decision ?? null;
          const added = typeof row.pool_added === "number" && row.pool_added > 0 ? row.pool_added : null;
          return (
            <li key={`${row.step}-${i}`} className={`dbg-step is-${status}`}>
              <span className="dbg-step-icon" role="img" aria-label={c.statuses[status] ?? status}>
                {STATUS_ICONS[status] ?? "❔"}
              </span>
              <div className="dbg-step-body">
                <div className="dbg-step-line">
                  <strong>{c.steps[row.step] ?? row.step}</strong>
                  {typeof row.ms === "number" && <span className="chip">{c.ms(row.ms)}</span>}
                  {added !== null && <span className="chip chip--accent">{c.added(added)}</span>}
                  {row.stopped_here === true && (
                    <span role="img" aria-label={c.stopped} title={c.stopped}>
                      🏁
                    </span>
                  )}
                </div>
                {decision !== null && (
                  <div className="dbg-step-note">
                    {c.decisions[decision] ?? decision}
                    {c.decisions[decision] !== undefined && <code className="dbg-code">{decision}</code>}
                  </div>
                )}
                {row.note && <div className="dbg-step-note qa-muted">{row.note}</div>}
              </div>
            </li>
          );
        })}
      </ol>
      {(hiddenCount > 0 || showAll) && total > 0 && (
        <button type="button" className="btn btn--ghost" onClick={() => setShowAll((v) => !v)}>
          {showAll ? c.showPath : c.showAll}
        </button>
      )}
    </>
  );
}

function QaSection({
  client,
  school,
  result,
  onOpenEntry,
}: Pick<AskDebugPanelProps, "client" | "school" | "result" | "onOpenEntry">) {
  const q = t.qa;
  const matchedId = result.matched_entry_id;
  const detail = qaLexicalDetail(result.trace);
  const chip = detail.ngram_chip;
  const suggestions = result.suggestions ?? [];
  const { state } = useKeyedResource(matchedId ? `${school}::${matchedId}` : null, (_key, signal) =>
    client.getEntryById(school, matchedId!, signal),
  );
  return (
    <ul className="dbg-rows">
      {matchedId && (
        <Row icon="🏆" phrase={q.winner} info={t.info.qaWinner}>
          {state.status === "loading" && <p role="status" className="qa-muted">{q.loading}</p>}
          {state.status === "error" && <p className="qa-muted">{q.gone}</p>}
          {state.status === "ready" && (
            <div className="dbg-winner">
              <p>
                <strong>{state.value.canonical_q}</strong>
              </p>
              <p className="qa-muted">
                {[q.pack(state.value.pack_id), result.match_score != null ? q.winnerScore(fmtScore(result.match_score)) : null]
                  .filter(Boolean)
                  .join(" · ")}
                {!state.value.is_active && <span className="badge badge-inactive">{q.inactive}</span>}
              </p>
              {onOpenEntry && (
                <button type="button" className="btn btn--secondary" onClick={() => onOpenEntry(state.value.pack_id, state.value.entry_key)}>
                  {q.open}
                </button>
              )}
            </div>
          )}
        </Row>
      )}
      {chip && (
        <Row icon="🔠" phrase={chip.entry_id && chip.score !== null ? q.ngram(fmtScore(chip.score), fmtScore(chip.min_sim ?? 0)) : q.ngramNone} info={t.info.ngram} />
      )}
      {suggestions.length > 0 && (
        <Row icon="💡" phrase={q.suggestions} info={t.info.suggestions}>
          <ul className="dbg-list">
            {suggestions.map((s, i) => (
              <li key={`${i}-${s.question}`}>
                {s.question}
                {s.score != null && <span className="qa-muted">{` · ${q.suggestionScore(fmtScore(s.score))}`}</span>}
              </li>
            ))}
          </ul>
        </Row>
      )}
    </ul>
  );
}

function DocSourceRow({
  client,
  school,
  source,
  onOpenDocument,
}: { client: ApiClient; school: string; source: AskSource } & Pick<AskDebugPanelProps, "onOpenDocument">) {
  const d = t.docs;
  const fileUuid = source.file_uuid ?? null;
  const { state } = useKeyedResource(fileUuid ? `${school}::${fileUuid}` : null, (_key, signal) => client.getDocument(school, fileUuid!, signal));
  const doc = state.status === "ready" ? state.value.document : null;
  const format = doc?.file_format ?? formatFromSource(source);
  const title = source.title || source.original_file_name || doc?.title || "–";
  return (
    <li className="dbg-source">
      <span className="dbg-source-title">{title}</span>
      {format && <span className="chip">{formatChip(format)}</span>}
      {doc && (
        <>
          <span className={doc.is_active ? "badge badge-active" : "badge badge-inactive"}>{doc.is_active ? d.activeBadge : d.inactiveBadge}</span>
          <ValidityBadge doc={doc} />
        </>
      )}
      {state.status === "error" && <span className="qa-muted">{d.gone}</span>}
      {fileUuid && onOpenDocument && state.status !== "error" && (
        <button type="button" className="btn btn--ghost dbg-link" aria-label={`${d.open}: ${title}`} onClick={() => onOpenDocument(fileUuid)}>
          {d.open}
        </button>
      )}
    </li>
  );
}

function DocumentsSection({
  client,
  school,
  result,
  onOpenDocument,
}: Pick<AskDebugPanelProps, "client" | "school" | "result" | "onOpenDocument">) {
  const d = t.docs;
  const parts = result.answer_parts ?? [];
  return (
    <ul className="dbg-rows">
      {result.sources.length > 0 && (
        <Row icon="📄" phrase={d.sources} info={t.info.sources}>
          <ul className="dbg-list dbg-sources">
            {result.sources.map((source, i) => (
              <DocSourceRow key={`${i}-${source.file_uuid ?? source.title ?? ""}`} client={client} school={school} source={source} onOpenDocument={onOpenDocument} />
            ))}
          </ul>
        </Row>
      )}
      {parts.length > 0 && (
        <Row icon="🧱" phrase={d.parts} info={t.info.parts}>
          <ul className="dbg-list">
            {parts.map((part) => (
              <li key={part.index}>
                {`${d.part(part.index + 1)} ← `}
                {part.file_uuid && onOpenDocument ? (
                  <button type="button" className="btn btn--ghost dbg-link" onClick={() => onOpenDocument(part.file_uuid!)}>
                    {part.title || part.file_uuid}
                  </button>
                ) : (
                  <span>{part.title || d.unattributed}</span>
                )}
              </li>
            ))}
          </ul>
        </Row>
      )}
    </ul>
  );
}

type CompareState =
  | { status: "loading" }
  | { status: "done"; columns: Array<{ role: CodeOption; result: AskPreviewResult | null }> };

function CompareTable({ columns }: { columns: Extract<CompareState, { status: "done" }>["columns"] }) {
  const a = t.actions;
  const cell = (fn: (r: AskPreviewResult) => ReactNode, col: { result: AskPreviewResult | null }) =>
    col.result ? fn(col.result) : <span className="qa-muted">{a.compareFailed}</span>;
  const rows: Array<[string, (r: AskPreviewResult) => ReactNode]> = [
    [
      a.compareRowOutcome,
      (r) => {
        const o = r.outcome ?? null;
        return o === null ? "–" : `${OUTCOME_ICONS[o] ?? "ℹ️"} ${t.outcomes[o] ?? strings.ask.outcomes[o] ?? o}`;
      },
    ],
    [a.compareRowSource, (r) => `${SOURCE_ICONS[sourceKindOf(r)]} ${t.sourceKinds[sourceKindOf(r)] ?? r.answer_source ?? "–"}`],
    [a.compareRowScore, (r) => (r.match_score != null ? fmtScore(r.match_score) : "–")],
    [a.compareRowMatch, (r) => (r.matched_entry_id ? r.matched_entry_id.slice(0, 8) : "–")],
    [a.compareRowSources, (r) => String(r.sources.length)],
    [a.compareRowDuration, (r) => (r.duration_ms != null ? `${r.duration_ms} ms` : "–")],
  ];
  return (
    <div className="doc-table-scroll">
      <table className="doc-table dbg-compare">
        <caption className="qa-muted">{a.compareHeading}</caption>
        <thead>
          <tr>
            <td />
            {columns.map((col) => (
              <th key={col.role.code} scope="col">
                {col.role.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, fn]) => (
            <tr key={label}>
              <th scope="row">{label}</th>
              {columns.map((col) => (
                <td key={col.role.code}>{cell(fn, col)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ActionsSection({
  client,
  school,
  result,
  asked,
  roles,
  onCreateQa,
}: Pick<AskDebugPanelProps, "client" | "school" | "result" | "asked" | "roles" | "onCreateQa">) {
  const a = t.actions;
  const [notice, setNotice] = useState<string | null>(null);
  const [compare, setCompare] = useState<CompareState | null>(null);
  const mounted = useRef(true);
  useEffect(() => () => void (mounted.current = false), []);
  // A new answer makes the old comparison and notice stale.
  useEffect(() => {
    setNotice(null);
    setCompare(null);
  }, [result]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(JSON.stringify(result, null, 2));
      setNotice(a.copied);
    } catch {
      setNotice(a.copyFailed);
    }
  }

  async function runCompare() {
    if (roles.length === 0) {
      setNotice(a.compareNoRoles);
      return;
    }
    setNotice(null);
    setCompare({ status: "loading" });
    const columns = await Promise.all(
      roles.map(async (role) => {
        try {
          const res = await client.askPreview(school, {
            question: asked.question,
            user_role: role.code,
            user_school_grade: asked.grade === "" ? null : asked.grade,
            retrieval_only: true,
            debug: true,
          });
          return { role, result: res };
        } catch {
          return { role, result: null };
        }
      }),
    );
    if (mounted.current) setCompare({ status: "done", columns });
  }

  function createQa() {
    setNotice(onCreateQa?.(asked.question) === "no-pack" ? a.createQaNoPack : a.createQaDone);
  }

  return (
    <>
      <ul className="dbg-rows">
        <Row icon="📋" phrase={a.copy} info={t.info.copy}>
          <button type="button" className="btn btn--secondary" onClick={() => void copy()}>
            {a.copy}
          </button>
        </Row>
        <Row icon="👥" phrase={a.compare} info={t.info.compare}>
          <button type="button" className="btn btn--secondary" disabled={compare?.status === "loading"} onClick={() => void runCompare()}>
            {compare?.status === "loading" ? a.comparing : a.compare}
          </button>
        </Row>
        {onCreateQa && (
          <Row icon="➕" phrase={a.createQa} info={t.info.createQa}>
            <button type="button" className="btn btn--secondary" onClick={createQa}>
              {a.createQa}
            </button>
          </Row>
        )}
      </ul>
      {notice && (
        <p role="status" className="dbg-notice">
          {notice}
        </p>
      )}
      {compare?.status === "done" && <CompareTable columns={compare.columns} />}
    </>
  );
}

/** Debug panel of "Vyzkoušet otázku" (CONTRACT.md §14.11): summary, how the question was
 *  understood, the cascade walk, the winning QA entry, the documents and a few actions. */
export function AskDebugPanel(props: AskDebugPanelProps) {
  const { client, school, result, asked, roles } = props;
  const showQa = Boolean(result.matched_entry_id) || (result.suggestions ?? []).length > 0 || qaLexicalDetail(result.trace).ngram_chip !== null;
  const showDocs = result.sources.some((s) => s.file_uuid) || (result.answer_parts ?? []).length > 0;
  return (
    <section className="dbg-panel" aria-label={t.heading}>
      <div className="dbg-panel-head">
        <h3>{t.heading}</h3>
        <Legend />
      </div>
      <Section title={t.sections.summary}>
        <SummarySection school={school} result={result} asked={asked} roles={roles} />
      </Section>
      <Section title={t.sections.understanding}>
        <UnderstandingSection result={result} />
      </Section>
      <Section title={t.sections.cascade}>
        <CascadeSection result={result} />
      </Section>
      {showQa && (
        <Section title={t.sections.qa}>
          <QaSection client={client} school={school} result={result} onOpenEntry={props.onOpenEntry} />
        </Section>
      )}
      {showDocs && (
        <Section title={t.sections.documents}>
          <DocumentsSection client={client} school={school} result={result} onOpenDocument={props.onOpenDocument} />
        </Section>
      )}
      <Section title={t.sections.actions}>
        <ActionsSection client={client} school={school} result={result} asked={asked} roles={roles} onCreateQa={props.onCreateQa} />
      </Section>
    </section>
  );
}
