import type { CodeOption, DocumentSummary } from "../api/types";
import { strings } from "../strings.cs";

const t = strings.documents;

export function codeLabel(list: CodeOption[] | undefined, code: string | null): string {
  if (!code) return t.notSet;
  return list?.find((o) => o.code === code)?.label ?? code;
}

export function visibilityLabel(v: DocumentSummary["visibility"]): string {
  if (v === "entity") return t.visibilityEntity;
  if (v === "organization") return t.visibilityOrganization;
  return t.visibilityPrivate;
}

/** Short chip text for a stored file format (§14.10): "PDF", "DOCX", "Web". */
export function formatChip(format: string): string {
  return format === "web" ? t.formatChipWeb : format.toUpperCase();
}
