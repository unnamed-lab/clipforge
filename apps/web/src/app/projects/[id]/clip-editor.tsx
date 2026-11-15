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
import { RenderPanel } from "./render-panel";
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
  onRender: (body: Record<string, unknown>) => Promise<void>;
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

  useEffect(() => {
    setStart(candidate.start_ms);
    setEnd(candidate.end_ms);
    setTitle(candidate.title);
    setCurrentMs(candidate.start_ms);
    const video = videoRef.current;
    if (video && Math.abs(video.currentTime * 1000 - candidate.start_ms) > 800) {
      video.currentTime = candidate.start_ms / 1000;
    }
  }, [candidate.id, candidate.start_ms, candidate.end_ms, candidate.title]);

  const dirty = start !== candidate.start_ms || end !== candidate.end_ms || title !== candidate.title;
  const durationMs = Math.max(0, end - start);
  const duration = source.duration_ms;

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
    setCurrentMs(Math.round(ms / 100) * 100);
    if (loop && ms >= end) {
      video.currentTime = start / 1000;
    } else if (ms < start - 500 || ms > end + 500) {
      // keep the visible window honest while free-scrubbing the source
    }
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
    await onRender({ ...body, candidate_id: candidate.id, title: title || candidate.title });
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
        />
        <div className="flex flex-wrap items-center gap-2 border-t border-white/8 bg-ink-900 px-2.5 py-2">
          <button
            type="button"
            className="btn btn-ghost btn-sm h-7 px-2"
            onClick={() => {
              const video = videoRef.current;
              if (!video) return;
              if (video.paused) {
                if (video.currentTime * 1000 < start || video.currentTime * 1000 >= end) seek(start);
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
            {formatTimecodePrecise(currentMs)} / {formatTimecode(duration)}
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
