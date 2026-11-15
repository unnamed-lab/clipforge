"use client";

import { useRef, useState } from "react";
import { FileVideo, Link2, Loader2, UploadCloud } from "lucide-react";
import type { Capabilities } from "@clipforge/shared";
import { formatBytes } from "@clipforge/shared";
import { apiJson, messageFrom, uploadSource } from "@/lib/api";
import { Alert, ProgressBar } from "@/components/ui";

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
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const maxBytes = capabilities?.limits.max_upload_bytes ?? 4 * 1024 ** 3;

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

  async function importUrl() {
    if (!rights || url.trim().length < 8 || busy) return;
    setBusy(true);
    setError(null);
    try {
      await apiJson(`/api/projects/${projectId}/sources/url`, {
        method: "POST",
        body: { url: url.trim(), rights_confirmed: rights },
      });
      setUrl("");
      await onImported();
    } catch (caught) {
      setError(messageFrom(caught));
    } finally {
      setBusy(false);
    }
  }

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

      {mode === "file" ? (
        <div className="space-y-3">
          <input
            ref={inputRef}
            type="file"
            accept="video/*,.mkv,.mov,.m4v,.webm"
            className="sr-only"
            onChange={(event) => {
              setError(null);
              setFile(event.target.files?.[0] ?? null);
            }}
            disabled={busy}
          />
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
            onChange={(event) => setUrl(event.target.value)}
            placeholder="https://www.youtube.com/watch?v=…"
            inputMode="url"
            autoComplete="off"
            disabled={busy}
            aria-label="Video link"
          />
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
