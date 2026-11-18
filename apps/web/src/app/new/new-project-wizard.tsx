"use client";

import { useCallback, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { FileVideo, Link2, Loader2, ShieldCheck, UploadCloud, X } from "lucide-react";
import type { Capabilities } from "@clipforge/shared";
import { formatBytes } from "@clipforge/shared";
import { apiJson, messageFrom, uploadSource } from "@/lib/api";
import { Alert, ProgressBar } from "@/components/ui";
import { cn } from "@/lib/utils";

type Mode = "file" | "url";

type CreatedProject = { project: { id: string; name: string } };

export function NewProjectWizard({ capabilities }: { capabilities: Capabilities | null }) {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<(() => void) | null>(null);

  const [mode, setMode] = useState<Mode>("file");
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState("");
  const [rights, setRights] = useState(false);
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState<string | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const maxBytes = capabilities?.limits.max_upload_bytes ?? 4 * 1024 ** 3;
  const maxMinutes = Math.round((capabilities?.limits.max_duration_ms ?? 180 * 60_000) / 60_000);

  const chooseFile = useCallback((next: File | null) => {
    setError(null);
    if (!next) {
      setFile(null);
      return;
    }
    if (next.size > maxBytes) {
      setError(`That file is ${formatBytes(next.size)}, over the ${formatBytes(maxBytes)} limit for this workspace.`);
      return;
    }
    setFile(next);
    setName((current) => current || stripExtension(next.name));
  }, [maxBytes]);

  const onDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    const dropped = event.dataTransfer.files?.[0];
    if (dropped) chooseFile(dropped);
  };

  const canSubmit =
    !busy &&
    (mode === "file" ? Boolean(file) : url.trim().length > 7 && rights);

  async function submit() {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    setProgress(null);
    setStage("Creating the project");
    try {
      const projectName = name.trim() || (mode === "file" && file ? stripExtension(file.name) : "Untitled project");
      const created = await apiJson<CreatedProject>("/api/projects", {
        method: "POST",
        body: { name: projectName },
      });
      const projectId = created.project.id;

      if (mode === "file" && file) {
        setStage("Copying the file into the local workspace");
        const handle = uploadSource(projectId, file, (fraction) => {
          setProgress(Math.round(fraction * 100));
          if (fraction >= 1) setStage("Verifying the video container");
        });
        abortRef.current = handle.abort;
        await handle.promise;
        abortRef.current = null;
      } else {
        setStage("Checking the link");
        await apiJson<{ job: unknown }>(`/api/projects/${projectId}/sources/url`, {
          method: "POST",
          body: { url: url.trim(), rights_confirmed: rights },
        });
      }

      setStage("Opening the workspace");
      router.push(`/projects/${projectId}`);
    } catch (caught) {
      setError(messageFrom(caught));
      setStage(null);
      setBusy(false);
      setProgress(null);
      abortRef.current = null;
    }
  }

  return (
    <div className="space-y-5">
      <div className="surface space-y-5 p-5">
        <div className="space-y-1.5">
          <label htmlFor="project-name" className="label">
            Project name
          </label>
          <input
            id="project-name"
            className="field"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Episode 42 — the founder interview"
            maxLength={120}
            disabled={busy}
          />
          <p className="text-[11.5px] text-mist-600">
            Only used inside Clipforge. It becomes part of the exported file name.
          </p>
        </div>

        <div className="space-y-3">
          <div className="segmented" role="tablist" aria-label="Source type">
            <button
              type="button"
              role="tab"
              aria-selected={mode === "file"}
              data-active={mode === "file"}
              onClick={() => setMode("file")}
              disabled={busy}
            >
              <span className="flex items-center gap-1.5">
                <FileVideo className="size-3.5" strokeWidth={2} />
                Upload a file
              </span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "url"}
              data-active={mode === "url"}
              onClick={() => setMode("url")}
              disabled={busy}
            >
              <span className="flex items-center gap-1.5">
                <Link2 className="size-3.5" strokeWidth={2} />
                From a link
              </span>
            </button>
          </div>

          {mode === "file" ? (
            <div
              onDragOver={(event) => {
                event.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
              className={cn(
                "rounded-xl border border-dashed px-4 py-8 text-center transition-colors sm:px-5",
                dragging ? "border-ember-500/60 bg-ember-500/[0.06]" : "border-white/12 bg-black/20",
              )}
            >
              <input
                ref={inputRef}
                type="file"
                accept="video/*,.mkv,.mov,.m4v,.webm"
                className="sr-only"
                onChange={(event) => chooseFile(event.target.files?.[0] ?? null)}
                disabled={busy}
              />
              {file ? (
                <div className="mx-auto flex max-w-md items-center gap-3 rounded-lg border border-white/10 bg-ink-850 px-3 py-2.5 text-left">
                  <FileVideo className="size-5 shrink-0 text-ember-400" strokeWidth={1.8} />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[13px] text-mist-100">{file.name}</p>
                    <p className="numeric text-[11.5px] text-mist-500">
                      {formatBytes(file.size)} · {file.type || "unknown type"}
                    </p>
                  </div>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm px-2"
                    onClick={() => chooseFile(null)}
                    disabled={busy}
                    aria-label="Remove selected file"
                  >
                    <X className="size-3.5" strokeWidth={2} />
                  </button>
                </div>
              ) : (
                <div className="space-y-2.5">
                  <UploadCloud className="mx-auto size-7 text-mist-600" strokeWidth={1.6} />
                  <p className="text-[13px] text-mist-200">Drop a video here, or choose a file</p>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => inputRef.current?.click()}
                    disabled={busy}
                  >
                    Choose file
                  </button>
                  <p className="text-[11.5px] text-mist-600">
                    MP4, MOV, MKV, WebM · up to {formatBytes(maxBytes)} and {maxMinutes} minutes
                  </p>
                </div>
              )}
            </div>
          ) : (
            <div className="space-y-3">
              <div className="space-y-1.5">
                <label htmlFor="source-url" className="label">
                  Video link
                </label>
                <input
                  id="source-url"
                  className="field"
                  value={url}
                  onChange={(event) => setUrl(event.target.value)}
                  placeholder="https://www.youtube.com/watch?v=…"
                  inputMode="url"
                  autoComplete="off"
                  disabled={busy}
                />
                <p className="text-[11.5px] text-mist-600">
                  Public pages only. Private, live and playlist links are rejected. The download runs
                  through yt-dlp on this machine.
                </p>
              </div>
              <label className="flex items-start gap-2.5 rounded-lg border border-white/8 bg-black/20 px-3 py-2.5 text-[12.5px] text-mist-300">
                <input
                  type="checkbox"
                  className="mt-0.5 size-3.5 accent-ember-500"
                  checked={rights}
                  onChange={(event) => setRights(event.target.checked)}
                  disabled={busy}
                />
                <span>
                  I have the rights to reuse this video, or the rights holder allows it. Clipforge will not
                  publish anything for me.
                </span>
              </label>
            </div>
          )}
        </div>

        {error ? (
          <Alert tone="bad" title="Could not start this project">
            {error}
          </Alert>
        ) : null}

        {stage ? (
          <div className="space-y-2 rounded-lg border border-white/8 bg-black/25 px-3.5 py-3">
            <div className="flex items-center justify-between gap-3 text-[12.5px] text-mist-300">
              <span className="flex items-center gap-2">
                <Loader2 className="size-3.5 animate-spin" strokeWidth={2} />
                {stage}
              </span>
              {progress !== null ? <span className="numeric text-mist-500">{progress}%</span> : null}
            </div>
            {progress !== null ? <ProgressBar value={progress} /> : null}
            {busy ? (
              <button
                type="button"
                className="btn btn-ghost btn-sm h-7 px-2 text-mist-500"
                onClick={() => abortRef.current?.()}
              >
                Cancel upload
              </button>
            ) : null}
          </div>
        ) : null}

        <div className="flex flex-col-reverse gap-3 border-t border-white/6 pt-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="flex items-center gap-1.5 text-[11.5px] text-mist-600">
            <ShieldCheck className="size-3.5 shrink-0" strokeWidth={2} />
            Files stay on this machine. Nothing is uploaded to a cloud service.
          </p>
          <button type="button" className="btn btn-primary w-full sm:w-auto" onClick={submit} disabled={!canSubmit}>
            {busy ? <Loader2 className="size-4 animate-spin" strokeWidth={2} /> : null}
            Import and open
          </button>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        {[
          {
            step: "2",
            title: "Transcribe",
            body: "Speech is transcribed on the CPU with word-level timings. Roughly real time on a laptop.",
          },
          {
            step: "3",
            title: "Rank moments",
            body: "Candidate clips are cut on sentence boundaries, then scored with explainable signals.",
          },
          {
            step: "4",
            title: "Review and export",
            body: "Tighten the edges on a waveform, choose captions, and render an MP4 locally.",
          },
        ].map((item) => (
          <div key={item.step} className="surface px-4 py-3.5">
            <p className="label">Step {item.step}</p>
            <p className="mt-1 text-[13px] font-medium text-mist-100">{item.title}</p>
            <p className="mt-1 text-[12px] leading-relaxed text-mist-500">{item.body}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function stripExtension(filename: string): string {
  const cut = filename.lastIndexOf(".");
  return (cut > 0 ? filename.slice(0, cut) : filename).replace(/[_-]+/g, " ").trim().slice(0, 120);
}
