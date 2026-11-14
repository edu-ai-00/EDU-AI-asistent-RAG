import type { DocumentOptions } from "../api/types";
import { strings } from "../strings.cs";

/** Editable document-only metadata. "" in a select means "not chosen";
 *  `validity` starts "" so the administrator must pick one explicitly. */
export type DocumentMetadataDraft = {
  document_type: string;
  document_language: string;
  school_subject: string;
  domain_codes: string[];
  validity: "" | "none" | "custom" | `period:${string}`;
  valid_from: string;
  valid_to: string;
};

export const EMPTY_DOCUMENT_METADATA_DRAFT: DocumentMetadataDraft = {
  document_type: "",
  document_language: "",
  school_subject: "",
  domain_codes: [],
  validity: "",
  valid_from: "",
  valid_to: "",
};

/** Resolves the validity choice to dates, or an error message (null = ok). */
export function resolveValidity(
  draft: DocumentMetadataDraft,
  options: DocumentOptions,
): { valid_from: string | null; valid_to: string | null; error: string | null } {
  if (draft.validity === "none") return { valid_from: null, valid_to: null, error: null };
  if (draft.validity === "custom") {
    const from = draft.valid_from || null;
    const to = draft.valid_to || null;
    if (!from && !to) return { valid_from: null, valid_to: null, error: strings.documentMetadata.errCustomEmpty };
    if (from && to && from > to) return { valid_from: from, valid_to: to, error: strings.documentMetadata.errRange };
    return { valid_from: from, valid_to: to, error: null };
  }
  if (draft.validity.startsWith("period:")) {
    const id = draft.validity.slice("period:".length);
    const period = options.validity_periods.find((p) => String(p.id) === id);
    if (period) return { valid_from: period.valid_from, valid_to: period.valid_to, error: null };
  }
  return { valid_from: null, valid_to: null, error: null };
}

type Props = {
  options: DocumentOptions;
  value: DocumentMetadataDraft;
  onChange: (next: DocumentMetadataDraft) => void;
  idPrefix: string;
  uiDefaultLang: string;
};

function label(option: { label: string; label_fallback?: boolean }, lang: string): string {
  return option.label_fallback ? `${option.label} ${strings.editor.missingTranslationHint(lang)}` : option.label;
}

type ValidityProps = {
  options: DocumentOptions;
  value: DocumentMetadataDraft;
  onChange: (next: DocumentMetadataDraft) => void;
  idPrefix: string;
};

/** The validity select with its custom date range; shared by upload and edit dialogs. */
export function ValidityFields({ options, value, onChange, idPrefix }: ValidityProps) {
  const s = strings.documentMetadata;
  const validityError = resolveValidity(value, options).error;
  return (
    <>
      <div className="field">
        <label htmlFor={`${idPrefix}-validity`}>{s.validity}</label>
        <select
          id={`${idPrefix}-validity`}
          value={value.validity}
          onChange={(e) => onChange({ ...value, validity: e.target.value as DocumentMetadataDraft["validity"] })}
        >
          <option value="">{s.validityChoose}</option>
          <option value="none">{s.validityNone}</option>
          {options.validity_periods.map((p) => (
            <option key={String(p.id)} value={`period:${p.id}`}>
              {p.name}
              {p.is_default ? s.validityDefaultSuffix : ""}
            </option>
          ))}
          <option value="custom">{s.validityCustom}</option>
        </select>
      </div>

      {value.validity === "custom" && (
        <div className="date-range">
          <label className="field">
            <span>{s.validFrom}</span>
            <input
              type="date"
              value={value.valid_from}
              onChange={(e) => onChange({ ...value, valid_from: e.target.value })}
              aria-invalid={validityError !== null}
            />
          </label>
          <label className="field">
            <span>{s.validTo}</span>
            <input
              type="date"
              value={value.valid_to}
              onChange={(e) => onChange({ ...value, valid_to: e.target.value })}
              aria-invalid={validityError !== null}
            />
          </label>
        </div>
      )}
      {validityError && <p className="import-errors">{validityError}</p>}
    </>
  );
}

/** Type, language, subject, domains and validity for a document batch
 *  (spec.qa-portal-documents §6.1). Complements PackMetadataFields. */
export function DocumentMetadataFields({ options, value, onChange, idPrefix, uiDefaultLang }: Props) {
  const s = strings.documentMetadata;
  const toggleDomain = (code: string) =>
    onChange({
      ...value,
      domain_codes: value.domain_codes.includes(code)
        ? value.domain_codes.filter((c) => c !== code)
        : [...value.domain_codes, code],
    });

  return (
    <>
      <div className="field">
        <label htmlFor={`${idPrefix}-type`}>{s.documentType}</label>
        <select
          id={`${idPrefix}-type`}
          value={value.document_type}
          onChange={(e) => onChange({ ...value, document_type: e.target.value })}
        >
          <option value="">{s.documentTypeAuto}</option>
          {options.document_types.map((o) => (
            <option key={o.code} value={o.code}>
              {label(o, uiDefaultLang)}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor={`${idPrefix}-lang`}>{s.language}</label>
        <select
          id={`${idPrefix}-lang`}
          value={value.document_language}
          onChange={(e) => onChange({ ...value, document_language: e.target.value })}
        >
          <option value="">{s.languageAuto}</option>
          {options.languages.map((o) => (
            <option key={o.code} value={o.code}>
              {label(o, uiDefaultLang)}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor={`${idPrefix}-subject`}>{s.subject}</label>
        <select
          id={`${idPrefix}-subject`}
          value={value.school_subject}
          onChange={(e) => onChange({ ...value, school_subject: e.target.value })}
        >
          <option value="">{s.subjectNone}</option>
          {options.school_subjects.map((o) => (
            <option key={o.code} value={o.code}>
              {label(o, uiDefaultLang)}
            </option>
          ))}
        </select>
      </div>

      <fieldset className="field">
        <legend>{s.domains}</legend>
        {options.domains.length === 0 ? (
          <p className="qa-muted">{s.noDomains}</p>
        ) : (
          <>
            <p className="qa-muted">{s.domainsHint}</p>
            <div className="checkbox-grid">
              {options.domains.map((d) => (
                <label key={d.code} className="checkbox-row">
                  <input type="checkbox" checked={value.domain_codes.includes(d.code)} onChange={() => toggleDomain(d.code)} />
                  <span>{label(d, uiDefaultLang)}</span>
                </label>
              ))}
            </div>
          </>
        )}
      </fieldset>

      <ValidityFields options={options} value={value} onChange={onChange} idPrefix={idPrefix} />
    </>
  );
}

/** Maps stored dates back onto the validity select: none, a matching period, or custom. */
export function validityDraftFromDates(
  from: string | null,
  to: string | null,
  options: DocumentOptions,
): Pick<DocumentMetadataDraft, "validity" | "valid_from" | "valid_to"> {
  if (!from && !to) return { validity: "none", valid_from: "", valid_to: "" };
  const period = options.validity_periods.find((p) => p.valid_from === from && p.valid_to === to);
  if (period) return { validity: `period:${period.id}`, valid_from: "", valid_to: "" };
  return { validity: "custom", valid_from: from ?? "", valid_to: to ?? "" };
}
