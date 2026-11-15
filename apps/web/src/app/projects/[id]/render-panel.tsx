"use client";

import { useMemo, useState } from "react";
import { Download, Film, Loader2, Type } from "lucide-react";
import type { Capabilities, Render } from "@clipforge/shared";
import { ASPECTS, CAPTION_PRESETS, formatBytes, formatDuration, formatTimecode } from "@clipforge/shared";
import { Alert, Badge, EmptyState } from "@/components/ui";
import { cn } from "@/lib/utils";

type AspectId = (typeof ASPECTS)[number]["id"];
type CaptionPresetId = (typeof CAPTION_PRESETS)[number]["id"];

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
  const [framing, setFraming] = useState<"crop" | "blur">("crop");
  const [captionPreset, setCaptionPreset] = useState<CaptionPresetId>("clean");
  const [captions, setCaptions] = useState(true);
  const [normalize, setNormalize] = useState(true);
  const [showPreview, setShowPreview] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
        framing,
        caption_preset: captionPreset,
        captions_enabled: captions,
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
            <div className="segmented">
              <button type="button" data-active={framing === "crop"} onClick={() => setFraming("crop")}>
                Crop
              </button>
              <button type="button" data-active={framing === "blur"} onClick={() => setFraming("blur")}>
                Blurred background
              </button>
            </div>
          </div>
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
          {CAPTION_PRESETS.map((preset) => (
            <button
              key={preset.id}
              type="button"
              onClick={() => setCaptionPreset(preset.id)}
              aria-pressed={captionPreset === preset.id}
              disabled={!captions}
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

function RenderRow({ render }: { render: Render }) {
  const tone =
    render.status === "completed"
      ? "good"
      : render.status === "failed"
        ? "bad"
        : render.status === "cancelled"
          ? "neutral"
          : "ember";

  return (
    <li className="flex flex-wrap items-center gap-2 rounded-md border border-white/8 bg-black/20 px-2.5 py-2">
      <Badge tone={tone} className="h-5 px-2 text-[10.5px]">
        {render.status}
      </Badge>
      <span className="numeric text-[11.5px] text-mist-400">
        {render.width && render.height ? `${render.width}×${render.height}` : "—"}
        {render.duration_ms ? ` · ${formatTimecode(render.duration_ms)}` : ""}
        {render.size_bytes ? ` · ${formatBytes(render.size_bytes)}` : ""}
      </span>
      {render.status === "failed" && render.error_message ? (
        <span className="w-full text-[11.5px] text-signal-bad/90">{render.error_message}</span>
      ) : null}
      <div className="ml-auto flex items-center gap-1.5">
        {render.status === "completed" ? (
          <a className="btn btn-sm h-6 px-2 text-[11px]" href={`/api/renders/${render.id}/download`}>
            <Download className="size-3" strokeWidth={2} />
            MP4
          </a>
        ) : null}
      </div>
    </li>
  );
}
