import type { DocumentJob } from "../api/types";
import { jobText } from "../lib/documentJobs";
import { strings } from "../strings.cs";

/** The state of one ingest job as text; a job skipped for unchanged content (§14.14) is a neutral badge. */
export function JobStatus({ job, now }: { job: DocumentJob; now: number }) {
  if (job.status === "skipped") return <span className="badge badge-neutral">{strings.documentUpload.jobSkipped}</span>;
  return <>{jobText(job, now)}</>;
}
