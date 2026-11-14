import type { ApiClient } from "../api/client";
import { ApiError } from "../api/types";
import { strings } from "../strings.cs";

/** File name from a Content-Disposition header (RFC 6266: filename* wins). */
export function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const star = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header)?.[1];
  if (star) {
    try {
      return decodeURIComponent(star.trim());
    } catch {
      // fall through to the plain form
    }
  }
  const plain = /filename\s*=\s*(?:"([^"]*)"|([^;]+))/i.exec(header);
  const name = (plain?.[1] ?? plain?.[2] ?? "").trim();
  return name || null;
}

/** Fetch→blob→object URL, so a 404 JSON never replaces the page (§14.10). */
export async function saveStoredFile(client: ApiClient, school: string, fileUuid: string): Promise<void> {
  const { blob, filename } = await client.downloadDocument(school, fileUuid);
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename ?? "";
  link.rel = "noopener";
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/** The message shown instead of navigating when the download fails. */
export function downloadErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 404) return strings.documentDownload.notStored;
  return strings.documentDownload.failed(error instanceof Error ? error.message : strings.errors.generic);
}
