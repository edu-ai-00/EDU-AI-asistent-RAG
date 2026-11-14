export type MarkdownFormat = "bold" | "italic" | "bullets" | "link";

export type MarkdownEdit = { value: string; selectionStart: number; selectionEnd: number };

/** Applies one toolbar action to `value` for the selection [start, end) and returns the new
 *  text with the range to select afterwards. `placeholder` stands in for an empty selection. */
export function applyMarkdownFormat(value: string, start: number, end: number, kind: MarkdownFormat, placeholder: string): MarkdownEdit {
  const a = Math.max(0, Math.min(start, value.length));
  const b = Math.max(a, Math.min(end, value.length));
  const before = value.slice(0, a);
  const selected = value.slice(a, b);
  const after = value.slice(b);

  if (kind === "bold" || kind === "italic") {
    const mark = kind === "bold" ? "**" : "*";
    const text = selected || placeholder;
    return {
      value: `${before}${mark}${text}${mark}${after}`,
      selectionStart: a + mark.length,
      selectionEnd: a + mark.length + text.length,
    };
  }

  if (kind === "link") {
    const text = selected || placeholder;
    const url = "https://";
    const urlStart = a + 1 + text.length + 2;
    return { value: `${before}[${text}](${url})${after}`, selectionStart: urlStart, selectionEnd: urlStart + url.length };
  }

  // bullets: every line the selection touches becomes a list item
  const lineStart = value.lastIndexOf("\n", a - 1) + 1;
  const nextBreak = value.indexOf("\n", b);
  const lineEnd = nextBreak === -1 ? value.length : nextBreak;
  const block = value.slice(lineStart, lineEnd);
  const lines = block.trim() === "" ? [placeholder] : block.split("\n");
  const bullet = lines.map((line) => (/^\s*[-*]\s/.test(line) ? line : `- ${line}`)).join("\n");
  return {
    value: `${value.slice(0, lineStart)}${bullet}${value.slice(lineEnd)}`,
    selectionStart: lineStart,
    selectionEnd: lineStart + bullet.length,
  };
}
