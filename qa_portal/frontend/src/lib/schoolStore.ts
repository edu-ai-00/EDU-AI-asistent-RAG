const KEY = "edu-ai.qa-portal.school";

export function readStoredSchool(): string | null {
  try {
    return window.localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function storeSchool(code: string): void {
  try {
    window.localStorage.setItem(KEY, code);
  } catch {
    // Storage may be blocked; the selection then just lasts for this tab.
  }
}

/** The remembered school if it still exists, else the first one. */
export function pickInitialSchool(codes: string[]): string | null {
  const stored = readStoredSchool();
  if (stored !== null && codes.includes(stored)) return stored;
  return codes[0] ?? null;
}
