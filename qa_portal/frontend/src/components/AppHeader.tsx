import type { Ref } from "react";
import type { School } from "../api/types";
import type { AsyncState } from "../hooks/useKeyedResource";
import { pathFor, type PageId } from "../lib/route";
import { strings } from "../strings.cs";

type Props = {
  page: PageId;
  onNavigate: (page: PageId) => void;
  schoolsState: AsyncState<School[]> | { status: "empty" };
  selectedSchool: string | null;
  onSelectSchool: (code: string) => void;
  /** "Vyzkoušet otázku" (§14.9): shown only when the school's permissions.publish is true. */
  askAvailable?: boolean;
  askOpen?: boolean;
  onOpenAsk?: () => void;
  askButtonRef?: Ref<HTMLButtonElement>;
};

/** Full-width header: logo + title, page switch, compact school selector, role badge. */
export function AppHeader({
  page,
  onNavigate,
  schoolsState,
  selectedSchool,
  onSelectSchool,
  askAvailable = false,
  askOpen = false,
  onOpenAsk,
  askButtonRef,
}: Props) {
  return (
    <header className="app-header">
      <div className="app-header-inner">
        <div className="app-brand">
          <span className="app-brand-mark" role="img" aria-label={strings.header.logoAlt}>
            EDU<small>AI</small>
          </span>
          <h1>{strings.header.title}</h1>
        </div>

        <nav className="page-nav" aria-label={strings.nav.label}>
          {(["qa", "documents"] as const).map((id) => (
            <a
              key={id}
              href={pathFor(id)}
              aria-current={page === id ? "page" : undefined}
              className={page === id ? "page-nav-link is-current" : "page-nav-link"}
              onClick={(event) => {
                event.preventDefault();
                onNavigate(id);
              }}
            >
              {id === "qa" ? strings.nav.qa : strings.nav.documents}
            </a>
          ))}
        </nav>

        <div className="app-header-tools">
          {askAvailable && (
            <button
              ref={askButtonRef}
              type="button"
              className="btn btn--secondary"
              aria-haspopup="dialog"
              aria-expanded={askOpen}
              onClick={onOpenAsk}
            >
              {strings.ask.open}
            </button>
          )}
          <div className="school-select">
            <label htmlFor="school-select">{strings.toolbar.school}</label>
            {schoolsState.status === "loading" && <span role="status">{strings.toolbar.loadingSchools}</span>}
            {schoolsState.status === "error" && <span role="alert">{strings.toolbar.schoolsError}</span>}
            {schoolsState.status === "ready" && (
              <select id="school-select" value={selectedSchool ?? ""} onChange={(event) => onSelectSchool(event.target.value)}>
                {schoolsState.value.map((school) => (
                  <option key={school.code} value={school.code}>
                    {school.name}
                  </option>
                ))}
              </select>
            )}
          </div>
          <span className="admin-badge">{strings.header.adminBadge}</span>
        </div>
      </div>
    </header>
  );
}
