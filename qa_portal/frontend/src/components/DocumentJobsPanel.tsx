import { useCallback, useEffect, useRef, useState } from "react";
import type { ApiClient } from "../api/client";
import type { DocumentJob } from "../api/types";
import { DOCUMENT_JOBS_POLL_INTERVAL_MS } from "../config";
import { formatDateTime, shortenUrl } from "../lib/documentFormat";
import { isJobActive } from "../lib/documentJobs";
import { strings } from "../strings.cs";
import { JobStatus } from "./JobStatus";

type Props = {
  client: ApiClient;
  school: string;
  /** Bump to reload at once (an upload dialog just sent something). */
  refreshKey: number;
  /** A job that was queued/processing is not any more: the documents list may have changed. */
  onSettled: () => void;
};

const DAY_MS = 24 * 60 * 60 * 1000;
const t = strings.documentJobs;

function isRecent(job: DocumentJob, now: number): boolean {
  const at = new Date(job.finished_at ?? job.created_at).getTime();
  return Number.isNaN(at) ? false : now - at < DAY_MS;
}

/** "Probíhající nahrávání": the school's queued/processing jobs and those finished in the
 *  last 24 h. Polls only while something is queued or processing. */
export function DocumentJobsPanel({ client, school, refreshKey, onSettled }: Props) {
  const [jobs, setJobs] = useState<DocumentJob[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [tick, setTick] = useState(0);
  const activeIds = useRef<Set<string>>(new Set());
  const onSettledRef = useRef(onSettled);
  onSettledRef.current = onSettled;

  const load = useCallback(
    async (signal: AbortSignal) => {
      try {
        const list = await client.listDocumentJobs(school, undefined, signal);
        if (signal.aborted) return;
        const nowActive = new Set(list.filter(isJobActive).map((j) => j.job_id));
        const settled = [...activeIds.current].some((id) => !nowActive.has(id));
        activeIds.current = nowActive;
        setJobs(list);
        setFailed(false);
        if (settled) onSettledRef.current();
      } catch {
        if (!signal.aborted) setFailed(true);
      }
    },
    [client, school],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, refreshKey]);

  const hasActive = jobs?.some(isJobActive) ?? false;
  useEffect(() => {
    if (!hasActive) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      await load(controller.signal);
      if (!controller.signal.aborted) setTick((n) => n + 1);
    }, DOCUMENT_JOBS_POLL_INTERVAL_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [hasActive, tick, load]);

  const now = Date.now();
  const relevant = (jobs ?? []).filter((j) => isJobActive(j) || isRecent(j, now));
  if (relevant.length === 0) return null;
  const activeCount = relevant.filter(isJobActive).length;

  return (
    <section className="jobs-panel" aria-labelledby="documents-jobs-heading">
      <div className="jobs-panel-head">
        <h3 id="documents-jobs-heading">{t.heading}</h3>
        <span className="qa-muted">{t.summary(activeCount, relevant.length - activeCount)}</span>
        <button
          type="button"
          className="btn btn--ghost"
          aria-expanded={!collapsed}
          aria-controls="documents-jobs-body"
          onClick={() => setCollapsed((v) => !v)}
        >
          {collapsed ? t.expand : t.collapse}
        </button>
      </div>
      {failed && <p role="alert" className="qa-muted jobs-panel-body">{t.pollError}</p>}
      {!collapsed && (
        <div id="documents-jobs-body" className="jobs-panel-body doc-table-scroll">
          <table className="doc-table">
            <thead>
              <tr>
                <th scope="col">{t.colItem}</th>
                <th scope="col">{t.colState}</th>
                <th scope="col">{t.colTime}</th>
              </tr>
            </thead>
            <tbody>
              {relevant.map((job) => (
                <tr key={job.job_id} className={job.status === "failed" ? "doc-row-error" : undefined}>
                  <td className="doc-cell-title" title={job.source}>
                    {job.source_kind === "url" ? shortenUrl(job.source) : job.source}
                  </td>
                  <td>
                    <JobStatus job={job} now={now} />
                  </td>
                  <td>{formatDateTime(job.finished_at ?? job.started_at ?? job.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
