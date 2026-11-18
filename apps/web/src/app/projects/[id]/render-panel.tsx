"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, Download, Film, Loader2, Type } from "lucide-react";
import type { Capabilities, Render } from "@clipforge/shared";
import {
  ASPECTS,
  CAPTION_ALIGNMENTS,
  CAPTION_FONTS,
  CAPTION_POSITIONS,
  CAPTION_PRESETS,
  COMPOSITION_MODES,
  DEFAULT_CAPTION_ALIGNMENT,
  DEFAULT_CAPTION_MARGIN_V,
  DEFAULT_CAPTION_POSITION,
  DEFAULT_CAPTION_PRESET,
  formatBytes,
  formatDuration,
  formatTimecode,
} from "@clipforge/shared";
import { Alert, Badge, EmptyState } from "@/components/ui";
import { apiJson } from "@/lib/api";
import { cn } from "@/lib/utils";

type AspectId = (typeof ASPECTS)[number]["id"];
type CaptionPresetId = (typeof CAPTION_PRESETS)[number]["id"];
type CaptionPositionId = (typeof CAPTION_POSITIONS)[number]["id"];
type CaptionAlignmentId = (typeof CAPTION_ALIGNMENTS)[number]["id"];
type CompositionModeId = (typeof COMPOSITION_MODES)[number]["id"];

export function RenderPanel({
  projectId,
  capabilities,
  durationMs,
  renders,
  busy,
  onRender,
  disabled,
  disabledReason,
}: {
  projectId: string;
  capabilities: Capabilities | null;
  durationMs: number;
  renders: Render[];
  busy: boolean;
  onRender: (body: Record<string, unknown>) => Promise<void>;
  disabled: boolean;
  disabledReason: string;
}) {
  const [aspect, setAspect] = useState<AspectId>("9:16");
  const [resolution, setResolution] = useState<720 | 1080>(1080);
  const [composition, setComposition] = useState<CompositionModeId>("auto");
  const [cropX, setCropX] = useState(50);
  const [cropY, setCropY] = useState(50);
  const [captionPreset, setCaptionPreset] = useState<CaptionPresetId>(DEFAULT_CAPTION_PRESET);
  const [captionFont, setCaptionFont] = useState<string>("Montserrat");
  const [captionPosition, setCaptionPosition] = useState<CaptionPositionId>(DEFAULT_CAPTION_POSITION);
  const [captionAlignment, setCaptionAlignment] = useState<CaptionAlignmentId>(DEFAULT_CAPTION_ALIGNMENT);
  const [captionMarginV, setCaptionMarginV] = useState(DEFAULT_CAPTION_MARGIN_V);
  const [captionSaveState, setCaptionSaveState] = useState<"loading" | "saving" | "saved" | "error">("loading");
  const [captions, setCaptions] = useState(true);
  const [normalize, setNormalize] = useState(true);
  const [showPreview, setShowPreview] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const hydratedRef = useRef(false);
  const savedSnapshotRef = useRef("");

  const captionSnapshot = useMemo(
    () =>
      JSON.stringify({
        preset: captionPreset,
        position: captionPosition,
        alignment: captionAlignment,
        margin_v: captionMarginV,
      }),
    [captionPreset, captionPosition, captionAlignment, captionMarginV],
  );
  const captionControlsDisabled = !captions || captionSaveState === "loading";

  useEffect(() => {
    let alive = true;
    hydratedRef.current = false;
    apiJson<{
      resolved?: { captions?: Record<string, unknown> };
      overrides?: Record<string, unknown>;
    }>(`/api/projects/${projectId}/settings`)
      .then((loaded) => {
        if (!alive) return;
        const stored = (loaded.resolved?.captions ?? {}) as Record<string, unknown>;
        const preset =
          stored.preset === "custom" ||
          (typeof stored.preset === "string" && CAPTION_PRESETS.some((entry) => entry.id === stored.preset))
            ? (stored.preset as CaptionPresetId)
            : DEFAULT_CAPTION_PRESET;
        const position =
          stored.position === "top" || stored.position === "center" || stored.position === "bottom"
            ? stored.position
            : DEFAULT_CAPTION_POSITION;
        const alignment =
          stored.alignment === "left" || stored.alignment === "right" || stored.alignment === "center"
            ? stored.alignment
            : DEFAULT_CAPTION_ALIGNMENT;
        const margin = typeof stored.margin_v === "number" ? stored.margin_v : DEFAULT_CAPTION_MARGIN_V;
        setCaptionPreset(preset);
        setCaptionPosition(position);
        setCaptionAlignment(alignment);
        setCaptionMarginV(margin);
        savedSnapshotRef.current = JSON.stringify({
          preset,
          position,
          alignment,
          margin_v: margin,
        });
        hydratedRef.current = true;
        setCaptionSaveState("saved");
      })
      .catch(() => {
        if (!alive) return;
        hydratedRef.current = true;
        savedSnapshotRef.current = JSON.stringify({
          preset: DEFAULT_CAPTION_PRESET,
          position: DEFAULT_CAPTION_POSITION,
          alignment: DEFAULT_CAPTION_ALIGNMENT,
          margin_v: DEFAULT_CAPTION_MARGIN_V,
        });
        setCaptionSaveState("error");
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  useEffect(() => {
    if (!hydratedRef.current || captionSnapshot === savedSnapshotRef.current) return;
    setCaptionSaveState("saving");
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const current = await apiJson<{ overrides?: Record<string, unknown> }>(
            `/api/projects/${projectId}/settings`,
          );
          const base = current.overrides ?? {};
          const existing =
            typeof base.captions === "object" && base.captions !== null
              ? (base.captions as Record<string, unknown>)
              : {};
          await apiJson(`/api/projects/${projectId}/settings`, {
            method: "PATCH",
            body: {
              overrides: {
                ...base,
                captions: {
                  ...existing,
                  preset: captionPreset,
                  position: captionPosition,
                  alignment: captionAlignment,
                  margin_v: captionMarginV,
                },
              },
            },
          });
          savedSnapshotRef.current = captionSnapshot;
          setCaptionSaveState("saved");
        } catch {
          setCaptionSaveState("error");
        }
      })();
    }, 450);
    return () => window.clearTimeout(timer);
  }, [captionAlignment, captionMarginV, captionPosition, captionPreset, captionSnapshot, projectId]);

  function resetCaptionPosition() {
    setCaptionPosition(DEFAULT_CAPTION_POSITION);
    setCaptionAlignment(DEFAULT_CAPTION_ALIGNMENT);
    setCaptionMarginV(DEFAULT_CAPTION_MARGIN_V);
  }

  const latestComplete = useMemo(
    () => renders.find((render) => render.status === "completed"),
    [renders],
  );

  async function submit() {
    setError(null);
    try {
      await onRender({
        aspect,
        resolution,
        composition,
        crop_x_pct: cropX,
        crop_y_pct: cropY,
        caption_preset: captionPreset,
        captions_enabled: captions,
        captions: {
          font_family: captionFont,
          position: captionPosition,
          alignment: captionAlignment,
          margin_v: captionMarginV,
        },
        normalize_audio: normalize,
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The render could not be queued.");
    }
  }

  return (
    <section className="surface space-y-4 p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="label">Export</p>
          <p className="mt-1 text-[12.5px] text-mist-500">
            Rendered locally with ffmpeg from your source file. {formatDuration(durationMs)} of footage.
          </p>
        </div>
        <Badge tone="neutral">{capabilities?.media.ffmpeg.available === false ? "ffmpeg missing" : "H.264 · AAC"}</Badge>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <p className="label">Frame</p>
          <div className="grid gap-1.5">
            {ASPECTS.map((entry) => (
              <button
                key={entry.id}
                type="button"
                onClick={() => setAspect(entry.id)}
                aria-pressed={aspect === entry.id}
                className={cn(
                  "flex items-center justify-between rounded-md border px-2.5 py-1.5 text-left transition-colors",
                  aspect === entry.id
                    ? "border-ember-500/50 bg-ember-500/[0.08]"
                    : "border-white/8 bg-black/20 hover:border-white/16",
                )}
              >
                <span className="text-[12.5px] text-mist-200">{entry.label}</span>
                <span className="text-[10.5px] text-mist-600">{entry.hint}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="space-y-3">
          <div className="space-y-1.5">
            <p className="label">Resolution</p>
            <div className="segmented">
              <button type="button" data-active={resolution === 720} onClick={() => setResolution(720)}>
                720p
              </button>
              <button type="button" data-active={resolution === 1080} onClick={() => setResolution(1080)}>
                1080p
              </button>
            </div>
          </div>
          <div className="space-y-1.5">
            <p className="label">How to fill the frame</p>
            <div className="grid gap-1.5">
              {COMPOSITION_MODES.map((mode) => (
                <button
                  key={mode.id}
                  type="button"
                  onClick={() => setComposition(mode.id)}
                  aria-pressed={composition === mode.id}
                  className={cn(
                    "flex items-center justify-between gap-2 rounded-md border px-2.5 py-1.5 text-left transition-colors",
                    composition === mode.id
                      ? "border-ember-500/50 bg-ember-500/[0.08]"
                      : "border-white/8 bg-black/20 hover:border-white/16",
                    mode.id === "split" && "opacity-60",
                  )}
                >
                  <span className="text-[12.5px] text-mist-200">{mode.label}</span>
                  <span className="text-right text-[10.5px] text-mist-600">{mode.hint}</span>
                </button>
              ))}
            </div>
          </div>
          <div className="space-y-1.5">
            <label className="flex items-center gap-2 text-[12.5px] text-mist-300">
              <input
                type="checkbox"
                className="size-3.5 accent-ember-500"
                checked={normalize}
                onChange={(event) => setNormalize(event.target.checked)}
              />
              Normalise loudness
            </label>
          </div>
        </div>
      </div>

      <div className="grid gap-4 border-t border-white/6 pt-3 lg:grid-cols-[168px_1fr]">
        <CompositionPreview
          aspect={aspect}
          mode={composition}
          captionPosition={captionPosition}
          captionAlignment={captionAlignment}
        />
        <div className="space-y-2">
          <p className="label">Layout preview</p>
          <p className="text-[11.5px] leading-relaxed text-mist-500">
            {composition === "auto"
              ? "Scene changes are detected before encoding. Slides, tables and screen recordings are contained; a centred speaker is cropped."
              : COMPOSITION_MODES.find((mode) => mode.id === composition)?.hint}
          </p>
          {composition === "crop" || composition === "manual" ? (
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="space-y-1 text-[11px] text-mist-500">
                <span className="flex justify-between">
                  <span>Crop focus X</span>
                  <span className="numeric text-mist-300">{Math.round(cropX)}%</span>
                </span>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={cropX}
                  onChange={(event) => setCropX(Number(event.target.value))}
                  className="w-full accent-ember-500"
                />
              </label>
              <label className="space-y-1 text-[11px] text-mist-500">
                <span className="flex justify-between">
                  <span>Crop focus Y</span>
                  <span className="numeric text-mist-300">{Math.round(cropY)}%</span>
                </span>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={cropY}
                  onChange={(event) => setCropY(Number(event.target.value))}
                  className="w-full accent-ember-500"
                />
              </label>
            </div>
          ) : null}
          <p className="text-[11px] text-mist-600">
            Caption guides show the measured safe band, not a guess. The export gate records the real pixel width.
          </p>
        </div>
      </div>

      <div className="space-y-2 border-t border-white/6 pt-3">
        <div className="flex items-center justify-between">
          <p className="label flex items-center gap-1.5">
            <Type className="size-3" strokeWidth={2} />
            Captions
          </p>
          <label className="flex items-center gap-2 text-[12px] text-mist-300">
            <input
              type="checkbox"
              className="size-3.5 accent-ember-500"
              checked={captions}
              onChange={(event) => setCaptions(event.target.checked)}
            />
            Burn into the video
          </label>
        </div>
        <div className={cn("grid gap-1.5 sm:grid-cols-2", !captions && "pointer-events-none opacity-45")}>
          <label className="space-y-1 text-[11px] text-mist-500 sm:col-span-2">
            <span>Caption face</span>
            <select
              className="field h-8 w-full text-[12px]"
              value={captionFont}
              onChange={(event) => setCaptionFont(event.target.value)}
              disabled={!captions}
            >
              {CAPTION_FONTS.map((font) => (
                <option key={font.id} value={font.id}>
                  {font.label} — {font.note}
                </option>
              ))}
            </select>
          </label>
          {CAPTION_PRESETS.map((preset) => (
            <button
              key={preset.id}
              type="button"
              onClick={() => setCaptionPreset(preset.id)}
              aria-pressed={captionPreset === preset.id}
              disabled={captionControlsDisabled}
              className={cn(
                "rounded-md border px-2.5 py-2 text-left transition-colors",
                captionPreset === preset.id
                  ? "border-ember-500/50 bg-ember-500/[0.08]"
                  : "border-white/8 bg-black/20 hover:border-white/16",
              )}
            >
              <span className="flex items-center justify-between gap-2">
                <span className="text-[12.5px] font-medium text-mist-100">{preset.label}</span>
                <span
                  aria-hidden
                  className="size-2 rounded-full"
                  style={{ backgroundColor: preset.sample.highlight }}
                />
              </span>
              <span className="mt-0.5 block text-[11px] leading-snug text-mist-500">{preset.description}</span>
            </button>
          ))}
        </div>

        <div
          className={cn(
            "space-y-2 rounded-md border border-white/8 bg-black/20 p-2.5",
            !captions && "pointer-events-none opacity-45",
          )}
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="label">Placement</p>
            <button
              type="button"
              className="btn btn-ghost btn-sm h-6 px-1.5 text-[11px]"
              onClick={resetCaptionPosition}
              disabled={
                captionControlsDisabled ||
                (captionPosition === DEFAULT_CAPTION_POSITION &&
                  captionAlignment === DEFAULT_CAPTION_ALIGNMENT &&
                  captionMarginV === DEFAULT_CAPTION_MARGIN_V)
              }
            >
              Reset to bottom
            </button>
          </div>
          <div className="segmented w-full sm:w-auto" role="group" aria-label="Caption position">
            {CAPTION_POSITIONS.map((position) => (
              <button
                key={position.id}
                type="button"
                data-active={captionPosition === position.id}
                aria-pressed={captionPosition === position.id}
                onClick={() => setCaptionPosition(position.id)}
                disabled={!captions}
              >
                {position.label}
              </button>
            ))}
          </div>
          <p className="text-[11px] leading-relaxed text-mist-600">
            {CAPTION_POSITIONS.find((position) => position.id === captionPosition)?.hint}
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[11px] text-mist-500">Horizontal</span>
            <div className="segmented" role="group" aria-label="Caption alignment">
              {CAPTION_ALIGNMENTS.map((alignment) => (
                <button
                  key={alignment.id}
                  type="button"
                  data-active={captionAlignment === alignment.id}
                  aria-pressed={captionAlignment === alignment.id}
                  onClick={() => setCaptionAlignment(alignment.id)}
                  disabled={!captions}
                >
                  {alignment.label}
                </button>
              ))}
            </div>
          </div>
          {captionPosition === "bottom" ? (
            <label className="block space-y-1 text-[11px] text-mist-500">
              <span className="flex justify-between">
                <span>Bottom inset</span>
                <span className="numeric text-mist-300">{captionMarginV}px</span>
              </span>
              <input
                type="range"
                min={40}
                max={420}
                step={10}
                value={captionMarginV}
                onChange={(event) => setCaptionMarginV(Number(event.target.value))}
                className="w-full accent-ember-500"
                disabled={!captions}
              />
              <span className="block text-[10.5px] leading-relaxed text-mist-600">
                The export keeps captions above the platform-safe bottom edge.
              </span>
            </label>
          ) : null}
          <p
            aria-live="polite"
            className={cn(
              "text-[10.5px] leading-relaxed",
              captionSaveState === "error" ? "text-signal-bad/90" : "text-mist-600",
            )}
          >
            {captionSaveState === "loading"
              ? "Loading caption defaults..."
              : captionSaveState === "saving"
                ? "Saving caption defaults..."
                : captionSaveState === "error"
                  ? "Caption defaults could not be saved. This render still uses the selected placement."
                  : "Caption defaults are saved for this project."}
          </p>
        </div>

        <p className="text-[11px] text-mist-600">
          Captions come from the stored word timings, so the highlighted word always matches the audio.
        </p>
      </div>

      {error ? <Alert tone="bad">{error}</Alert> : null}

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-white/6 pt-3">
        <p className="text-[11.5px] text-mist-600">
          {disabled ? disabledReason : "Rendering keeps the source untouched and writes a new MP4."}
        </p>
        <button type="button" className="btn btn-primary btn-sm" onClick={submit} disabled={busy || disabled}>
          {busy ? <Loader2 className="size-3.5 animate-spin" strokeWidth={2} /> : <Film className="size-3.5" strokeWidth={2} />}
          {busy ? "Queued" : "Render clip"}
        </button>
      </div>

      <div className="space-y-2 border-t border-white/6 pt-3">
        <div className="flex items-center justify-between">
          <p className="label">Exports for this clip</p>
          {latestComplete ? (
            <button type="button" className="btn btn-ghost btn-sm h-6 px-1.5 text-[11px]" onClick={() => setShowPreview((value) => !value)}>
              {showPreview ? "Hide preview" : "Preview"}
            </button>
          ) : null}
        </div>

        {renders.length === 0 ? (
          <EmptyState
            title="No renders yet"
            description="Set the frame and captions, then render. The MP4 lands in this project's exports folder."
            className="py-7"
          />
        ) : (
          <ul className="space-y-1.5">
            {renders.map((render) => (
              <RenderRow key={render.id} render={render} />
            ))}
          </ul>
        )}

        {showPreview && latestComplete ? (
          <video
            key={latestComplete.id}
            src={`/api/renders/${latestComplete.id}/stream`}
            controls
            className="aspect-video w-full rounded-lg border border-white/10 bg-black"
          />
        ) : null}

        <p className="text-[11px] text-mist-600">
          Project media lives in <span className="numeric">data/projects/{projectId.slice(0, 6)}…/</span> on this machine.
          Downloads are served by the local worker, not a CDN.
        </p>
      </div>
    </section>
  );
}

function CompositionPreview({
  aspect,
  mode,
  captionPosition,
  captionAlignment,
}: {
  aspect: AspectId;
  mode: CompositionModeId;
  captionPosition: CaptionPositionId;
  captionAlignment: CaptionAlignmentId;
}) {
  const ratio = aspect === "9:16" ? "9 / 16" : aspect === "1:1" ? "1 / 1" : "16 / 9";
  const contained = mode === "fit" || mode === "split";
  const pip = mode === "pip";
  const bottomInset = aspect === "9:16" ? "18%" : "10%";
  const horizontal =
    captionAlignment === "left"
      ? { left: "8%", right: "auto" }
      : captionAlignment === "right"
        ? { left: "auto", right: "8%" }
        : { left: "10%", right: "10%" };
  return (
    <div className="space-y-1.5">
      <div
        className="relative overflow-hidden rounded-lg border border-white/10"
        style={{ aspectRatio: ratio, background: "linear-gradient(145deg,#171b22,#0b0d11)" }}
        aria-label={`Layout preview: ${mode}`}
        role="img"
      >
        {contained || pip ? (
          <>
            <div className="absolute inset-0 bg-[radial-gradient(circle_at_30%_30%,rgba(255,255,255,0.14),transparent_60%)] opacity-50 blur-sm" />
            <div
              className="absolute rounded-sm border border-white/15 bg-white/[0.06]"
              style={
                pip
                  ? { left: "14%", right: "14%", top: "26%", height: "38%" }
                  : { left: 0, right: 0, top: "34%", height: "32%" }
              }
            >
              <span className="absolute inset-0 grid place-items-center text-[9px] uppercase tracking-[0.18em] text-mist-500">
                source
              </span>
            </div>
          </>
        ) : (
          <div className="absolute inset-0 bg-[radial-gradient(circle_at_50%_45%,rgba(255,255,255,0.16),transparent_65%)]">
            <span className="absolute inset-0 grid place-items-center text-[9px] uppercase tracking-[0.18em] text-mist-600">
              filled
            </span>
          </div>
        )}
        <div
          className="absolute inset-x-0 border-y border-dashed border-ember-500/40 bg-ember-500/[0.06]"
          style={
            captionPosition === "center"
              ? { ...horizontal, top: "44%", height: "12%" }
              : captionPosition === "top"
                ? { ...horizontal, top: "8%", height: "12%" }
                : { ...horizontal, bottom: bottomInset, height: "12%" }
          }
        >
          <span className="absolute inset-0 grid place-items-center text-[8px] uppercase tracking-[0.2em] text-ember-300/80">
            captions
          </span>
        </div>
        <span className="absolute left-1.5 top-1.5 rounded bg-black/55 px-1 py-0.5 text-[8.5px] uppercase tracking-wide text-mist-400">
          {mode}
        </span>
      </div>
      <p className="text-[10px] leading-snug text-mist-600">
        Schematic. The worker stores the real scene plan in the render spec.
      </p>
    </div>
  );
}

function RenderRow({ render }: { render: Render }) {
  const [open, setOpen] = useState(false);
  const review = render.review;
  const tone =
    render.status === "completed"
      ? "good"
      : render.status === "failed"
        ? "bad"
        : render.status === "cancelled"
          ? "neutral"
          : "ember";
  const gateStatus = review?.status;
  const reviewTone = gateStatus === "fail" ? "bad" : gateStatus === "warn" ? "ember" : "good";

  return (
    <li className="rounded-md border border-white/8 bg-black/20 px-2.5 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={tone} className="h-5 px-2 text-[10.5px]">
          {render.status}
        </Badge>
        {gateStatus ? (
          <Badge tone={reviewTone} className="h-5 px-2 text-[10.5px]">
            gate {gateStatus}
          </Badge>
        ) : null}
        <span className="numeric text-[11.5px] text-mist-400">
          {render.width && render.height ? `${render.width}×${render.height}` : "—"}
          {render.duration_ms ? ` · ${formatTimecode(render.duration_ms)}` : ""}
          {render.size_bytes ? ` · ${formatBytes(render.size_bytes)}` : ""}
        </span>
        {render.status === "failed" && render.error_message ? (
          <span className="w-full text-[11.5px] text-signal-bad/90">{render.error_message}</span>
        ) : null}
        <div className="ml-auto flex items-center gap-1.5">
          {review?.checks?.length ? (
            <button
              type="button"
              className="btn btn-ghost btn-sm h-6 px-1.5 text-[11px]"
              onClick={() => setOpen((value) => !value)}
              aria-expanded={open}
            >
              Review
              <ChevronDown className={cn("size-3 transition-transform", open && "rotate-180")} strokeWidth={2} />
            </button>
          ) : null}
          {render.status === "completed" ? (
            <a className="btn btn-sm h-6 px-2 text-[11px]" href={`/api/renders/${render.id}/download`}>
              <Download className="size-3" strokeWidth={2} />
              MP4
            </a>
          ) : null}
          {review?.draft_relative_path ? (
            <a className="btn btn-ghost btn-sm h-6 px-2 text-[11px]" href={`/api/renders/${render.id}/draft`}>
              <Download className="size-3" strokeWidth={2} />
              Draft
            </a>
          ) : null}
        </div>
      </div>

      {open && review ? (
        <div className="mt-2 space-y-2 border-t border-white/6 pt-2">
          <p className="text-[11.5px] text-mist-300">{review.summary}</p>
          <ul className="space-y-1">
            {review.checks.map((check) => (
              <li key={check.id} className="flex gap-2 text-[11.5px] leading-snug">
                <Badge
                  tone={check.status === "fail" ? "bad" : check.status === "warn" ? "ember" : "neutral"}
                  className="mt-0.5 h-4 shrink-0 px-1.5 text-[9.5px]"
                >
                  {check.status}
                </Badge>
                <span className="text-mist-400">
                  <span className="text-mist-200">{check.label}:</span> {check.detail}
                </span>
              </li>
            ))}
          </ul>
          <EditDecisionList edl={review.edl} />
          <CaptionMeasurements review={review} />
        </div>
      ) : null}
    </li>
  );
}

function EditDecisionList({ edl }: { edl?: Record<string, unknown> }) {
  const segments = Array.isArray(edl?.segments) ? (edl.segments as Array<Record<string, unknown>>) : [];
  if (segments.length === 0) return null;
  return (
    <div className="space-y-1">
      <p className="label">Source → output</p>
      <div className="max-h-40 overflow-y-auto rounded border border-white/6">
        <table className="w-full border-collapse text-[10.5px]">
          <thead className="sticky top-0 bg-ink-900 text-mist-500">
            <tr>
              <th className="px-1.5 py-1 text-left font-medium">#</th>
              <th className="px-1.5 py-1 text-left font-medium">Source</th>
              <th className="px-1.5 py-1 text-left font-medium">Output</th>
              <th className="px-1.5 py-1 text-left font-medium">Layout</th>
            </tr>
          </thead>
          <tbody className="numeric text-mist-400">
            {segments.map((segment, index) => {
              const composition = segment.composition as Record<string, unknown> | null | undefined;
              return (
                <tr key={index} className="border-t border-white/5">
                  <td className="px-1.5 py-0.5">{index + 1}</td>
                  <td className="px-1.5 py-0.5">
                    {formatTimecode(Number(segment.source_start_ms ?? 0))} –{" "}
                    {formatTimecode(Number(segment.source_end_ms ?? 0))}
                  </td>
                  <td className="px-1.5 py-0.5">
                    {formatTimecode(Number(segment.output_start_ms ?? 0))} –{" "}
                    {formatTimecode(Number(segment.output_end_ms ?? 0))}
                  </td>
                  <td className="px-1.5 py-0.5">{String(composition?.mode ?? "legacy")}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CaptionMeasurements({ review }: { review: NonNullable<Render["review"]> }) {
  const check = review.checks.find((entry) => entry.id === "captions");
  const report = (check as Record<string, unknown> | undefined)?.report as Record<string, unknown> | undefined;
  if (!report || !report.used) return null;
  const width = Number(report.max_line_width_px ?? 0);
  const limit = Number(report.max_line_width_limit_px ?? 0);
  return (
    <p className="numeric text-[10.5px] text-mist-500">
      Captions: widest measured line {width.toFixed(0)}px
      {limit ? ` of ${limit.toFixed(0)}px safe width` : ""} · {String(report.chunks ?? 0)} chunks ·{" "}
      {report.bounds_ok ? "inside the safe band" : "outside the safe band"}
    </p>
  );
}
