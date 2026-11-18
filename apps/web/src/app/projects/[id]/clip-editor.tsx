"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  Pause,
  Play,
  Save,
  Scissors,
  SkipBack,
  SkipForward,
  Undo2,
  Volume2,
  VolumeX,
} from "lucide-react";
import type { Candidate, Capabilities, Render, Source, Transcript } from "@clipforge/shared";
import {
  clamp,
  formatDuration,
  formatTimecode,
  formatTimecodePrecise,
  scoreTone,
} from "@clipforge/shared";
import { Badge, ScoreMeter } from "@/components/ui";
import { EDITORIAL_STRATEGIES } from "@clipforge/shared";
import { RenderPanel } from "./render-panel";
import { PackagingPanel } from "./packaging-panel";
import { clipEndReached, clipStartReached, playbackStartMs } from "@/lib/preview-boundary";
import { cn } from "@/lib/utils";

const NUDGE_MS = 250;

export function ClipEditor({
  projectId,
  candidate,
  source,
  transcript,
  capabilities,
  hasWaveform,
  renders,
  onPatch,
  onBoundary,
  onEditorial,
  onCandidate,
  onRender,
  renderBusy,
}: {
  projectId: string;
  candidate: Candidate;
  source: Source;
  transcript: Transcript | null;
  capabilities: Capabilities | null;
  hasWaveform: boolean;
  renders: Render[];
  onPatch: (
    candidateId: string,
    patch: { start_ms?: number; end_ms?: number; title?: string; decision?: Candidate["decision"]; reset_edits?: boolean },
  ) => Promise<void>;
  onBoundary: (candidateId: string, action: { action: "accept" | "reject" | "reset"; end_ms?: number }) => Promise<void>;
  onEditorial: (
    candidateId: string,
    action: { strategy: string; pacing_profile?: string; apply?: boolean },
  ) => Promise<void>;
  onRender: (body: Record<string, unknown>) => Promise<void>;
  onCandidate: (candidate: Candidate) => void;
  renderBusy: boolean;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [currentMs, setCurrentMs] = useState(candidate.start_ms);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [loop, setLoop] = useState(false);
  const [start, setStart] = useState(candidate.start_ms);
  const [end, setEnd] = useState(candidate.end_ms);
  const [title, setTitle] = useState(candidate.title);
  const [saving, setSaving] = useState(false);
  const [showScores, setShowScores] = useState(true);
  const [notice, setNotice] = useState<string | null>(null);
  const [approveEditorial, setApproveEditorial] = useState(false);

  useEffect(() => {
    setStart(candidate.start_ms);
    setEnd(candidate.end_ms);
    setTitle(candidate.title);
    setCurrentMs(candidate.start_ms);
    const video = videoRef.current;
    if (video) {
      video.pause();
      setPlaying(false);
      video.currentTime = candidate.start_ms / 1000;
    }
  }, [candidate.id, candidate.start_ms, candidate.end_ms, candidate.title]);

  useEffect(() => {
    const video = videoRef.current;
    return () => {
      video?.pause();
    };
  }, [candidate.id]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const ms = video.currentTime * 1000;
    if (clipStartReached(ms, start) || clipEndReached(ms, end)) {
      video.pause();
      setPlaying(false);
      video.currentTime = start / 1000;
      setCurrentMs(start);
    }
  }, [start, end]);

  const dirty = start !== candidate.start_ms || end !== candidate.end_ms || title !== candidate.title;
  const durationMs = Math.max(0, end - start);
  const duration = source.duration_ms;
  const previewFollowsSourceOrder = Boolean(candidate.editorial?.editorial?.reordered);

  const seek = useCallback((ms: number) => {
    const video = videoRef.current;
    const next = clamp(ms, 0, duration);
    if (video) video.currentTime = next / 1000;
    setCurrentMs(next);
  }, [duration]);

  const onTimeUpdate = () => {
    const video = videoRef.current;
    if (!video) return;
    const ms = video.currentTime * 1000;

    // A paused player is also the trim scrubber: let it inspect source footage
    // outside the selection. Only an active playback is bounded by the clip.
    if (!video.paused && clipEndReached(ms, end)) {
      if (loop) {
        video.currentTime = start / 1000;
        setCurrentMs(start);
        return;
      }
      video.pause();
      video.currentTime = end / 1000;
      setCurrentMs(end);
      setPlaying(false);
      return;
    }
    if (!video.paused && clipStartReached(ms, start)) {
      video.currentTime = start / 1000;
      setCurrentMs(start);
      return;
    }
    setCurrentMs(Math.round(ms / 100) * 100);
  };

  const snapPoints = useMemo(() => collectBoundaries(transcript, start, end), [transcript, start, end]);

  const snapIn = () => {
    const target = nearestBoundary(transcript, start, "start");
    if (target !== null) setStart(clamp(target, 0, end - 1000));
  };
  const snapOut = () => {
    const target = nearestBoundary(transcript, end, "end");
    if (target !== null) setEnd(clamp(target, start + 1000, duration));
  };

  async function save() {
    setSaving(true);
    setNotice(null);
    try {
      await onPatch(candidate.id, { start_ms: start, end_ms: end, title });
      setNotice("Trim saved.");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Could not save the trim.");
    } finally {
      setSaving(false);
      window.setTimeout(() => setNotice(null), 3200);
    }
  }

  async function render(body: Record<string, unknown>) {
    if (dirty) {
      await onPatch(candidate.id, { start_ms: start, end_ms: end, title });
    }
    await onRender({
      ...body,
      candidate_id: candidate.id,
      title: title || candidate.title,
      approve_editorial: approveEditorial,
    });
  }

  const tone = scoreTone(candidate.overall);
  const components = Object.entries(candidate.scores.components ?? {}).sort((a, b) => {
    const weightA = candidate.scores.weights?.[a[0]] ?? 0;
    const weightB = candidate.scores.weights?.[b[0]] ?? 0;
    return weightB - weightA;
  });
  const candidateRenders = renders.filter((renderItem) => renderItem.candidate_id === candidate.id);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex items-center gap-2">
            <Badge tone={tone === "high" ? "good" : tone === "mid" ? "ember" : "neutral"}>
              {tone === "high" ? "Strong candidate" : tone === "mid" ? "Worth a look" : "Weak signal"}
            </Badge>
            <span className="numeric text-[11.5px] text-mist-500">
              rank {candidate.rank} · score {Math.round(candidate.overall)}/100
            </span>
            {candidate.decision !== "proposed" ? (
              <Badge tone={candidate.decision === "accepted" ? "good" : "bad"}>{candidate.decision}</Badge>
            ) : null}
          </div>
          <input
            className="field h-9 max-w-xl border-transparent bg-transparent px-0 text-[17px] font-semibold text-mist-50 hover:border-white/10 hover:px-2 focus:px-2"
            value={title}
            maxLength={120}
            onChange={(event) => setTitle(event.target.value)}
            aria-label="Clip title"
          />
          <p className="max-w-2xl text-[12.5px] leading-relaxed text-mist-400">{candidate.reason}</p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => onPatch(candidate.id, { decision: candidate.decision === "accepted" ? "proposed" : "accepted" })}
          >
            {candidate.decision === "accepted" ? "Unkeep" : "Keep"}
          </button>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => onPatch(candidate.id, { decision: candidate.decision === "rejected" ? "proposed" : "rejected" })}
          >
            {candidate.decision === "rejected" ? "Restore" : "Reject"}
          </button>
        </div>
      </div>

      <div className="overflow-hidden rounded-xl border border-white/8 bg-black">
        <video
          ref={videoRef}
          src={`/api/projects/${projectId}/media/stream#t=${(candidate.start_ms / 1000).toFixed(2)}`}
          preload="metadata"
          className="aspect-video max-h-[420px] w-full bg-black object-contain"
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onTimeUpdate={onTimeUpdate}
          onLoadedMetadata={() => seek(start)}
          muted={muted}
          playsInline
          controls={false}
          onEnded={() => setPlaying(false)}
        />
        {previewFollowsSourceOrder ? (
          <p className="border-t border-white/8 px-2.5 py-1.5 text-[11px] leading-relaxed text-mist-600">
            Preview follows source order. The approved editorial timeline is applied only in the export.
          </p>
        ) : null}
        <div className="flex flex-wrap items-center gap-2 border-t border-white/8 bg-ink-900 px-2.5 py-2">
          <button
            type="button"
            className="btn btn-ghost btn-sm h-7 px-2"
            onClick={() => {
              const video = videoRef.current;
              if (!video) return;
              if (video.paused) {
                const next = playbackStartMs(video.currentTime * 1000, start, end);
                if (next !== video.currentTime * 1000) seek(next);
                void video.play();
              } else {
                video.pause();
              }
            }}
            aria-label={playing ? "Pause" : "Play selection"}
          >
            {playing ? <Pause className="size-4" strokeWidth={2} /> : <Play className="size-4" strokeWidth={2} />}
          </button>
          <button type="button" className="btn btn-ghost btn-sm h-7 px-2" onClick={() => seek(start)} aria-label="Jump to in point">
            <SkipBack className="size-3.5" strokeWidth={2} />
          </button>
          <button type="button" className="btn btn-ghost btn-sm h-7 px-2" onClick={() => seek(end - 1000)} aria-label="Jump to out point">
            <SkipForward className="size-3.5" strokeWidth={2} />
          </button>
          <span className="numeric text-[11.5px] text-mist-400">
            {formatTimecodePrecise(currentMs)} / {formatTimecode(end)}
          </span>
          <button
            type="button"
            className={cn("btn btn-ghost btn-sm h-7 px-2", loop && "text-ember-400")}
            onClick={() => setLoop((value) => !value)}
            aria-pressed={loop}
            title="Loop the selected range"
          >
            Loop
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm h-7 px-2"
            onClick={() => setMuted((value) => !value)}
            aria-label={muted ? "Unmute" : "Mute"}
          >
            {muted ? <VolumeX className="size-3.5" strokeWidth={2} /> : <Volume2 className="size-3.5" strokeWidth={2} />}
          </button>
          <span className="ml-auto numeric text-[11.5px] text-mist-500">
            selection {formatDuration(durationMs)}
          </span>
        </div>
      </div>

      <section className="surface space-y-3 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="label">Trim</p>
          <div className="flex items-center gap-1.5">
            <button type="button" className="btn btn-sm h-7 px-2" onClick={snapIn} disabled={!transcript}>
              Snap in to speech
            </button>
            <button type="button" className="btn btn-sm h-7 px-2" onClick={snapOut} disabled={!transcript}>
              Snap out
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm h-7 px-2"
              onClick={() => onPatch(candidate.id, { reset_edits: true })}
              disabled={!candidate.edited}
              title="Restore the original boundaries"
            >
              <Undo2 className="size-3.5" strokeWidth={2} />
              Reset
            </button>
          </div>
        </div>

        <Timeline
          duration={duration}
          start={start}
          end={end}
          current={currentMs}
          hasWaveform={hasWaveform}
          projectId={projectId}
          snapPoints={snapPoints}
          onScrub={seek}
        />

        <div className="grid gap-3 sm:grid-cols-2">
          <BoundaryControl
            label="In"
            value={start}
            min={0}
            max={Math.max(0, end - 1000)}
            onChange={(value) => {
              setStart(value);
              seek(value);
            }}
            onNudge={(delta) => setStart((value) => clamp(value + delta, 0, end - 1000))}
            onSetHere={() => setStart(Math.min(currentMs, end - 1000))}
          />
          <BoundaryControl
            label="Out"
            value={end}
            min={Math.min(duration, start + 1000)}
            max={duration}
            onChange={(value) => {
              setEnd(value);
              seek(value - 300);
            }}
            onNudge={(delta) => setEnd((value) => clamp(value + delta, start + 1000, duration))}
            onSetHere={() => setEnd(Math.max(currentMs, start + 1000))}
          />
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn btn-sm" onClick={save} disabled={!dirty || saving}>
            <Save className="size-3.5" strokeWidth={2} />
            {saving ? "Saving" : dirty ? "Save trim" : "Saved"}
          </button>
          <p className="text-[11.5px] text-mist-600">
            Boundaries are validated against the source duration and stored per project.
          </p>
          {notice ? <span className="text-[11.5px] text-signal-info">{notice}</span> : null}
        </div>
      </section>

      <EndingReview
        candidate={candidate}
        onUseEnd={(ms) => setEnd(clamp(ms, start + 1000, duration))}
        onBoundary={onBoundary}
      />

      <EditorialPlan
        candidate={candidate}
        currentDurationMs={durationMs}
        approveEditorial={approveEditorial}
        onApproveEditorial={setApproveEditorial}
        onEditorial={onEditorial}
      />

      <PackagingPanel
        key={candidate.id}
        projectId={projectId}
        candidate={candidate}
        onCandidate={onCandidate}
      />

      <TranscriptWindow
        transcript={transcript}
        start={start}
        end={end}
        currentMs={currentMs}
        onSeek={seek}
        onUseStart={(ms) => setStart(clamp(ms, 0, end - 1000))}
        onUseEnd={(ms) => setEnd(clamp(ms, start + 1000, duration))}
      />

      {candidate.warnings.length > 0 ? (
        <section className="rounded-lg border border-signal-warn/25 bg-signal-warn/[0.05] px-3.5 py-2.5">
          <p className="text-[12px] font-medium text-signal-warn">Review notes</p>
          <ul className="mt-1 space-y-0.5 text-[12px] leading-relaxed text-mist-300">
            {candidate.warnings.map((warning) => (
              <li key={warning}>· {warning}</li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="surface space-y-3 p-4">
        <button
          type="button"
          className="flex w-full items-center justify-between gap-2 text-left"
          onClick={() => setShowScores((value) => !value)}
          aria-expanded={showScores}
        >
          <span className="label">Why this score</span>
          <span className="flex items-center gap-2 text-[11.5px] text-mist-500">
            {candidate.scores.method ?? "baseline"} · estimated
            <ChevronDown className={cn("size-3.5 transition-transform", showScores && "rotate-180")} strokeWidth={2} />
          </span>
        </button>
        {showScores ? (
          <>
            <div className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
              {components.map(([name, component]) => (
                <ScoreMeter
                  key={name}
                  label={name}
                  value={component.score}
                  weight={candidate.scores.weights?.[name]}
                  reason={component.reason}
                />
              ))}
            </div>
            <p className="text-[11.5px] leading-relaxed text-mist-600">
              These are deterministic signals computed from the transcript and timings. They favour clear,
              self-contained moments, not guaranteed performance on any platform.
            </p>
          </>
        ) : null}
      </section>

      <RenderPanel
        key={candidate.id}
        projectId={projectId}
        capabilities={capabilities}
        durationMs={durationMs}
        renders={candidateRenders}
        busy={renderBusy}
        onRender={render}
        disabled={durationMs < 2000}
        disabledReason="Keep at least two seconds of footage before rendering."
      />
    </div>
  );
}

function EditorialPlan({
  candidate,
  currentDurationMs,
  approveEditorial,
  onApproveEditorial,
  onEditorial,
}: {
  candidate: Candidate;
  currentDurationMs: number;
  approveEditorial: boolean;
  onApproveEditorial: (value: boolean) => void;
  onEditorial: (
    candidateId: string,
    action: { strategy: string; pacing_profile?: string; apply?: boolean },
  ) => Promise<void>;
}) {
  const editorial = candidate.editorial;
  const decision = editorial?.editorial;
  const duration = candidate.duration;
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  if (!editorial && !duration) return null;

  const strategy = decision?.strategy ?? editorial?.strategy ?? "chronological";
  const strategyLabel =
    EDITORIAL_STRATEGIES.find((entry) => entry.id === strategy)?.label ??
    (strategy === "chronological" ? "Chronological" : strategy);
  const rationale = decision?.rationale ?? editorial?.rationale ?? "";
  const confidence = decision?.confidence ?? editorial?.confidence ?? 0;
  const concerns = [...(decision?.concerns ?? []), ...(editorial?.warnings ?? [])].filter(
    (value, index, all) => value && all.indexOf(value) === index,
  );
  const alternatives = decision?.alternatives?.length
    ? decision.alternatives
    : editorial?.alternatives ?? [];
  const timeline = (decision?.timeline ?? editorial?.timeline) as TimelineView | undefined;
  const reviewRequired = Boolean(decision?.review_required ?? editorial?.review_required);
  const reordered = Boolean(decision?.reordered ?? editorial?.reordered);
  const applied = Boolean(decision?.applied ?? editorial?.applied);
  const fallback = decision?.fallback ?? "";

  const targetMin = duration?.min_ms;
  const targetMax = duration?.max_ms;
  // A reordered plan or removed pauses change the rendered duration. A manual
  // trim wins over both, because that is the cut the renderer will export.
  const actualDurationMs = candidate.edited
    ? currentDurationMs
    : (timeline?.total_output_ms ?? duration?.actual_ms ?? currentDurationMs);
  const fit =
    targetMin && targetMax
      ? actualDurationMs < targetMin
        ? "too_short"
        : actualDurationMs > targetMax
          ? "too_long"
          : "fit"
      : duration?.status ?? "unknown";
  const planMatchesCut =
    !candidate.edited ||
    !duration?.actual_ms ||
    Math.abs(actualDurationMs - duration.actual_ms) < 50;
  const durationReason = planMatchesCut
    ? duration?.reason
    : `This manual cut is ${formatDuration(actualDurationMs)}; the generated plan was not changed.`;

  async function reEvaluate(value: { strategy: string; pacing_profile?: string }) {
    setBusy(value.strategy + (value.pacing_profile ?? ""));
    setNotice(null);
    try {
      await onEditorial(candidate.id, { ...value, apply: true });
      setNotice("Editorial plan re-evaluated. Manual trims and export settings are unchanged.");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Could not re-evaluate the edit.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="surface space-y-3 p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="label">Editorial plan</p>
          <p className="mt-1 text-[12px] leading-relaxed text-mist-400">
            The source-to-timeline map behind this cut. Captions, audio and video use the same order.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone={reordered ? "ember" : "neutral"}>{strategyLabel}</Badge>
          <span className="numeric text-[11px] text-mist-500">
            {Math.round(confidence * 100)}% confidence
          </span>
          {reviewRequired ? <Badge tone="warn">review required</Badge> : null}
          {reordered ? (
            <Badge tone={applied ? "good" : "neutral"}>{applied ? "reordered" : "reorder proposed"}</Badge>
          ) : null}
          {fallback ? <Badge tone="neutral">fell back from {fallback}</Badge> : null}
        </div>
      </div>

      <div className="grid gap-2 sm:grid-cols-3">
        <div className="rounded-md border border-white/8 bg-black/20 px-2.5 py-2">
          <p className="text-[10.5px] uppercase tracking-wide text-mist-600">Actual duration</p>
          <p className="numeric mt-0.5 text-[13px] text-mist-100">{formatDuration(actualDurationMs)}</p>
        </div>
        <div className="rounded-md border border-white/8 bg-black/20 px-2.5 py-2">
          <p className="text-[10.5px] uppercase tracking-wide text-mist-600">Target range</p>
          <p className="numeric mt-0.5 text-[13px] text-mist-100">
            {targetMin && targetMax
              ? `${formatDuration(targetMin)}-${formatDuration(targetMax)}`
              : duration?.label ?? "Not set"}
          </p>
        </div>
        <div className="rounded-md border border-white/8 bg-black/20 px-2.5 py-2">
          <p className="text-[10.5px] uppercase tracking-wide text-mist-600">Fit</p>
          <p
            className={cn(
              "mt-0.5 text-[12.5px]",
              fit === "fit"
                ? "text-signal-good"
                : fit === "near"
                  ? "text-signal-warn"
                  : "text-mist-300",
            )}
          >
            {fit.replace("_", " ")}
          </p>
        </div>
      </div>
      {durationReason ? (
        <p className="text-[11.5px] leading-relaxed text-mist-500">{durationReason}</p>
      ) : null}

      {rationale ? (
        <p className="text-[12px] leading-relaxed text-mist-300">{rationale}</p>
      ) : null}

      {timeline?.entries?.length ? (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <p className="label">Source → timeline</p>
            <span className="numeric text-[10.5px] text-mist-600">
              {formatDuration(timeline.total_output_ms ?? currentDurationMs)} output ·{" "}
              {timeline.valid === false ? "invalid" : "validated"}
            </span>
          </div>
          <div className="overflow-x-auto rounded-md border border-white/8">
            <table className="w-full min-w-[560px] text-left text-[11px]">
              <thead className="bg-black/30 text-mist-500">
                <tr>
                  <th className="px-2 py-1.5 font-medium">#</th>
                  <th className="px-2 py-1.5 font-medium">Role</th>
                  <th className="px-2 py-1.5 font-medium">Source</th>
                  <th className="px-2 py-1.5 font-medium">Timeline</th>
                  <th className="px-2 py-1.5 font-medium">Join</th>
                  <th className="px-2 py-1.5 font-medium">Key point</th>
                </tr>
              </thead>
              <tbody>
                {timeline.entries.map((entry, index) => (
                  <tr key={`${entry.source_start_ms}-${entry.output_start_ms}-${index}`} className="border-t border-white/6">
                    <td className="numeric px-2 py-1.5 text-mist-600">{index + 1}</td>
                    <td className="px-2 py-1.5 text-mist-200">{entry.role ?? "main"}</td>
                    <td className="numeric px-2 py-1.5 text-mist-400">
                      {formatTimecodePrecise(entry.source_start_ms)}–{formatTimecodePrecise(entry.source_end_ms)}
                    </td>
                    <td className="numeric px-2 py-1.5 text-mist-400">
                      {formatTimecodePrecise(entry.output_start_ms)}–{formatTimecodePrecise(entry.output_end_ms)}
                    </td>
                    <td className="px-2 py-1.5 text-mist-500">{entry.transition ?? "cut"}</td>
                    <td className="max-w-[240px] px-2 py-1.5 text-mist-400">
                      {entry.key_point ? truncate(entry.key_point, 90) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {timeline.warnings?.length ? (
            <ul className="space-y-0.5 text-[11px] text-signal-warn">
              {timeline.warnings.map((warning) => (
                <li key={warning}>· {warning}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : (
        <p className="text-[11.5px] text-mist-500">
          Source order is preserved. No source speech is duplicated or skipped.
        </p>
      )}

      {alternatives.length > 0 ? (
        <div className="space-y-1.5">
          <p className="label">Alternatives</p>
          <div className="grid gap-1.5 sm:grid-cols-2">
            {alternatives.map((alternative) => (
              <div
                key={`${alternative.id}-${alternative.strategy}`}
                className="flex flex-col gap-1 rounded-md border border-white/8 bg-black/20 px-2.5 py-2"
              >
                <div className="flex items-center justify-between gap-2">
                  <p className="text-[12px] text-mist-200">{alternative.label}</p>
                  {alternative.available ? (
                    <span className="numeric text-[10.5px] text-mist-500">
                      {Math.round(alternative.confidence * 100)}%
                    </span>
                  ) : (
                    <Badge tone="neutral">unavailable</Badge>
                  )}
                </div>
                <p className="text-[11px] leading-snug text-mist-500">
                  {alternative.available ? alternative.description || alternative.reason : alternative.reason}
                </p>
                {alternative.available ? (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm mt-0.5 h-6 self-start px-2 text-[11px]"
                    disabled={busy !== null}
                    onClick={() => void reEvaluate({ strategy: alternative.strategy })}
                  >
                    {busy?.startsWith(alternative.strategy) ? "Re-evaluating" : "Use this strategy"}
                  </button>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {concerns.length > 0 ? (
        <div className="rounded-md border border-signal-warn/25 bg-signal-warn/[0.05] px-2.5 py-2">
          <p className="text-[11.5px] font-medium text-signal-warn">Editorial concerns</p>
          <ul className="mt-1 space-y-0.5 text-[11px] leading-relaxed text-mist-300">
            {concerns.map((concern) => (
              <li key={concern}>· {concern}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {reviewRequired && reordered ? (
        <label className="flex items-start gap-2 rounded-md border border-signal-warn/25 bg-signal-warn/[0.05] px-2.5 py-2 text-[11.5px] text-mist-300">
          <input
            type="checkbox"
            className="mt-0.5 size-3.5 accent-ember-500"
            checked={approveEditorial}
            onChange={(event) => onApproveEditorial(event.target.checked)}
          />
          <span>
            I reviewed this non-linear edit. Export may apply the reordered timeline; without this
            approval the render uses chronological order and records the fallback.
          </span>
        </label>
      ) : null}

      {notice ? <p className="text-[11.5px] text-signal-info">{notice}</p> : null}

      <p className="text-[11px] leading-relaxed text-mist-600">
        Re-evaluating uses the stored transcript. It does not re-transcribe, change your trim, or wipe
        captions, branding or composition overrides.
      </p>
    </section>
  );
}

type TimelineEntryView = {
  role?: string;
  key_point?: string;
  source_start_ms: number;
  source_end_ms: number;
  output_start_ms: number;
  output_end_ms: number;
  transition?: string;
};

type TimelineView = {
  entries?: TimelineEntryView[];
  total_output_ms?: number;
  valid?: boolean;
  warnings?: string[];
};

function truncate(value: string, limit: number): string {
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}

function EndingReview({
  candidate,
  onUseEnd,
  onBoundary,
}: {
  candidate: Candidate;
  onUseEnd: (ms: number) => void;
  onBoundary: (candidateId: string, action: { action: "accept" | "reject" | "reset"; end_ms?: number }) => Promise<void>;
}) {
  const boundary = candidate.boundary;
  const evidence = boundary?.evidence ?? [];
  const alternatives = boundary?.alternatives ?? [];
  const [busy, setBusy] = useState<string | null>(null);
  if (!boundary || (evidence.length === 0 && alternatives.length === 0)) return null;

  async function act(action: "accept" | "reject" | "reset", endMs?: number) {
    setBusy(action);
    try {
      await onBoundary(candidate.id, { action, end_ms: endMs });
    } finally {
      setBusy(null);
    }
  }

  return (
    <section
      className={cn(
        "surface space-y-2.5 p-4",
        boundary.suspicious && boundary.reviewed === "pending" && "border-signal-warn/30",
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="label">Ending review</p>
        <div className="flex items-center gap-1.5">
          {boundary.suspicious ? <Badge tone="ember">possible trailing shot</Badge> : <Badge tone="neutral">no strong signal</Badge>}
          {boundary.reviewed !== "pending" ? (
            <Badge tone={boundary.reviewed === "accepted" ? "good" : "bad"}>{boundary.reviewed}</Badge>
          ) : null}
        </div>
      </div>

      <p className="text-[12px] leading-relaxed text-mist-400">
        The pipeline checked the transcript and scene cuts around the out point. It never trims on its own.
        {boundary.current_sentence ? ` Last sentence: “${boundary.current_sentence.slice(-140)}”` : ""}
      </p>

      {evidence.length > 0 ? (
        <ul className="space-y-1 text-[11.5px] leading-relaxed text-mist-400">
          {evidence.slice(0, 6).map((item) => (
            <li key={item}>· {item}</li>
          ))}
        </ul>
      ) : null}

      {alternatives.length > 1 ? (
        <div className="space-y-1.5">
          {alternatives.map((alternative) => (
            <div
              key={`${alternative.end_ms}-${alternative.source}`}
              className="flex flex-wrap items-center gap-2 rounded-md border border-white/8 bg-black/20 px-2.5 py-2"
            >
              <div className="min-w-0 flex-1">
                <p className="text-[12px] text-mist-200">{alternative.label}</p>
                <p className="text-[11px] leading-snug text-mist-500">{alternative.reason}</p>
              </div>
              <span className="numeric text-[11px] text-mist-400">
                out {formatTimecodePrecise(alternative.end_ms)} · {Math.round(alternative.confidence * 100)}%
              </span>
              {alternative.source === "current" ? null : (
                <button
                  type="button"
                  className="btn btn-sm h-6 px-2 text-[11px]"
                  disabled={busy !== null}
                  onClick={() => {
                    onUseEnd(alternative.end_ms);
                    void act("accept", alternative.end_ms);
                  }}
                >
                  {busy === "accept" ? "Saving" : "Use this ending"}
                </button>
              )}
            </div>
          ))}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-1.5">
        {boundary.reviewed === "pending" ? (
          <button
            type="button"
            className="btn btn-ghost btn-sm h-6 px-2 text-[11px]"
            disabled={busy !== null}
            onClick={() => void act("reject")}
          >
            Keep current out point
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-ghost btn-sm h-6 px-2 text-[11px]"
            disabled={busy !== null}
            onClick={() => void act("reset")}
          >
            Reset review
          </button>
        )}
        <span className="text-[10.5px] text-mist-600">
          Accepting writes the out point; rejecting leaves the clip untouched.
        </span>
      </div>
    </section>
  );
}

function BoundaryControl({
  label,
  value,
  min,
  max,
  onChange,
  onNudge,
  onSetHere,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
  onNudge: (delta: number) => void;
  onSetHere: () => void;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <span className="label">{label}</span>
        <span className="numeric text-[12px] text-mist-200">{formatTimecodePrecise(value)}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={50}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className="w-full accent-ember-500"
        aria-label={`${label} point`}
      />
      <div className="flex items-center gap-1">
        <button type="button" className="btn btn-ghost btn-sm h-6 px-1.5 text-[11px]" onClick={() => onNudge(-NUDGE_MS)}>
          −0.25s
        </button>
        <button type="button" className="btn btn-ghost btn-sm h-6 px-1.5 text-[11px]" onClick={() => onNudge(NUDGE_MS)}>
          +0.25s
        </button>
        <button type="button" className="btn btn-ghost btn-sm ml-auto h-6 px-1.5 text-[11px]" onClick={onSetHere}>
          <Scissors className="size-3" strokeWidth={2} />
          Set at playhead
        </button>
      </div>
    </div>
  );
}

function Timeline({
  duration,
  start,
  end,
  current,
  hasWaveform,
  projectId,
  snapPoints,
  onScrub,
}: {
  duration: number;
  start: number;
  end: number;
  current: number;
  hasWaveform: boolean;
  projectId: string;
  snapPoints: number[];
  onScrub: (ms: number) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<number | null>(null);

  const position = (ms: number) => `${clamp((ms / duration) * 100, 0, 100)}%`;

  return (
    <div className="space-y-1">
      <div
        ref={ref}
        className="relative h-20 cursor-crosshair overflow-hidden rounded-md border border-white/8 bg-ink-950"
        onPointerDown={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          const ratio = clamp((event.clientX - rect.left) / rect.width, 0, 1);
          onScrub(ratio * duration);
        }}
        onPointerMove={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          setHover(clamp((event.clientX - rect.left) / rect.width, 0, 1) * duration);
        }}
        onPointerLeave={() => setHover(null)}
        role="slider"
        aria-label="Timeline"
        aria-valuemin={0}
        aria-valuemax={Math.round(duration)}
        aria-valuenow={Math.round(current)}
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") onScrub(current - 1000);
          if (event.key === "ArrowRight") onScrub(current + 1000);
        }}
      >
        {hasWaveform ? (
          // The worker renders this PNG with ffmpeg's showwavespic from the real audio.
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={`/api/projects/${projectId}/media/waveform`}
            alt=""
            aria-hidden="true"
            className="absolute inset-0 size-full object-fill opacity-55"
          />
        ) : (
          <div
            aria-hidden
            className="absolute inset-0 opacity-40"
            style={{
              backgroundImage:
                "repeating-linear-gradient(90deg, rgba(255,255,255,0.18) 0 1px, transparent 1px 4px)",
              maskImage: "linear-gradient(to bottom, transparent, black 40%, black 60%, transparent)",
            }}
          />
        )}

        <div className="absolute inset-y-0 left-0 bg-black/55" style={{ width: position(start) }} />
        <div className="absolute inset-y-0 right-0 bg-black/55" style={{ width: `calc(100% - ${position(end)})` }} />
        <div
          className="pointer-events-none absolute inset-y-0 border-x border-ember-500/70 bg-ember-500/[0.06]"
          style={{ left: position(start), width: `calc(${position(end)} - ${position(start)})` }}
        />

        {snapPoints.slice(0, 400).map((point) => (
          <span
            key={point}
            aria-hidden
            className="absolute bottom-0 h-1.5 w-px bg-white/25"
            style={{ left: position(point) }}
          />
        ))}

        <span aria-hidden className="absolute inset-y-0 w-px bg-mist-50/90" style={{ left: position(current) }} />
        {hover !== null ? (
          <span
            aria-hidden
            className="absolute inset-y-0 w-px bg-white/30"
            style={{ left: `${clamp((hover / duration) * 100, 0, 100)}%` }}
          />
        ) : null}
      </div>
      <div className="flex justify-between text-[10.5px] text-mist-600">
        <span className="numeric">0:00</span>
        <span className="text-mist-500">Click to seek · ticks mark sentence starts</span>
        <span className="numeric">{formatTimecode(duration)}</span>
      </div>
    </div>
  );
}

function TranscriptWindow({
  transcript,
  start,
  end,
  currentMs,
  onSeek,
  onUseStart,
  onUseEnd,
}: {
  transcript: Transcript | null;
  start: number;
  end: number;
  currentMs: number;
  onSeek: (ms: number) => void;
  onUseStart: (ms: number) => void;
  onUseEnd: (ms: number) => void;
}) {
  if (!transcript) {
    return (
      <section className="surface px-4 py-5">
        <p className="label">Transcript</p>
        <p className="mt-1.5 text-[12.5px] text-mist-500">
          No transcript is stored for this project yet. Run the analysis to create one.
        </p>
      </section>
    );
  }

  const windowStart = Math.max(0, start - 4000);
  const windowEnd = end + 4000;
  const segments = transcript.segments.filter(
    (segment) => segment.end_ms >= windowStart && segment.start_ms <= windowEnd,
  );

  if (segments.length === 0) {
    return (
      <section className="surface px-4 py-5">
        <p className="label">Transcript</p>
        <p className="mt-1.5 text-[12.5px] text-mist-500">
          No speech was transcribed around this selection. It may be a quiet passage — check whether the
          clip still makes sense on its own.
        </p>
      </section>
    );
  }

  return (
    <section className="surface space-y-2 p-4">
      <div className="flex items-center justify-between gap-2">
        <p className="label">Transcript around the selection</p>
        <span className="text-[11px] text-mist-600">{transcript.language ?? "unknown language"}</span>
      </div>
      <div className="max-h-64 space-y-2 overflow-y-auto pr-1">
        {segments.map((segment) => {
          const inside = segment.start_ms >= start && segment.end_ms <= end;
          return (
            <div
              key={segment.id}
              className={cn(
                "rounded-md px-2.5 py-2 text-[12.5px] leading-relaxed transition-colors",
                inside ? "bg-ember-500/[0.06]" : "bg-black/20",
              )}
            >
              <div className="mb-1 flex items-center gap-2 text-[10.5px] text-mist-600">
                <button
                  type="button"
                  className="numeric underline-offset-2 hover:text-mist-300 hover:underline"
                  onClick={() => onSeek(segment.start_ms)}
                >
                  {formatTimecode(segment.start_ms)}
                </button>
                <span>·</span>
                <span>{formatDuration(segment.end_ms - segment.start_ms)}</span>
                <span className="ml-auto flex gap-1">
                  <button
                    type="button"
                    className="hover:text-ember-300"
                    onClick={() => onUseStart(segment.start_ms)}
                    title="Use this sentence start as the in point"
                  >
                    in
                  </button>
                  <button
                    type="button"
                    className="hover:text-ember-300"
                    onClick={() => onUseEnd(segment.end_ms)}
                    title="Use this sentence end as the out point"
                  >
                    out
                  </button>
                </span>
              </div>
              <p className="text-mist-200">
                {segment.words.length > 0
                  ? segment.words.map((word) => {
                      const active = currentMs >= word.start_ms && currentMs < word.end_ms;
                      const inRange = word.start_ms >= start && word.end_ms <= end;
                      return (
                        <button
                          key={`${word.start_ms}-${word.word}`}
                          type="button"
                          onClick={() => onSeek(word.start_ms)}
                          className={cn(
                            "rounded px-0.5 transition-colors hover:bg-white/10",
                            inRange ? "text-mist-100" : "text-mist-500",
                            active && "bg-ember-500/25 text-ember-200",
                          )}
                        >
                          {word.word}{" "}
                        </button>
                      );
                    })
                  : segment.text}
              </p>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function collectBoundaries(transcript: Transcript | null, start: number, end: number): number[] {
  if (!transcript) return [];
  const points: number[] = [];
  for (const segment of transcript.segments) {
    if (segment.end_ms < start - 12_000 || segment.start_ms > end + 12_000) continue;
    points.push(segment.start_ms, segment.end_ms);
    for (const word of segment.words) points.push(word.start_ms, word.end_ms);
  }
  return Array.from(new Set(points)).sort((a, b) => a - b);
}

function nearestBoundary(
  transcript: Transcript | null,
  target: number,
  edge: "start" | "end",
  toleranceMs = 1200,
): number | null {
  if (!transcript) return null;
  let best: { distance: number; value: number } | null = null;
  for (const segment of transcript.segments) {
    const words = segment.words.length > 0 ? segment.words : [{ start_ms: segment.start_ms, end_ms: segment.end_ms }];
    for (const word of words) {
      const value = edge === "start" ? word.start_ms : word.end_ms;
      const distance = Math.abs(value - target);
      if (distance <= toleranceMs && (!best || distance < best.distance)) {
        best = { distance, value };
      }
    }
  }
  return best?.value ?? null;
}
