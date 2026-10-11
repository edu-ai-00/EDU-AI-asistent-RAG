import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSanitize from "rehype-sanitize";
import { strings } from "../strings.cs";

/** Renders `answer_md` the same way the assistant would show it to a parent —
 *  never raw HTML. `rehype-sanitize` strips anything not on its safe default
 *  schema (script tags, event handlers, javascript: URLs) so a malicious
 *  CSV/import row cannot become executable markup in the admin's browser. */

// Fenced code blocks and inline code spans are literal text to a reader, so
// any "<...>" inside them is not the raw HTML that react-markdown drops —
// strip them before scanning, or `<div>` in a code sample would false-positive.
const FENCED_CODE_RE = /```[\s\S]*?```|~~~[\s\S]*?~~~/g;
const INLINE_CODE_RE = /(`+)[\s\S]*?\1/g;

// A tag name must start right after "<" (or "</"): this is what separates
// `<img onerror=...>` from prose like "a < b" or an email "<foo@bar.com>",
// where the character after "<" is not a valid tag-name start.
const HTML_TAG_RE = /<\/?[a-zA-Z][a-zA-Z0-9-]*(?:\s+[^<>]*)?\/?>/;

// CommonMark also treats comments, CDATA, declarations and processing
// instructions as raw HTML; react-markdown drops these silently too (e.g. a
// Word-pasted "<!--[if !supportLists]-->" conditional comment), so they must
// trip the same notice as a tag.
const HTML_OTHER_RE = /<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<!DOCTYPE[\s\S]*?>|<\?[\s\S]*?\?>/i;

/** True when `source` contains raw HTML that react-markdown (without
 *  rehype-raw) silently drops instead of rendering — the case this component
 *  must surface with a visible notice rather than an unexplained gap. */
export function containsUnrenderedHtml(source: string): boolean {
  const withoutCode = source.replace(FENCED_CODE_RE, " ").replace(INLINE_CODE_RE, " ");
  return HTML_TAG_RE.test(withoutCode) || HTML_OTHER_RE.test(withoutCode);
}

export function MarkdownPreview({ source }: { source: string }) {
  const htmlStripped = containsUnrenderedHtml(source);
  return (
    <div>
      <div className="markdown-preview">
        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>
          {source}
        </ReactMarkdown>
      </div>
      {htmlStripped && (
        <p className="markdown-preview-notice" data-testid="markdown-html-stripped">
          {strings.markdownPreview.htmlStripped}
        </p>
      )}
    </div>
  );
}
