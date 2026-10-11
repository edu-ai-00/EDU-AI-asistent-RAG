import type { MetadataOptions } from "../api/types";
import { strings } from "../strings.cs";

/** Editable draft of PackMetadata (api/types.ts): visibility starts unset
 *  ("") so the caller can tell "not chosen yet" apart from a real choice —
 *  nothing here ever defaults silently (CONTRACT.md §2.4a/§2.4c). */
export type PackMetadataDraft = {
  visibility: "" | "entity" | "organization";
  audience_roles: string[];
  school_grade_codes: string[];
};

export const EMPTY_PACK_METADATA_DRAFT: PackMetadataDraft = {
  visibility: "",
  audience_roles: [],
  school_grade_codes: [],
};

type Props = {
  options: MetadataOptions;
  value: PackMetadataDraft;
  onChange: (next: PackMetadataDraft) => void;
  /** Keeps radio group names (and any other per-instance ids) unique when
   *  several instances render at once — one per new pack in a ZIP import. */
  idPrefix: string;
  /** The language these labels were requested in — named in the
   *  missing-translation hint so it cannot claim the wrong one. */
  uiDefaultLang: string;
};

function toggle(list: string[], code: string): string[] {
  return list.includes(code) ? list.filter((c) => c !== code) : [...list, code];
}

/** The visibility/audience_roles/school_grades controls shared by
 *  NewPackDialog (§2.4a) and ImportDialog (§2.4c) — extracted so a new pack
 *  confirmed from a dialog and one confirmed from an import file use exactly
 *  the same fields, labels and validation-relevant shape. */
export function PackMetadataFields({ options, value, onChange, idPrefix, uiDefaultLang }: Props) {
  return (
    <>
      <fieldset className="field">
        <legend>{strings.newPack.visibility}</legend>
        <label className="checkbox-row">
          <input
            type="radio"
            name={`${idPrefix}-visibility`}
            checked={value.visibility === "entity"}
            onChange={() => onChange({ ...value, visibility: "entity" })}
          />
          <span>{strings.newPack.visibilityEntity}</span>
        </label>
        <label className="checkbox-row">
          <input
            type="radio"
            name={`${idPrefix}-visibility`}
            checked={value.visibility === "organization"}
            onChange={() => onChange({ ...value, visibility: "organization" })}
          />
          <span>{strings.newPack.visibilityOrganization}</span>
        </label>
      </fieldset>

      <fieldset className="field">
        <legend>{strings.newPack.audienceRoles}</legend>
        <p className="qa-muted">{strings.newPack.audienceRolesHint}</p>
        {options.audience_roles.map((role) => (
          <label key={role.code} className="checkbox-row">
            <input
              type="checkbox"
              checked={value.audience_roles.includes(role.code)}
              onChange={() => onChange({ ...value, audience_roles: toggle(value.audience_roles, role.code) })}
            />
            <span>
              {role.label}
              {role.label_fallback ? ` ${strings.editor.missingTranslationHint(uiDefaultLang)}` : ""}
            </span>
          </label>
        ))}
      </fieldset>

      <fieldset className="field">
        <legend>{strings.newPack.grades}</legend>
        <p className="qa-muted">{strings.newPack.gradesHint}</p>
        {/* Dense multi-column layout (Job 1): one long column of grade
            checkboxes is what made the dialog too tall to fit a small
            screen in the first place. */}
        <div className="checkbox-grid">
          {options.school_grades.map((grade) => (
            <label key={grade.code} className="checkbox-row">
              <input
                type="checkbox"
                checked={value.school_grade_codes.includes(grade.code)}
                onChange={() => onChange({ ...value, school_grade_codes: toggle(value.school_grade_codes, grade.code) })}
              />
              <span>
                {grade.label}
                {grade.label_fallback ? ` ${strings.editor.missingTranslationHint(uiDefaultLang)}` : ""}
              </span>
            </label>
          ))}
        </div>
      </fieldset>
    </>
  );
}
