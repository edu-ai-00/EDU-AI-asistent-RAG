export type PageId = "qa" | "documents";

// Vite's base (/knowledge/ in the build) is the root of the SPA's own paths.
const rawBase = import.meta.env.BASE_URL || "/";
export const BASE_PATH = rawBase.endsWith("/") ? rawBase : `${rawBase}/`;

export function pathFor(page: PageId): string {
  return `${BASE_PATH}${page === "documents" ? "documents" : "qa"}`;
}

/** Legacy `#/documents` wins; unknown paths fall back to the QA page. */
export function pageFromLocation(loc: { pathname: string; hash: string }): PageId {
  if (loc.hash === "#/documents") return "documents";
  const rest = loc.pathname.startsWith(BASE_PATH) ? loc.pathname.slice(BASE_PATH.length) : loc.pathname.replace(/^\//, "");
  return rest.split("/")[0] === "documents" ? "documents" : "qa";
}
