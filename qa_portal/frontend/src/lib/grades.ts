import type { CodeOption } from "../api/types";

/** The school-wide "all grades" option in the grade code list. */
export const ALL_GRADES_CODE = "9999";

/** Every real grade of the school; falls back to 9999 when that is all there is. */
export function allSchoolGradeCodes(grades: CodeOption[]): string[] {
  const real = grades.filter((g) => g.code !== ALL_GRADES_CODE).map((g) => g.code);
  if (real.length > 0) return real;
  return grades.some((g) => g.code === ALL_GRADES_CODE) ? [ALL_GRADES_CODE] : [];
}
