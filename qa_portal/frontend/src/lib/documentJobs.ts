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
