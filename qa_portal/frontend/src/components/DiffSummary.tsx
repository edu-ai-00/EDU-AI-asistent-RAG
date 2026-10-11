import type { Diff } from "../api/types";
import { strings } from "../strings.cs";

type Props = { diff: Diff };

/** Renders the four buckets a preview always returns (added / changed /
 *  unchanged / deactivated) plus the changed field names, so the admin can
 *  see exactly what a publish would do before confirming it. Purely
 *  presentational — never decides whether a diff is safe to publish. */
export function DiffSummary({ diff }: Props) {
  return (
    <div className="diff-summary">
      <ul className="diff-summary-counts">
        <li>
          {strings.importDialog.added}: <strong>{diff.added.length}</strong>
        </li>
        <li>
          {strings.importDialog.changed}: <strong>{diff.changed.length}</strong>
        </li>
        <li>
          {strings.importDialog.unchanged}: <strong>{diff.unchanged.length}</strong>
        </li>
        <li>
          {strings.importDialog.deactivated}: <strong>{diff.deactivated.length}</strong>
        </li>
      </ul>

      {diff.added.length > 0 && (
        <div className="diff-summary-group">
          <h4>{strings.importDialog.added}</h4>
          <ul>
            {diff.added.map((item) => (
              <li key={item.entry_key}>{item.canonical_q}</li>
            ))}
          </ul>
        </div>
      )}

      {diff.changed.length > 0 && (
        <div className="diff-summary-group">
          <h4>{strings.importDialog.changed}</h4>
          <ul>
            {diff.changed.map((item) => (
              <li key={item.entry_key}>
                {item.entry_key}
                <ul className="diff-summary-fields">
                  {item.fields.map((field) => (
                    <li key={field.field}>{field.field}</li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </div>
      )}

      {diff.deactivated.length > 0 && (
        <div className="diff-summary-group">
          <h4>{strings.importDialog.deactivated}</h4>
          <ul>
            {diff.deactivated.map((item) => (
              <li key={item.entry_key}>{item.canonical_q}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
