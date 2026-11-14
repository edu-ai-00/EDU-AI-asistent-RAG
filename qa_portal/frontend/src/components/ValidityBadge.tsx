import type { DocumentSummary } from "../api/types";
import { formatDate } from "../lib/documentFormat";
import { strings } from "../strings.cs";

/** "Platnost vypršela" / "Platnost od …" from validity_state (§14.8); nothing when current. */
export function ValidityBadge({ doc }: { doc: Pick<DocumentSummary, "validity_state" | "valid_from"> }) {
  if (doc.validity_state === "expired") return <span className="badge badge-warn">{strings.documents.validityExpired}</span>;
  if (doc.validity_state === "future") {
    return <span className="badge badge-warn">{strings.documents.validityFuture(formatDate(doc.valid_from))}</span>;
  }
  return null;
}
