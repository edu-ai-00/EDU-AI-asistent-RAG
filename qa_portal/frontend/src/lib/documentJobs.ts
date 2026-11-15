import type { DocumentJob } from "../api/types";
import { strings } from "../strings.cs";

const CHUNK_WAIT_MS = 10 * 60 * 1000;
const t = strings.documentUpload;

export function isJobActive(job: DocumentJob): boolean {
  return job.status === "queued" || job.status === "processing";
}

/** §14.5: keep polling while a job runs, or while a fresh completion has no chunk count yet. */
export function jobNeedsPoll(job: DocumentJob, now: number): boolean {
  if (isJobActive(job)) return true;
  if (job.status !== "completed" || (job.stored_chunks ?? 0) > 0 || !job.finished_at) return false;
  return now - new Date(job.finished_at).getTime() < CHUNK_WAIT_MS;
}

/** How many active jobs of the list are before `job` (the queue runs one at a time, oldest first). */
export function queueAhead(jobs: DocumentJob[], job: DocumentJob): number {
  if (job.status !== "queued") return 0;
  return jobs.filter(
    (j) => j.job_id !== job.job_id && isJobActive(j) && (j.status === "processing" || j.created_at < job.created_at),
  ).length;
}

/** A short explanation under the state: the place in the queue, or how long it has run. */
export function jobDetail(job: DocumentJob, now: number, ahead?: number): string | null {
  if (job.status === "queued" && ahead !== undefined) return ahead > 0 ? t.jobAhead(ahead) : t.jobNext;
  if (job.status === "processing" && job.started_at) {
    const minutes = Math.floor((now - new Date(job.started_at).getTime()) / 60000);
    return minutes >= 1 ? t.jobRunningFor(minutes) : null;
  }
  return null;
}

export function jobText(job: DocumentJob, now: number): string {
  switch (job.status) {
    case "queued":
      return t.jobQueued;
    case "processing":
      return t.jobProcessing;
    case "failed":
      return t.jobFailed(job.error?.message ?? job.error?.code ?? strings.errors.generic);
    case "unknown":
      return t.jobUnknown;
    case "skipped":
      return t.jobSkipped;
    case "completed":
      if ((job.stored_chunks ?? 0) > 0) return t.jobCompletedChunks(job.stored_chunks!);
      return jobNeedsPoll(job, now) ? t.jobCompletedWaiting : t.jobCompletedNoChunks;
    default:
      return t.jobAccepted;
  }
}
