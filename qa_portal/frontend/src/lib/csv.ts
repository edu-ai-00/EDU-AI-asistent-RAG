// Minimal RFC 4180 (quoted multi-line fields) reader/writer. CONTRACT.md
// §5.1/§10: the REAL parser is the backend's ("CSV parsuje a generuje
// server"); this exists only so the fixture-mode mock adapter can show a
// plausible import preview without one. The live adapter never parses CSV
// client-side — it just uploads the file.
//
// Writing is always "," (contract §5.1). Reading accepts both "," and ";":
// when no delimiter is given, parseCsv detects it the same deterministic way
// the backend does — try the header as "," first, then ";", and pick
// whichever matches a known column set. Never guessed by character frequency.

/** New (with category_code) and legacy (without it) header shapes, both
 *  still valid input per CONTRACT.md §5.1. */
const NEW_HEADER_COLUMNS = ["entry_key", "canonical_q", "answer_md", "cat", "variants", "sources", "is_active", "domains", "category_code"];
const LEGACY_HEADER_COLUMNS = ["entry_key", "canonical_q", "answer_md", "cat", "variants", "sources", "is_active", "domains"];

function isKnownHeader(fields: string[]): boolean {
  const trimmed = fields.map((f) => f.trim());
  const matches = (columns: string[]) => trimmed.length === columns.length && columns.every((name, i) => trimmed[i] === name);
  return matches(NEW_HEADER_COLUMNS) || matches(LEGACY_HEADER_COLUMNS);
}

/** Deterministic delimiter detection for an uploaded CSV of unknown origin
 *  (CONTRACT.md §5.1): try "," then ";", keep whichever parses the header
 *  into a known column set. Falls back to "," when neither matches — the
 *  caller's own header validation then reports the real "unexpected header"
 *  error instead of this function silently guessing. */
export function detectCsvDelimiter(text: string): "," | ";" {
  if (isKnownHeader(parseCsv(text, ",")[0] ?? [])) return ",";
  if (isKnownHeader(parseCsv(text, ";")[0] ?? [])) return ";";
  return ",";
}

export function parseCsv(text: string, delimiter?: string): string[][] {
  const effectiveDelimiter = delimiter ?? detectCsvDelimiter(text);
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  const src = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;

  for (let i = 0; i < src.length; i++) {
    const c = src[i];
    if (inQuotes) {
      if (c === '"') {
        if (src[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        field += c;
      }
      continue;
    }
    if (c === '"') {
      inQuotes = true;
      continue;
    }
    if (c === effectiveDelimiter) {
      row.push(field);
      field = "";
      continue;
    }
    if (c === "\r") continue;
    if (c === "\n") {
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
      continue;
    }
    field += c;
  }
  if (field.length > 0 || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows.filter((r) => !(r.length === 1 && r[0] === ""));
}

function needsQuoting(value: string, delimiter: string): boolean {
  return value.includes(delimiter) || value.includes("\n") || value.includes('"') || value.includes("\r");
}

function quoteField(value: string, delimiter: string): string {
  if (!needsQuoting(value, delimiter)) return value;
  return `"${value.replace(/"/g, '""')}"`;
}

export function serializeCsv(rows: string[][], delimiter = ","): string {
  const body = rows.map((row) => row.map((cell) => quoteField(cell, delimiter)).join(delimiter)).join("\r\n");
  return `﻿${body}\r\n`;
}

export function rowsToRecords(rows: string[][]): Array<Record<string, string>> {
  if (rows.length === 0) return [];
  const [header, ...rest] = rows;
  return rest.map((row) => {
    const record: Record<string, string> = {};
    header.forEach((name, i) => {
      record[name] = row[i] ?? "";
    });
    return record;
  });
}
