import type { Entry, Source } from "../api/types";

/** In-browser-memory copy of one question being edited. Never persisted —
 *  the brief forbids MVP from promising server-side draft storage. */
export type EntryDraft = {
  canonical_q: string;
  answer_md: string;
  variants: string[];
  sources: Source[];
  domains: string[];
  category_code: string | null;
  is_active: boolean;
};

/** All drafts open in this session, keyed by entry_key (or a temporary key
 *  for a not-yet-published new question). Switching which question is
 *  SELECTED never touches this map — that is what "switching question keeps
 *  its edits" means. */
export type DraftMap = Record<string, EntryDraft>;

export const NEW_ENTRY_PREFIX = "__new__:";

export function draftFromEntry(entry: Entry): EntryDraft {
  return {
    canonical_q: entry.canonical_q,
    answer_md: entry.answer_md,
    variants: entry.variants ? [...entry.variants] : [],
    sources: entry.sources ? entry.sources.map((s) => ({ ...s })) : [],
    domains: [...entry.domains],
    category_code: entry.category_code ?? null,
    is_active: entry.is_active,
  };
}

export function blankDraft(): EntryDraft {
  return {
    canonical_q: "",
    answer_md: "",
    variants: [],
    sources: [],
    domains: [],
    category_code: null,
    is_active: true,
  };
}

/** Opens `key` in the draft map from `base` if not already open. Existing
 *  drafts (including edits to OTHER keys) are left untouched. */
export function ensureDraft(drafts: DraftMap, key: string, base: EntryDraft): DraftMap {
  if (Object.prototype.hasOwnProperty.call(drafts, key)) return drafts;
  return { ...drafts, [key]: base };
}

export function setDraftField<K extends keyof EntryDraft>(
  drafts: DraftMap,
  key: string,
  field: K,
  value: EntryDraft[K],
): DraftMap {
  const current = drafts[key];
  if (!current) return drafts;
  return { ...drafts, [key]: { ...current, [field]: value } };
}

export function removeDraft(drafts: DraftMap, key: string): DraftMap {
  if (!Object.prototype.hasOwnProperty.call(drafts, key)) return drafts;
  const next = { ...drafts };
  delete next[key];
  return next;
}

function arraysEqual(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

function sourcesEqual(a: readonly Source[], b: readonly Source[]): boolean {
  return a.length === b.length && a.every((s, i) => s.title === b[i].title && s.url === b[i].url);
}

export function draftEqualsBaseline(draft: EntryDraft, base: EntryDraft): boolean {
  return (
    draft.canonical_q === base.canonical_q &&
    draft.answer_md === base.answer_md &&
    draft.category_code === base.category_code &&
    draft.is_active === base.is_active &&
    arraysEqual(draft.domains, base.domains) &&
    arraysEqual(draft.variants, base.variants) &&
    sourcesEqual(draft.sources, base.sources)
  );
}

/** True if ANY open draft still differs from its baseline (or is a new,
 *  never-published entry) — the single source of truth for "unpublished
 *  change" state and for the unsaved-changes guard. */
export function hasUnsavedChanges(drafts: DraftMap, baselines: Record<string, EntryDraft>): boolean {
  return Object.entries(drafts).some(([key, draft]) => {
    const base = baselines[key];
    if (!base) return true;
    return !draftEqualsBaseline(draft, base);
  });
}
