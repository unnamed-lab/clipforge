import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { Job } from "@clipforge/shared";

import { JobPanel } from "../src/components/job-panel";

function waitingJob(overrides: Partial<Job> = {}): Job {
  return {
    id: "job_1",
    project_id: "project_1",
    type: "url_import",
    state: "waiting",
    stage: "waiting",
    progress: 43.2,
    message: "The connection dropped. Resuming automatically in 2 minutes.",
    error_code: null,
    error_message: null,
    attempts: 2,
    payload: { url: "https://example.com/video" },
    result: null,
    metrics: {},
    cancel_requested: false,
    created_at: "2026-01-01T00:00:00Z",
    started_at: "2026-01-01T00:00:00Z",
    finished_at: null,
    ...overrides,
  };
}

describe("JobPanel for a parked download", () => {
  it("shows it as alive with a stop action, not as a failure", () => {
    // when
    const markup = renderToStaticMarkup(
      <JobPanel job={waitingJob()} onCancel={() => {}} onRetry={() => {}} />,
    );

    // then
    expect(markup).toContain("Downloading source");
    expect(markup).toContain("waiting");
    expect(markup).toContain("Waiting for the connection");
    expect(markup).toContain("Resuming automatically in 2 minutes.");
    expect(markup).toContain("Stop");
    expect(markup).not.toContain("Retry");
    expect(markup).toContain("overall 43%");
  });

  it("offers a retry once the job has actually failed", () => {
    // given
    const markup = renderToStaticMarkup(
      <JobPanel
        job={waitingJob({
          state: "failed",
          stage: "failed",
          error_code: "url_download_failed",
          error_message: "The connection dropped.",
        })}
        onCancel={() => {}}
        onRetry={() => {}}
      />,
    );

    // then
    expect(markup).toContain("Retry");
    expect(markup).not.toContain("Stop");
    expect(markup).toContain("url_download_failed");
  });
});
