// Environment-specific settings live here, nowhere else. Vite only exposes
// variables prefixed VITE_ via import.meta.env (set at build time, or in dev
// via qa_portal/frontend/.env.local — never committed).

/** Same-origin base per CONTRACT.md §1/§8. Configurable for local dev against
 *  a backend on a different port; never hardcode a school or environment. */
export const API_BASE_URL: string = (import.meta.env.VITE_QA_API_BASE as string | undefined) || "/qa-api/v1";

/** The backend (edu_ai_qa_admin) does not exist yet for this delivery
 *  (CONTRACT.md §10). Default true builds against the frozen fixtures in
 *  src/fixtures/; flip to "false" once the real API is reachable. */
export const USE_FIXTURES: boolean =
  (import.meta.env.VITE_QA_USE_FIXTURES as string | undefined) !== "false";

export const DEFAULT_UI_LANG = "cs";

/** Operation polling (CONTRACT.md §6). A real publish goes through the ingest
 *  gateway and takes seconds, so the window has to outlast it: 1.5 s × 120 is
 *  three minutes. Tests override this module — they must never be the reason
 *  production polls faster. */
export const PUBLISH_POLL_INTERVAL_MS = 1500;
export const PUBLISH_POLL_MAX_ATTEMPTS = 120;
