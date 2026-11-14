// Display helpers for the documents page (Czech locale, no server data reinterpreted).

export function formatBytes(bytes: number): string {
  const fmt = (n: number) => n.toLocaleString("cs-CZ", { maximumFractionDigits: 1 });
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${fmt(bytes / 1024)} kB`;
  return `${fmt(bytes / (1024 * 1024))} MB`;
}

export function formatDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("cs-CZ", { dateStyle: "short", timeStyle: "short" });
}

/** Lower-cased extension including the dot, "" when there is none. */
export function fileExtension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot).toLowerCase() : "";
}

/** Absolute http(s) URL with a host — the only kind the portal accepts. */
export function isHttpUrl(value: string): boolean {
  try {
    const u = new URL(value);
    return (u.protocol === "http:" || u.protocol === "https:") && u.hostname !== "";
  } catch {
    return false;
  }
}

export function formatDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(`${iso}T00:00:00`);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString("cs-CZ");
}

/** Long URLs become `domain/…/last-segment`; the caller keeps the full value in `title`. */
export function shortenUrl(value: string, max = 60): string {
  if (value.length <= max) return value;
  try {
    const u = new URL(value);
    const segments = u.pathname.split("/").filter(Boolean);
    const clip = (s: string) => (s.length > 32 ? `${s.slice(0, 31)}…` : s);
    if (segments.length === 0) return u.host;
    if (segments.length === 1) return `${u.host}/${clip(segments[0])}`;
    return `${u.host}/…/${clip(segments[segments.length - 1])}`;
  } catch {
    return `${value.slice(0, max - 1)}…`;
  }
}

/** Display text for a document title: URLs get shortened, file names stay. */
export function shortTitle(title: string): string {
  return /^https?:\/\//i.test(title) ? shortenUrl(title) : title;
}
