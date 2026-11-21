"use client";

import { useMemo, useState } from "react";
import {
  Check,
  ChevronDown,
  Circle,
  Loader2,
  RotateCcw,
  Sparkles,
} from "lucide-react";
import type {
  Capabilities,
  DiscoveryCriteria,
  DiscoveryState,
  Job,
  ProjectDetail,
} from "@clipforge/shared";
import { formatTimecode } from "@clipforge/shared";
import { apiJson, messageFrom } from "@/lib/api";
import { Alert, Badge } from "@/components/ui";
import { cn } from "@/lib/utils";

const ACTIVE_STATES = new Set(["queued", "running", "waiting"]);

const REASON_COPY: Record<string, string> = {
  duplicates_only:
    "Every new moment overlapped one you already have. This source may be exhausted for that type and range.",
  no_matching_moments:
    "No moment matched that type. Try a different type, widen the source range, or add a custom request.",
  no_matching_speaker:
    "No moment matched that speaker. Speaker labels only cover part of this transcript.",
  no_moments_in_range:
    "No complete moment could be built inside that source range. Widen the range or choose a different length.",
  below_quality_threshold:
    "Nothing cleared the project's quality bar. Lower the quality floor only if you are willing to review rougher moments.",
  no_candidates:
    "No new candidate could be built from the remaining transcript. This source is genuinely sparse.",
};

function list(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function seconds(value: string): number | null {
  if (!value.trim()) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? Math.round(parsed * 1000) : null;
}

export function DiscoveryCounts({
  state,
  displayed,
  selected,
}: {
  state: DiscoveryState | undefined;
  displayed: number;
  selected: number;
}) {
  const counts = state?.counts;
  const items = [
    { label: "discovered", value: counts?.total_discovered ?? 0 },
    { label: "displayed", value: displayed },
    { label: "selected", value: selected },
    { label: "kept", value: counts?.accepted ?? 0 },
    { label: "clips made", value: counts?.converted ?? 0 },
    { label: "batches", value: counts?.batches ?? 0 },
  ];
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
      {items.map((item) => (
        <span key={item.label} className="numeric text-[11.5px] text-mist-500">
          <span className="font-medium text-mist-200">{item.value}</span> {item.label}
        </span>
      ))}
      {state?.generating ? (
        <Badge tone="ember" className="h-5 px-2 text-[10.5px]">
          <Loader2 className="size-3 animate-spin" strokeWidth={2} />
          still generating
        </Badge>
      ) : null}
    </div>
  );
}

/** Source → transcript → analysis → discovery, driven only by stored state. */
export function DiscoveryStages({ detail }: { detail: ProjectDetail }) {
  const state = detail.discovery;
  const analysisJob = detail.jobs.find(
    (job) => job.type === "analysis" && ACTIVE_STATES.has(job.state),
  );
  const discoveryJob = detail.jobs.find(
    (job) => job.type === "discovery" && ACTIVE_STATES.has(job.state),
  );
  const activeJob = analysisJob ?? discoveryJob ?? null;
  const batches = state?.batches ?? [];
  const hasAdditional = batches.some((batch) => batch.kind === "additional");
  const total = state?.counts.total_discovered ?? detail.candidates.length;

  const stages = [
    {
      id: "source",
      label: "Source ready",
      done: Boolean(detail.source),
      active: false,
      hint: detail.source ? detail.source.original_name : "Import a video",
    },
    {
      id: "transcript",
      label: "Transcribed",
      done: Boolean(detail.transcript),
      active: Boolean(analysisJob && ["prepare", "audio", "transcribe"].includes(analysisJob.stage)),
      hint: detail.transcript ? `${detail.transcript.word_count} words` : "Waiting for speech-to-text",
    },
    {
      id: "analysis",
      label: "First moments",
      done: total > 0,
      active: Boolean(
        analysisJob && ["candidates", "rank", "boundaries"].includes(analysisJob.stage),
      ),
      hint: total > 0 ? `${total} discovered` : "No moments yet",
    },
    {
      id: "discovery",
      label: "Additional searches",
      done: hasAdditional,
      active: Boolean(discoveryJob),
      hint: hasAdditional
        ? `${batches.filter((batch) => batch.kind === "additional").length} batch(es)`
        : "Not run yet",
    },
    {
      id: "ready",
      label: "Ready to edit",
      done: total > 0 && !activeJob,
      active: false,
      hint: total > 0 && !activeJob ? "Pick a moment" : "Waiting",
    },
  ];

  return (
    <div className="space-y-2">
      <ol className="flex flex-wrap items-center gap-x-1 gap-y-2">
        {stages.map((stage, index) => (
          <li key={stage.id} className="flex items-center gap-1">
            <span
              className={cn(
                "flex items-center gap-1.5 rounded-full border px-2 py-1 text-[10.5px]",
                stage.done
                  ? "border-signal-good/30 bg-signal-good/10 text-signal-good"
                  : stage.active
                    ? "border-ember-500/40 bg-ember-500/10 text-ember-300"
                    : "border-white/8 bg-white/[0.02] text-mist-600",
              )}
              title={stage.hint}
            >
              {stage.active ? (
                <Loader2 className="size-3 animate-spin" strokeWidth={2} />
              ) : stage.done ? (
                <Check className="size-3" strokeWidth={2.6} />
              ) : (
                <Circle className="size-2.5" strokeWidth={2} />
              )}
              {stage.label}
            </span>
            {index < stages.length - 1 ? (
              <span aria-hidden className="h-px w-2 bg-white/10" />
            ) : null}
          </li>
        ))}
      </ol>
      {activeJob ? (
        <p className="text-[11px] text-mist-500">
          {activeJob.message}
          {activeJob.type === "discovery" && activeJob.stage
            ? ` · stage ${activeJob.stage}`
            : ""}
        </p>
      ) : null}
    </div>
  );
}

function CoverageStrip({ state }: { state: DiscoveryState | undefined }) {
  const coverage = state?.coverage;
  if (!coverage || !coverage.buckets.length) return null;
  const percent = Math.round(coverage.ratio * 100);
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2 text-[11px] text-mist-500">
        <span>
          <span className="numeric font-medium text-mist-200">{percent}%</span> of the source is
          covered by kept moments
        </span>
        <span className="numeric">
          {coverage.unexplored} never used · {coverage.thin} thin
        </span>
      </div>
      <div className="flex h-2 gap-px overflow-hidden rounded-full bg-white/8">
        {coverage.buckets.map((bucket) => (
          <span
            key={bucket.index}
            className={cn(
              "h-full flex-1",
              bucket.status === "covered"
                ? "bg-signal-good/70"
                : bucket.status === "thin"
                  ? "bg-ember-500/60"
                  : "bg-white/10",
            )}
            title={`${formatTimecode(bucket.start_ms)}–${formatTimecode(bucket.end_ms)} · ${bucket.candidates} moment(s) · ${bucket.status}`}
          />
        ))}
      </div>
      <p className="text-[10.5px] leading-snug text-mist-600">
        Coverage is measured on the source timeline. A section is explored when a moment actually
        covers part of it, not because a clip merely starts there.
      </p>
    </div>
  );
}

export function DiscoveryPanel({
  detail,
  capabilities,
  expanded,
  onToggle,
  onStarted,
}: {
  detail: ProjectDetail;
  capabilities: Capabilities | null;
  expanded: boolean;
  onToggle: () => void;
  onStarted: () => Promise<void>;
}) {
  const state = detail.discovery;
  const source = detail.source;
  const maxCount = capabilities?.discovery?.max_count ?? capabilities?.limits.max_moments_per_request ?? 30;
  const countChoices = useMemo(() => {
    const base = [5, 10, 20, 30].filter((value) => value <= maxCount);
    return base.length ? base : [maxCount];
  }, [maxCount]);

  const [count, setCount] = useState(countChoices[0] ?? 10);
  const [types, setTypes] = useState<string[]>([]);
  const [topics, setTopics] = useState("");
  const [keywords, setKeywords] = useState("");
  const [speaker, setSpeaker] = useState("");
  const [fromSec, setFromSec] = useState("");
  const [toSec, setToSec] = useState("");
  const [minSec, setMinSec] = useState("");
  const [maxSec, setMaxSec] = useState("");
  const [custom, setCustom] = useState("");
  const [diversity, setDiversity] = useState<DiscoveryCriteria["diversity"]>("balanced");
  const [revisit, setRevisit] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const discoveryJob = detail.jobs.find(
    (job) => job.type === "discovery" && ACTIVE_STATES.has(job.state),
  );
  const analysisJob = detail.jobs.find(
    (job) => job.type === "analysis" && ACTIVE_STATES.has(job.state),
  );
  const speakers = detail.transcript?.speakers ?? [];
  const momentTypes = capabilities?.discovery?.moment_types ?? [];
  const last = state?.last_result ?? null;
  const lastReason = last?.reason ? REASON_COPY[last.reason] : null;

  function toggleType(id: string) {
    setTypes((current) =>
      current.includes(id) ? current.filter((value) => value !== id) : [...current, id],
    );
  }

  function reset() {
    setCount(countChoices[0] ?? 10);
    setTypes([]);
    setTopics("");
    setKeywords("");
    setSpeaker("");
    setFromSec("");
    setToSec("");
    setMinSec("");
    setMaxSec("");
    setCustom("");
    setDiversity("balanced");
    setRevisit(true);
    setNotice(null);
    setError(null);
  }

  async function submit() {
    if (!source) return;
    setSubmitting(true);
    setError(null);
    setNotice(null);
    try {
      const body: DiscoveryCriteria = {
        count,
        moment_types: types,
        include_topics: list(topics),
        include_keywords: list(keywords),
        include_speakers: speaker ? [speaker] : [],
        source_start_ms: seconds(fromSec),
        source_end_ms: seconds(toSec),
        min_duration_ms: seconds(minSec),
        max_duration_ms: seconds(maxSec),
        custom_request: custom.trim(),
        min_quality: null,
        diversity,
        revisit_underexplored: revisit,
        exclude_existing: true,
      };
      const response = await apiJson<{
        job: Job;
        warnings: string[];
        deduplicated: boolean;
      }>(`/api/projects/${detail.project.id}/discovery`, { method: "POST", body });
      setNotice(
        response.deduplicated
          ? "That exact search is already running. Its results will appear here."
          : `Searching for up to ${count} more moment${count === 1 ? "" : "s"}. The existing transcript and moments are reused.`,
      );
      await onStarted();
    } catch (caught) {
      setError(messageFrom(caught));
    } finally {
      setSubmitting(false);
    }
  }

  const disabled = submitting || Boolean(analysisJob) || !source;

  return (
    <section className="surface px-4 py-3.5">
      <button
        type="button"
        className="flex w-full items-start justify-between gap-3 text-left"
        onClick={onToggle}
        aria-expanded={expanded}
      >
        <span className="min-w-0">
          <span className="flex items-center gap-1.5">
            <Sparkles className="size-3.5 text-ember-400" strokeWidth={2} />
            <span className="label">Find more moments</span>
          </span>
          <span className="mt-0.5 block text-[12.5px] text-mist-500">
            Search the same source again without transcribing or downloading it. Existing moments
            are kept.
          </span>
        </span>
        <ChevronDown
          className={cn(
            "mt-0.5 size-4 shrink-0 text-mist-500 transition-transform",
            expanded && "rotate-180",
          )}
          strokeWidth={2}
        />
      </button>

      <div className="mt-3 space-y-3">
        <DiscoveryStages detail={detail} />
        <CoverageStrip state={state} />

        {last ? (
          <div
            className={cn(
              "rounded-lg border px-3 py-2 text-[11.5px] leading-snug",
              (last.found ?? 0) > 0
                ? "border-signal-good/25 bg-signal-good/[0.06] text-mist-300"
                : "border-white/10 bg-black/20 text-mist-400",
            )}
          >
            <p>
              Last search: requested {last.requested ?? "?"}, found{" "}
              <span className="numeric font-medium text-mist-100">{last.found ?? 0}</span>.
              {last.exhausted ? " The source had nothing further for that request." : ""}
            </p>
            {lastReason ? <p className="mt-1 text-mist-500">{lastReason}</p> : null}
            {last.warnings.length ? (
              <ul className="mt-1 space-y-0.5">
                {last.warnings.slice(0, 3).map((warning) => (
                  <li key={warning} className="text-[11px] text-mist-500">
                    {warning}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}

        {notice ? <Alert tone="info">{notice}</Alert> : null}
        {error ? <Alert tone="bad">{error}</Alert> : null}
        {analysisJob ? (
          <p className="text-[11.5px] text-mist-500">
            Wait for the current analysis to finish. A targeted search reads the moments that
            analysis writes.
          </p>
        ) : null}

        {expanded ? (
          <div className="space-y-3 border-t border-white/6 pt-3">
            <div className="space-y-1.5">
              <span className="label">How many new moments?</span>
              <div className="flex flex-wrap items-center gap-1.5">
                {countChoices.map((value) => (
                  <button
                    key={value}
                    type="button"
                    className={cn(
                      "btn btn-sm h-7 min-w-9 px-2 text-[11.5px]",
                      count === value && "border-ember-500/50 text-ember-200",
                    )}
                    aria-pressed={count === value}
                    onClick={() => setCount(value)}
                  >
                    {value}
                  </button>
                ))}
                <label className="flex items-center gap-1 text-[11px] text-mist-500">
                  Custom
                  <input
                    className="field h-7 w-16 text-[12px]"
                    type="number"
                    min={1}
                    max={maxCount}
                    value={count}
                    onChange={(event) =>
                      setCount(Math.max(1, Math.min(maxCount, Number(event.target.value) || 1)))
                    }
                    aria-label="Custom number of new moments"
                  />
                </label>
              </div>
              <p className="text-[10.5px] text-mist-600">
                A request is not a guarantee. The actual number found is reported after the search,
                and existing moments are never regenerated.
              </p>
            </div>

            {momentTypes.length ? (
              <div className="space-y-1.5">
                <span className="label">Moment type</span>
                <div className="flex flex-wrap gap-1.5">
                  {momentTypes.map((type) => {
                    const active = types.includes(type.id);
                    return (
                      <button
                        key={type.id}
                        type="button"
                        className={cn(
                          "btn btn-sm h-7 px-2 text-[11px]",
                          active && "border-ember-500/50 text-ember-200",
                          !type.available && "opacity-45",
                        )}
                        aria-pressed={active}
                        disabled={!type.available}
                        title={type.evidence}
                        onClick={() => toggleType(type.id)}
                      >
                        {type.label}
                      </button>
                    );
                  })}
                </div>
                <p className="text-[10.5px] text-mist-600">
                  Types marked unavailable need evidence this pipeline does not produce yet, so they
                  are shown disabled rather than silently ignored.
                </p>
              </div>
            ) : null}

            <div className="grid gap-2 sm:grid-cols-2">
              <label className="space-y-1">
                <span className="label">Source range (seconds)</span>
                <div className="flex items-center gap-1">
                  <input
                    className="field h-8 w-full text-[12px]"
                    type="number"
                    min={0}
                    placeholder="from"
                    value={fromSec}
                    onChange={(event) => setFromSec(event.target.value)}
                    aria-label="Source range from in seconds"
                  />
                  <span className="text-[11px] text-mist-600">–</span>
                  <input
                    className="field h-8 w-full text-[12px]"
                    type="number"
                    min={0}
                    placeholder="to"
                    value={toSec}
                    onChange={(event) => setToSec(event.target.value)}
                    aria-label="Source range to in seconds"
                  />
                </div>
                <span className="text-[10.5px] text-mist-600">
                  Leave blank for the whole {source ? formatTimecode(source.duration_ms) : "source"}.
                </span>
              </label>

              <label className="space-y-1">
                <span className="label">Moment length (seconds)</span>
                <div className="flex items-center gap-1">
                  <input
                    className="field h-8 w-full text-[12px]"
                    type="number"
                    min={2}
                    placeholder="min"
                    value={minSec}
                    onChange={(event) => setMinSec(event.target.value)}
                    aria-label="Minimum moment length in seconds"
                  />
                  <span className="text-[11px] text-mist-600">–</span>
                  <input
                    className="field h-8 w-full text-[12px]"
                    type="number"
                    min={3}
                    placeholder="max"
                    value={maxSec}
                    onChange={(event) => setMaxSec(event.target.value)}
                    aria-label="Maximum moment length in seconds"
                  />
                </div>
                <span className="text-[10.5px] text-mist-600">
                  Optional. The project&apos;s saved duration preference applies otherwise.
                </span>
              </label>
            </div>

            {speakers.length ? (
              <label className="block space-y-1">
                <span className="label">Speaker</span>
                <select
                  className="field h-8 w-full text-[12px]"
                  value={speaker}
                  onChange={(event) => setSpeaker(event.target.value)}
                >
                  <option value="">Any speaker</option>
                  {speakers.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}

            <div className="grid gap-2 sm:grid-cols-2">
              <label className="space-y-1">
                <span className="label">Topics (comma separated)</span>
                <input
                  className="field h-8 w-full text-[12px]"
                  value={topics}
                  onChange={(event) => setTopics(event.target.value)}
                  placeholder="pricing, hiring"
                  maxLength={240}
                />
              </label>
              <label className="space-y-1">
                <span className="label">Keywords (comma separated)</span>
                <input
                  className="field h-8 w-full text-[12px]"
                  value={keywords}
                  onChange={(event) => setKeywords(event.target.value)}
                  placeholder="mistake, lesson"
                  maxLength={240}
                />
              </label>
            </div>

            <label className="block space-y-1">
              <span className="label">Custom request</span>
              <textarea
                className="field min-h-16 w-full text-[12px]"
                value={custom}
                onChange={(event) => setCustom(event.target.value)}
                placeholder="e.g. find the part where they explain why the launch failed"
                maxLength={600}
              />
            </label>

            <div className="flex flex-wrap items-end justify-between gap-2">
              <label className="space-y-1">
                <span className="label">Spread</span>
                <select
                  className="field h-8 text-[12px]"
                  value={diversity}
                  onChange={(event) =>
                    setDiversity(event.target.value as DiscoveryCriteria["diversity"])
                  }
                >
                  <option value="balanced">Balanced across the source</option>
                  <option value="chronological">Earliest first</option>
                  <option value="quality">Highest quality first</option>
                </select>
              </label>
              <label className="flex items-center gap-1.5 pb-1.5 text-[11.5px] text-mist-400">
                <input
                  type="checkbox"
                  className="size-3.5 accent-ember-500"
                  checked={revisit}
                  onChange={(event) => setRevisit(event.target.checked)}
                />
                Revisit underexplored sections
              </label>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={() => void submit()}
                disabled={disabled}
              >
                {submitting ? (
                  <Loader2 className="size-3.5 animate-spin" strokeWidth={2} />
                ) : (
                  <Sparkles className="size-3.5" strokeWidth={2} />
                )}
                {discoveryJob ? "Queue another search" : "Find more moments"}
              </button>
              <button type="button" className="btn btn-ghost btn-sm" onClick={reset}>
                <RotateCcw className="size-3.5" strokeWidth={2} />
                Reset filters
              </button>
              <span className="text-[10.5px] text-mist-600">
                Local search, sorting and filters below never call the model.
              </span>
            </div>
          </div>
        ) : null}

        {(state?.batches.length ?? 0) > 0 ? (
          <details className="rounded-lg border border-white/8 bg-black/20 px-3 py-2">
            <summary className="cursor-pointer text-[11.5px] font-medium text-mist-300">
              Search history ({state?.batches.length} batch
              {state?.batches.length === 1 ? "" : "es"})
            </summary>
            <ul className="mt-2 space-y-2">
              {state?.batches.map((batch) => (
                <li key={batch.id} className="border-t border-white/6 pt-2 first:border-0 first:pt-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge
                      tone={batch.kind === "initial" ? "neutral" : "ember"}
                      className="h-5 px-2 text-[10px]"
                    >
                      {batch.kind === "initial" ? "first analysis" : "additional search"}
                    </Badge>
                    <span className="numeric text-[11px] text-mist-400">
                      {batch.candidate_count} moment{batch.candidate_count === 1 ? "" : "s"}
                    </span>
                    {batch.requested !== null && batch.requested !== undefined ? (
                      <span className="numeric text-[11px] text-mist-600">
                        requested {batch.requested} · found {batch.found ?? 0}
                      </span>
                    ) : null}
                    <span className="ml-auto text-[10.5px] text-mist-600">
                      {new Date(batch.created_at).toLocaleString()}
                    </span>
                  </div>
                  {batch.criteria.moment_types?.length ? (
                    <p className="mt-1 text-[10.5px] text-mist-500">
                      Types: {batch.criteria.moment_types.join(", ").replace(/_/g, " ")}
                    </p>
                  ) : null}
                  {batch.reason ? (
                    <p className="mt-0.5 text-[10.5px] text-mist-500">
                      {REASON_COPY[batch.reason] ?? batch.reason}
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
          </details>
        ) : null}
      </div>
    </section>
  );
}
