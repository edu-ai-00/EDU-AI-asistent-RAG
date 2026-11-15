import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { DocumentJob } from "../api/types";
import { JobStatus } from "../components/JobStatus";
import { jobDetail, queueAhead } from "../lib/documentJobs";
import { strings } from "../strings.cs";

afterEach(cleanup);
const t = strings.documentUpload;
const NOW = new Date("2026-11-15T10:00:00Z").getTime();

function job(over: Partial<DocumentJob>): DocumentJob {
  return {
    job_id: "j", batch_id: "b", school: "ZS", source_kind: "url", source: "https://zs.example/",
    status: "queued", created_at: "2026-11-15T09:00:00Z", started_at: null, finished_at: null,
    processing_uuid: null, result: null, stored_chunks: null, error: null, ...over,
  } as DocumentJob;
}

describe("job status details", () => {
  it("counts the running job and older queued ones as ahead in the queue", () => {
    const jobs = [
      job({ job_id: "run", status: "processing", created_at: "2026-11-15T09:05:00Z" }),
      job({ job_id: "q1", created_at: "2026-11-15T09:01:00Z" }),
      job({ job_id: "q2", created_at: "2026-11-15T09:02:00Z" }),
      job({ job_id: "done", status: "completed", created_at: "2026-11-15T08:00:00Z" }),
    ];
    expect(queueAhead(jobs, jobs[1])).toBe(1);
    expect(queueAhead(jobs, jobs[2])).toBe(2);
    expect(queueAhead(jobs, jobs[0])).toBe(0);
  });

  it("explains the queue position and a long run in plain words", () => {
    expect(jobDetail(job({}), NOW, 0)).toBe(t.jobNext);
    expect(jobDetail(job({}), NOW, 3)).toBe("před ní 3 položky");
    expect(jobDetail(job({}), NOW, 5)).toBe("před ní 5 položek");
    const running = job({ status: "processing", started_at: "2026-11-15T09:57:30Z" });
    expect(jobDetail(running, NOW)).toBe(t.jobRunningFor(2));
    expect(jobDetail(job({ status: "processing", started_at: "2026-11-15T09:59:40Z" }), NOW)).toBeNull();
  });

  it("declines the chunk count", () => {
    expect(t.jobCompletedChunks(1)).toBe("hotovo · uložen 1 chunk");
    expect(t.jobCompletedChunks(3)).toBe("hotovo · uloženy 3 chunky");
    expect(t.jobCompletedChunks(42)).toBe("hotovo · uloženo 42 chunků");
  });

  it("renders the state and its explanation as separate lines", () => {
    render(<JobStatus job={job({})} now={NOW} ahead={1} />);
    expect(screen.getByText(t.jobQueued)).toBeTruthy();
    expect(screen.getByText("před ní 1 položka")).toBeTruthy();
  });
});
