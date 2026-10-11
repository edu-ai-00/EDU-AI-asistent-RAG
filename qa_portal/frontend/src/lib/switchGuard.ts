/**
 * Pure decision for "the user is about to switch school/pack (or navigate
 * away) while a draft is unpublished". Takes `confirm` as a parameter so it
 * is testable without a real browser dialog (CONTRACT.md §10: switching
 * school/pack or closing the page "musí chránit před tichou ztrátou").
 */
export function guardSwitch<T>(params: {
  hasUnsavedChanges: boolean;
  current: T;
  next: T;
  confirm: (message: string) => boolean;
  message: string;
}): { proceed: boolean; value: T } {
  if (!params.hasUnsavedChanges) return { proceed: true, value: params.next };
  const ok = params.confirm(params.message);
  if (!ok) return { proceed: false, value: params.current };
  return { proceed: true, value: params.next };
}
