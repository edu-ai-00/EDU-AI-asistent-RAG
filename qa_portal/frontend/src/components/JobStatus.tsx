import type { DocumentJob } from "../api/types";
import { jobDetail, jobText } from "../lib/documentJobs";
import { strings } from "../strings.cs";

/** The state of one ingest job as text, with a short explanation under it (queue position,
 *  running time); a job skipped for unchanged content (§14.14) is a neutral badge. */
export function JobStatus({ job, now, ahead }: { job: DocumentJob; now: number; ahead?: number }) {
  if (job.status === "skipped") return <span className="badge badge-neutral">{strings.documentUpload.jobSkipped}</span>;
  const detail = jobDetail(job, now, ahead);
  return (
    <>
      <span>{jobText(job, now)}</span>
      {detail && <span className="doc-sub">{detail}</span>}
    </>
  );
}
