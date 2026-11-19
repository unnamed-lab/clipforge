"use client";

import { useRef, useState } from "react";
import { FileVideo, Link2, Loader2, Search, UploadCloud } from "lucide-react";
import type {
  Capabilities,
  DownloadQuality,
  ProcessingMode,
  UrlInspectResult,
} from "@clipforge/shared";
import { DOWNLOAD_QUALITY_LADDER, formatBytes, formatDuration } from "@clipforge/shared";
import { apiJson, messageFrom, uploadSource } from "@/lib/api";
import { Alert, Badge, ProgressBar } from "@/components/ui";
import { cn } from "@/lib/utils";

const MODE_HELP: Record<Exclude<ProcessingMode, "custom">, string> = {
  fast: "Small download, low-resolution proxy, fewer scene samples. Small on-screen text may be unreadable.",
  balanced: "A 720p download with a 480p proxy. The default for talking-head video.",
  high: "1080p download and a 720p proxy. Best for slides, charts and screen recordings.",
};

type QualityOption = {
  id: string;
  label: string;
  available?: boolean;
  closest_height?: number | null;
  height?: number | null;
};

export function SourceImporter({
  projectId,
  capabilities,
  replacing = false,
  onImported,
}: {
  projectId: string;
  capabilities: Capabilities | null;
  replacing?: boolean;
  onImported: () => void | Promise<void>;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [mode, setMode] = useState<"file" | "url">("file");
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState("");
  const [rights, setRights] = useState(false);
  const [processing, setProcessing] = useState<ProcessingMode>("balanced");
  const [quality, setQuality] = useState<DownloadQuality | null>(null);
  const [proxyHeight, setProxyHeight] = useState<string>("480");
  const [inspection, setInspection] = useState<UrlInspectResult | null>(null);
  const [inspecting, setInspecting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const maxBytes = capabilities?.limits.max_upload_bytes ?? 4 * 1024 ** 3;
  const qualities: readonly QualityOption[] =
    inspection?.qualities ?? capabilities?.download?.qualities ?? DOWNLOAD_QUALITY_LADDER;
  const modeDefaults = capabilities?.download?.processing ?? [];
  const proxyRange = capabilities?.download?.proxy_height_range ?? { min: 144, max: 1440 };

  async function importFile() {
    if (!file || busy) return;
    if (file.size > maxBytes) {
      setError(`That file is ${formatBytes(file.size)}, over the ${formatBytes(maxBytes)} limit.`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const handle = uploadSource(projectId, file, (fraction) => setProgress(Math.round(fraction * 100)));
      await handle.promise;
      setProgress(null);
      await onImported();
    } catch (caught) {
      setError(messageFrom(caught));
    } finally {
      setBusy(false);
    }
  }

  async function inspectUrl() {
    if (url.trim().length < 8 || inspecting) return;
    setInspecting(true);
    setError(null);
    try {
      const result = await apiJson<UrlInspectResult>(`/api/projects/${projectId}/sources/url/inspect`, {
        method: "POST",
        body: {
          url: url.trim(),
          quality: quality ?? null,
          processing,
          proxy_height: processing === "custom" ? Number(proxyHeight) : null,
        },
      });
      setInspection(result);
    } catch (caught) {
      setInspection(null);
      setError(messageFrom(caught));
    } finally {
      setInspecting(false);
    }
  }

  async function importUrl() {
    if (!rights || url.trim().length < 8 || busy) return;
    setBusy(true);
    setError(null);
    try {
      await apiJson(`/api/projects/${projectId}/sources/url`, {
        method: "POST",
        body: {
          url: url.trim(),
          rights_confirmed: rights,
          quality: quality ?? null,
          processing,
          proxy_height: processing === "custom" ? Number(proxyHeight) : null,
        },
      });
      setUrl("");
      setInspection(null);
      await onImported();
    } catch (caught) {
      setError(messageFrom(caught));
    } finally {
      setBusy(false);
    }
  }

  const selected = inspection?.selection as
    | { width?: number | null; height?: number | null; estimated_bytes?: number | null; explanation?: string }
    | undefined;

  return (
    <section className="surface space-y-4 p-5">
      <div className="space-y-1">
        <h2 className="text-[15px] font-semibold text-mist-50">
          {replacing ? "Replace the source video" : "Add the source video"}
        </h2>
        <p className="text-[12.5px] leading-relaxed text-mist-500">
          {replacing
            ? "A new file replaces the current source. Existing moments and exports stay, but they will point at the old timings."
            : "The file is copied into this project's workspace and verified with ffprobe before it is accepted."}
        </p>
      </div>

      <div className="segmented" role="tablist" aria-label="Import method">
        <button type="button" role="tab" aria-selected={mode === "file"} data-active={mode === "file"} onClick={() => setMode("file")}>
          <span className="flex items-center gap-1.5">
            <FileVideo className="size-3.5" strokeWidth={2} /> Upload a file
          </span>
        </button>
        <button type="button" role="tab" aria-selected={mode === "url"} data-active={mode === "url"} onClick={() => setMode("url")}>
          <span className="flex items-center gap-1.5">
            <Link2 className="size-3.5" strokeWidth={2} /> From a link
          </span>
        </button>
      </div>

      {/* Kept mounted at a stable position: the OS picker writes the selected
          path into this element's DOM value, so it must not be re-created by a
          tab switch while React owns it. */}
      <input
        ref={inputRef}
        type="file"
        accept="video/*,.mkv,.mov,.m4v,.webm"
        className="sr-only"
        hidden={mode !== "file"}
        onChange={(event) => {
          setError(null);
          setFile(event.target.files?.[0] ?? null);
          // Clearing the DOM value keeps React in control of the element and
          // lets the same file be picked again after an error.
          event.currentTarget.value = "";
        }}
        disabled={busy}
      />

      {mode === "file" ? (
        <div className="space-y-3">
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            disabled={busy}
            className="flex w-full items-center gap-3 rounded-lg border border-dashed border-white/12 bg-black/20 px-4 py-4 text-left transition-colors hover:border-white/20 disabled:opacity-60"
          >
            <UploadCloud className="size-5 shrink-0 text-mist-500" strokeWidth={1.7} />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px] text-mist-200">
                {file ? file.name : "Choose a video file"}
              </span>
              <span className="numeric block text-[11.5px] text-mist-600">
                {file ? formatBytes(file.size) : `up to ${formatBytes(maxBytes)}`}
              </span>
            </span>
          </button>
          {progress !== null ? <ProgressBar value={progress} /> : null}
          <button type="button" className="btn btn-primary btn-sm" onClick={importFile} disabled={!file || busy}>
            {busy ? <Loader2 className="size-3.5 animate-spin" strokeWidth={2} /> : null}
            {busy ? "Importing" : "Import file"}
          </button>
        </div>
      ) : (
        <div className="space-y-3">
          <input
            className="field"
            value={url}
            onChange={(event) => {
              setUrl(event.target.value);
              setInspection(null);
            }}
            placeholder="https://www.youtube.com/watch?v=…"
            inputMode="url"
            autoComplete="off"
            disabled={busy}
            aria-label="Video link"
          />

          <div className="space-y-1.5">
            <p className="label">Processing</p>
            <div className="grid gap-1.5 sm:grid-cols-2">
              {(["fast", "balanced", "high", "custom"] as ProcessingMode[]).map((id) => {
                const entry = modeDefaults.find((item) => item.id === id);
                return (
                  <button
                    key={id}
                    type="button"
                    aria-pressed={processing === id}
                    onClick={() => {
                      setProcessing(id);
                      // A custom quality must not silently override a mode that
                      // already decides it; reset the explicit choice on switch.
                      if (id !== "custom") setQuality(null);
                    }}
                    disabled={busy}
                    className={cn(
                      "rounded-md border px-2.5 py-2 text-left transition-colors",
                      processing === id
                        ? "border-ember-500/50 bg-ember-500/[0.08]"
                        : "border-white/8 bg-black/20 hover:border-white/16",
                    )}
                  >
                    <span className="text-[12.5px] font-medium text-mist-100">
                      {entry?.label ?? (id === "custom" ? "Custom" : id)}
                    </span>
                    <span className="mt-0.5 block text-[11px] leading-snug text-mist-500">
                      {id === "custom" ? "Choose the download, audio-only and the proxy height yourself." : MODE_HELP[id]}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>

          {processing === "custom" ? (
            <div className="space-y-2 rounded-md border border-white/8 bg-black/20 p-2.5">
              <p className="label">Download quality</p>
              <select
                className="field h-8 w-full text-[12px]"
                value={quality ?? ""}
                onChange={(event) => setQuality((event.target.value || null) as DownloadQuality | null)}
                disabled={busy}
              >
                <option value="">Choose a rendition</option>
                {qualities.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.label}
                    {inspection && !entry.available
                      ? ` — not offered${entry.closest_height ? `, uses ${entry.closest_height}p` : ""}`
                      : ""}
                  </option>
                ))}
              </select>
              <label className="block space-y-1 text-[11px] text-mist-500">
                <span className="flex justify-between">
                  <span>Analysis proxy height</span>
                  <span className="numeric text-mist-300">{proxyHeight}p</span>
                </span>
                <input
                  type="range"
                  min={proxyRange.min}
                  max={proxyRange.max}
                  step={24}
                  value={Number(proxyHeight)}
                  onChange={(event) => setProxyHeight(event.target.value)}
                  className="w-full accent-ember-500"
                  disabled={busy}
                />
                <span className="block text-[10.5px] leading-relaxed text-mist-600">
                  The proxy is for analysis only. Exports always read the retained source.
                </span>
              </label>
            </div>
          ) : null}

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={inspectUrl}
              disabled={url.trim().length < 8 || inspecting || busy}
            >
              {inspecting ? <Loader2 className="size-3.5 animate-spin" strokeWidth={2} /> : <Search className="size-3.5" strokeWidth={2} />}
              Check available formats
            </button>
            <span className="text-[11px] text-mist-600">Reads metadata only. Nothing is downloaded.</span>
          </div>

          {inspection ? (
            <div className="space-y-2 rounded-md border border-white/8 bg-black/20 p-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <p className="text-[12.5px] font-medium text-mist-100">{inspection.title}</p>
                {inspection.uploader ? (
                  <span className="text-[11px] text-mist-500">{inspection.uploader}</span>
                ) : null}
                <Badge tone="neutral" className="h-5 px-2 text-[10.5px]">
                  {formatDuration(inspection.duration_ms)}
                </Badge>
              </div>
              {selected?.height ? (
                <p className="numeric text-[11.5px] text-mist-400">
                  Will download {selected.width ?? "?"}×{selected.height}
                  {selected.estimated_bytes ? ` · about ${formatBytes(selected.estimated_bytes)}` : ""}
                  <span className="text-mist-600"> · size is an estimate</span>
                </p>
              ) : null}
              {selected?.explanation ? (
                <p className="text-[11.5px] leading-relaxed text-mist-500">{selected.explanation}</p>
              ) : null}
              <div className="flex flex-wrap gap-1.5">
                {inspection.qualities.map((entry) => (
                  <span
                    key={entry.id}
                    className={cn(
                      "numeric rounded border px-1.5 py-0.5 text-[10.5px]",
                      entry.available
                        ? "border-white/12 text-mist-300"
                        : "border-white/6 text-mist-600",
                    )}
                    title={
                      entry.available
                        ? "Published by this source"
                        : entry.closest_height
                          ? `Not published; ${entry.closest_height}p would be used`
                          : "Not published by this source"
                    }
                  >
                    {entry.label}
                  </span>
                ))}
              </div>
              {inspection.warnings.length ? (
                <ul className="space-y-1">
                  {inspection.warnings.map((warning) => (
                    <li key={warning} className="text-[11.5px] leading-snug text-ember-300/90">
                      {warning}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}

          <label className="flex items-start gap-2.5 text-[12.5px] text-mist-300">
            <input
              type="checkbox"
              className="mt-0.5 size-3.5 accent-ember-500"
              checked={rights}
              onChange={(event) => setRights(event.target.checked)}
              disabled={busy}
            />
            <span>I have the rights to reuse this video.</span>
          </label>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={importUrl}
            disabled={!rights || url.trim().length < 8 || busy}
          >
            {busy ? <Loader2 className="size-3.5 animate-spin" strokeWidth={2} /> : null}
            Download and import
          </button>
          <p className="text-[11.5px] text-mist-600">
            The download runs on this machine with yt-dlp. Progress appears below as a job.
          </p>
        </div>
      )}

      {error ? <Alert tone="bad">{error}</Alert> : null}
    </section>
  );
}
