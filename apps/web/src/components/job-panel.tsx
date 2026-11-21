"use client";

import { AlertTriangle, Ban, RotateCw } from "lucide-react";
import type { Job } from "@clipforge/shared";
import { formatBytes } from "@clipforge/shared";
import { Badge, ProgressBar, Spinner } from "@/components/ui";

const TYPE_LABEL: Record<string, string> = {
  analysis: "Finding moments",
  discovery: "Finding more moments",
  render: "Rendering clip",
  url_import: "Downloading source",
};

/** Worker stage ids, grouped by job type because ids are not globally unique. */
const STAGE_LABEL: Record<string, Record<string, string>> = {
  url_import: {
    validating: "Checking the link",
    inspecting: "Inspecting source",
    selecting_format: "Selecting available formats",
    downloading_audio: "Downloading audio",
    downloading_video: "Downloading video",
    verifying: "Verifying the file",
    preparing_audio: "Preparing audio",
    preparing_proxy: "Preparing proxy",
    waiting: "Waiting for the connection",
    done: "Ready to edit",
  },
  analysis: {
    prepare: "Reading source",
    audio: "Preparing audio",
    transcribe: "Transcribing",
    candidates: "Analysing scenes",
    rank: "Ranking moments",
    boundaries: "Reviewing clip endings",
    done: "Ready to edit",
  },
  discovery: {
    prepare: "Loading the existing analysis",
    discover: "Finding new moments",
    rank: "Ranking against the brief",
    dedupe: "Removing duplicates",
    boundaries: "Reviewing new endings",
    done: "Additional moments ready",
  },
  render: {
    assets: "Checking assets",
    compose: "Planning the layout",
    rendering: "Encoding video",
    validating: "Checking the output",
    done: "Ready to edit",
  },
};

const ACTIVE = new Set(["queued", "running", "waiting"]);

type StageDetail = {
  downloaded_bytes?: number;
  total_bytes?: number | null;
  speed_bps?: number | null;
  eta_seconds?: number | null;
  indeterminate?: boolean;
  percent?: number | null;
  slow?: boolean;
};

type DownloadSummary = {
  width?: number | null;
  height?: number | null;
  bytes_downloaded?: number;
  size_bytes?: number;
  elapsed_seconds?: number;
  speed_bps?: number | null;
  explanation?: string;
  warnings?: string[];
  reused?: boolean;
};

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Seconds, in the unit a person actually plans around. */
function formatEta(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`;
}

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
  const stageLabel = STAGE_LABEL[job.type]?.[job.stage];

  const metrics = record(job.metrics);
  const download = record(metrics.download) as DownloadSummary;
  const stages = record(metrics.stages);
  const live = record(stages[job.stage]) as StageDetail;
  const liveDownloaded = numberOrNull(live.downloaded_bytes);
  const liveTotal = numberOrNull(live.total_bytes);
  const liveSpeed = numberOrNull(live.speed_bps);
  const liveEta = numberOrNull(live.eta_seconds);
  const showLive = active && job.type === "url_import" && liveDownloaded !== null;
  const warnings = Array.isArray(download.warnings) ? download.warnings : [];
  const hasDownloadSummary =
    job.type === "url_import" &&
    (download.height !== undefined ||
      download.bytes_downloaded !== undefined ||
      download.size_bytes !== undefined ||
      Boolean(download.explanation));

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
          tone={
            failed ? "bad" : completed ? "good" : cancelled ? "neutral" : job.state === "waiting" ? "warn" : "ember"
          }
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

      {active && stageLabel ? (
        <p className="mt-1.5 flex items-center gap-1.5 text-[11px] uppercase tracking-[0.08em] text-mist-600">
          <span aria-hidden className="size-1 rounded-full bg-ember-400" />
          {stageLabel}
        </p>
      ) : null}

      <p className="mt-1.5 text-[12.5px] text-mist-400">
        {failed || cancelled ? (job.error_message ?? job.message) : job.message}
      </p>

      {showLive ? (
        <p className="numeric mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-mist-500">
          <span>
            {formatBytes(liveDownloaded ?? 0)}
            {liveTotal ? ` of ${formatBytes(liveTotal)}` : " downloaded"}
          </span>
          {live.percent !== null && live.percent !== undefined ? (
            <span className="text-mist-600">{live.percent.toFixed(1)}% of this stream</span>
          ) : null}
          {liveSpeed ? <span className="text-mist-600">{formatBytes(liveSpeed)}/s</span> : null}
          {liveEta ? <span className="text-mist-600">about {formatEta(liveEta)} left</span> : null}
          {!liveTotal && !liveEta ? <span className="text-mist-600">total unknown</span> : null}
        </p>
      ) : null}

      {showLive && live.slow ? (
        <p className="mt-1 flex items-center gap-1.5 text-[11px] text-signal-warn">
          <AlertTriangle className="size-3.5" strokeWidth={2} />
          The source is answering very slowly. Stop and retry with Fast preview if it does not recover.
        </p>
      ) : null}

      {active || completed ? (
        <div className="mt-2.5 flex items-center gap-2.5">
          <ProgressBar
            value={job.progress}
            tone={completed ? "good" : "ember"}
            className="h-1"
          />
          <span className="numeric shrink-0 text-right text-[11px] text-mist-600">
            overall {Math.round(job.progress)}%
          </span>
        </div>
      ) : null}

      {hasDownloadSummary ? (
        <div className="mt-2 space-y-1 rounded-lg border border-white/6 bg-black/20 px-2.5 py-2">
          <p className="numeric flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-mist-500">
            {download.height ? (
              <span>
                {download.width ?? "?"}×{download.height}
              </span>
            ) : null}
            {download.bytes_downloaded !== undefined ? (
              <span>{formatBytes(download.bytes_downloaded)} downloaded</span>
            ) : download.size_bytes !== undefined ? (
              <span>{formatBytes(download.size_bytes)} on disk</span>
            ) : null}
            {download.elapsed_seconds !== undefined ? (
              <span>{download.elapsed_seconds.toFixed(1)} s</span>
            ) : null}
            {download.reused ? <span className="text-mist-600">reused</span> : null}
          </p>
          {download.explanation ? (
            <p className="text-[11.5px] leading-snug text-mist-500">{download.explanation}</p>
          ) : null}
          {warnings.length ? (
            <ul className="space-y-0.5">
              {warnings.map((warning) => (
                <li key={warning} className="text-[11px] leading-snug text-ember-300/90">
                  {warning}
                </li>
              ))}
            </ul>
          ) : null}
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
