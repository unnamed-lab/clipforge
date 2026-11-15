"use client";

import { AlertTriangle, Ban, RotateCw } from "lucide-react";
import type { Job } from "@clipforge/shared";
import { Badge, ProgressBar, Spinner } from "@/components/ui";

const TYPE_LABEL: Record<string, string> = {
  analysis: "Finding moments",
  render: "Rendering clip",
  url_import: "Downloading source",
};

const ACTIVE = new Set(["queued", "running"]);

export function JobPanel({
  job,
  onCancel,
  onRetry,
  onDismiss,
  className,
}: {
  job: Job;
  onCancel?: (job: Job) => void;
  onRetry?: (job: Job) => void;
  onDismiss?: () => void;
  className?: string;
}) {
  const active = ACTIVE.has(job.state);
  const failed = job.state === "failed";
  const cancelled = job.state === "cancelled";
  const completed = job.state === "completed";

  return (
    <section
      className={`rounded-xl border px-4 py-3.5 ${
        failed
          ? "border-signal-bad/30 bg-signal-bad/[0.06]"
          : completed
            ? "border-signal-good/25 bg-signal-good/[0.05]"
            : "border-white/8 bg-ink-900"
      } ${className ?? ""}`}
    >
      <div className="flex flex-wrap items-center gap-2.5">
        <p className="flex items-center gap-2 text-[13px] font-medium text-mist-100">
          {active ? <Spinner className="text-ember-400" /> : null}
          {TYPE_LABEL[job.type] ?? job.type}
        </p>
        <Badge
          tone={failed ? "bad" : completed ? "good" : cancelled ? "neutral" : "ember"}
          className="h-5 px-2 text-[10.5px]"
        >
          {job.state}
        </Badge>
        {job.attempts > 1 ? (
          <span className="numeric text-[11px] text-mist-600">attempt {job.attempts}</span>
        ) : null}
        <div className="ml-auto flex items-center gap-1.5">
          {active && onCancel ? (
            <button
              type="button"
              className="btn btn-ghost btn-sm h-7 px-2 text-mist-400"
              onClick={() => onCancel(job)}
            >
              <Ban className="size-3.5" strokeWidth={2} />
              Stop
            </button>
          ) : null}
          {(failed || cancelled) && onRetry ? (
            <button type="button" className="btn btn-sm h-7 px-2" onClick={() => onRetry(job)}>
              <RotateCw className="size-3.5" strokeWidth={2} />
              Retry
            </button>
          ) : null}
          {completed && onDismiss ? (
            <button type="button" className="btn btn-ghost btn-sm h-7 px-2 text-mist-500" onClick={onDismiss}>
              Dismiss
            </button>
          ) : null}
        </div>
      </div>

      <p className="mt-1.5 text-[12.5px] text-mist-400">
        {failed || cancelled ? (job.error_message ?? job.message) : job.message}
      </p>

      {active || completed ? (
        <div className="mt-2.5 flex items-center gap-2.5">
          <ProgressBar
            value={job.progress}
            tone={completed ? "good" : "ember"}
            className="h-1"
          />
          <span className="numeric w-9 shrink-0 text-right text-[11px] text-mist-500">
            {Math.round(job.progress)}%
          </span>
        </div>
      ) : null}

      {failed && job.error_code ? (
        <p className="mt-2 flex items-center gap-1.5 text-[11px] text-signal-bad/80">
          <AlertTriangle className="size-3" strokeWidth={2} />
          {job.error_code}
        </p>
      ) : null}
    </section>
  );
}
