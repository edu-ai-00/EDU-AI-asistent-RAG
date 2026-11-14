import type { AskEffectiveScope, AskPreviewResult, AskTraceStep } from "../api/types";

/** Pure helpers behind the debug panel of "try a question" (CONTRACT.md §14.11). The trace is a
 *  debugging surface and its inner shape is not a contract, so every read here is defensive. */

export type SourceKind = "qa_exact" | "qa_lexical" | "qa_vector" | "documents" | "school" | "none" | "other";

export const SOURCE_ICONS: Record<SourceKind, string> = {
  qa_exact: "📋",
  qa_lexical: "🔤",
  qa_vector: "🧭",
  documents: "📄",
  school: "🏫",
  none: "–",
  other: "❔",
};

export const OUTCOME_ICONS: Record<string, string> = { answer: "✅", suggest: "💡", no_answer: "⛔" };

export const STATUS_ICONS: Record<string, string> = {
  ran: "✔️",
  skipped: "⏭️",
  disabled: "⏸️",
  error: "⚠️",
};

const DOCUMENT_STRATEGY = /^(rag|document_|retrieval)/;

/** Where the answer (or the offer) came from: `answer_source`, else the strategies that ran. */
export function sourceKindOf(result: Pick<AskPreviewResult, "answer_source" | "strategies">): SourceKind {
  switch (result.answer_source) {
    case "qa_exact":
    case "qa_lexical":
    case "qa_vector":
      return result.answer_source;
    case "rag":
      return "documents";
    case "meta":
      return "school";
    case "none":
    case "guard":
      return "none";
    default:
      break;
  }
  const strategies = result.strategies ?? [];
  if (strategies.some((s) => DOCUMENT_STRATEGY.test(s))) return "documents";
  const qa = strategies.find((s) => s === "qa_exact" || s === "qa_lexical" || s === "qa_vector");
  if (qa) return qa as SourceKind;
  return result.answer_source ? "other" : "none";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export type Concept = { c: string; w: number | null; hit: boolean };
export type NgramChip = { entry_id: string | null; score: number | null; min_sim: number | null };
export type QaLexicalDetail = {
  t_answer: number | null;
  t_suggest: number | null;
  concepts: Concept[];
  ngram_chip: NgramChip | null;
};

/** The `detail` of the qa_lexical row, or nulls/empties when the step left none. */
export function qaLexicalDetail(trace: AskTraceStep[] | null | undefined): QaLexicalDetail {
  const row = (trace ?? []).find((r) => r.step === "qa_lexical");
  const detail = row?.detail;
  const d = isRecord(detail) ? detail : {};
  const concepts: Concept[] = Array.isArray(d.concepts)
    ? d.concepts.filter(isRecord).map((x) => ({ c: String(x.c ?? ""), w: num(x.w), hit: x.hit === true }))
    : [];
  const chip = isRecord(d.ngram_chip)
    ? {
        entry_id: typeof d.ngram_chip.entry_id === "string" ? d.ngram_chip.entry_id : null,
        score: num(d.ngram_chip.score),
        min_sim: num(d.ngram_chip.min_sim),
      }
    : null;
  return { t_answer: num(d.t_answer), t_suggest: num(d.t_suggest), concepts, ngram_chip: chip };
}

/** Czech decimal comma, two places: 0.4812 -> "0,48". */
export function fmtScore(value: number): string {
  return value.toFixed(2).replace(".", ",");
}

export type Verdict = "answer" | "suggest" | "belowAnswer" | "belowSuggest";

/** One line on how the score met the two bars; null without both thresholds or a score. */
export function verdictOf(score: number | null, tAnswer: number | null, tSuggest: number | null, outcome: string | null): Verdict | null {
  if (score === null || tAnswer === null || tSuggest === null) return null;
  if (score >= tAnswer) return "answer";
  if (score >= tSuggest) return outcome === "suggest" ? "suggest" : "belowAnswer";
  return "belowSuggest";
}

export type ScopeDiff = { school: boolean; role: boolean; grade: boolean };

const same = (a: string | null | undefined, b: string | null | undefined) => (a ?? "").toLowerCase() === (b ?? "").toLowerCase();

/** What the search actually ran with vs what the admin asked for. */
export function scopeDiff(scope: AskEffectiveScope, asked: { school: string; role: string; grade: string }): ScopeDiff {
  return {
    school: !same(scope.legal_entity, asked.school),
    role: !same(scope.role, asked.role),
    grade: !same(scope.school_grade, asked.grade),
  };
}

export type CascadeView = { rows: AskTraceStep[]; hiddenCount: number };

/** Up to and including the row that stopped the walk, or everything with `all`. */
export function cascadeRows(trace: AskTraceStep[] | null | undefined, all: boolean): CascadeView {
  const rows = trace ?? [];
  const stop = rows.findIndex((r) => r.stopped_here === true);
  if (all || stop < 0) return { rows, hiddenCount: 0 };
  return { rows: rows.slice(0, stop + 1), hiddenCount: rows.length - stop - 1 };
}

/** Short, human check of a stored file format for a source that has no document row. */
export function formatFromSource(source: { original_file_name?: string | null; url?: string | null; source_url?: string | null }): string | null {
  const name = source.original_file_name ?? "";
  const ext = /\.([A-Za-z0-9]{2,5})$/.exec(name)?.[1];
  if (ext) return ext.toLowerCase();
  return /^https?:\/\//i.test(source.url ?? source.source_url ?? "") ? "web" : null;
}
