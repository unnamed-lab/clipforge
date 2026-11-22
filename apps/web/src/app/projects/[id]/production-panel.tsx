"use client";

import { useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Circle,
  Download,
  FolderOpen,
  Loader2,
  Play,
  RefreshCw,
  Save,
  ShieldCheck,
  Sparkles,
  Trash2,
  XCircle,
} from "lucide-react";
import type {
  Candidate,
  ExportAsset,
  ExportList,
  PipelineState,
  ProductionClip,
  ProductionFinding,
  ProductionStage,
} from "@clipforge/shared";
import { formatBytes, formatDuration } from "@clipforge/shared";
import { apiJson, messageFrom } from "@/lib/api";
import { Alert, Badge } from "@/components/ui";

const ACTIVE_RUN_STATES = new Set(["queued", "running", "retrying"]);
const RETRYABLE_RUN_STATES = new Set([
  "failed",
  "partially_completed",
  "awaiting_review",
  "cancelled",
  "retrying",
]);

const CLIP_STATUS_LABELS: Record<string, string> = {
  pending: "Waiting",
  planned: "Planned",
  configured: "Configured",
  rendering: "Rendering",
  rendered: "Rendered",
  requires_repair: "Repair needed",
  repair_planned: "Repair planned",
  needs_review: "Needs review",
  passed: "Passed",
  passed_with_warnings: "Passed with warnings",
  failed: "Failed",
};

function stageIcon(stage: ProductionStage) {
  if (stage.status === "completed") return <CheckCircle2 className="size-3.5 text-signal-good" strokeWidth={2} />;
  if (stage.status === "failed") return <XCircle className="size-3.5 text-signal-bad" strokeWidth={2} />;
  if (stage.status === "running") return <Loader2 className="size-3.5 animate-spin text-ember-300" strokeWidth={2} />;
  if (stage.status === "skipped") return <Circle className="size-3.5 text-mist-600" strokeWidth={2} />;
  return <Circle className="size-3.5 text-mist-600" strokeWidth={2} />;
}

function severityTone(severity: string): "bad" | "warn" | "neutral" {
  if (severity === "blocking") return "bad";
  if (severity === "warning") return "warn";
  return "neutral";
}

function FindingRow({ finding }: { finding: ProductionFinding }) {
  const evidence = finding.evidence ?? [];
  return (
    <li className="space-y-1 border-t border-white/5 py-2 first:border-t-0">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone={severityTone(finding.severity)}>{finding.severity}</Badge>
        <span className="text-[12px] font-medium text-mist-100">{finding.title}</span>
        <span className="text-[10.5px] uppercase tracking-wide text-mist-600">{finding.category}</span>
        {finding.auto_repairable ? <Badge tone="info">auto-repairable</Badge> : null}
        {finding.origin === "model" ? <Badge>model note</Badge> : null}
      </div>
      <p className="text-[11.5px] leading-relaxed text-mist-400">{finding.detail}</p>
      {finding.output_range ? (
        <p className="numeric text-[10.5px] text-mist-600">
          output {finding.output_range.start_ms}–{finding.output_range.end_ms} ms
        </p>
      ) : null}
      {evidence.length > 0 ? (
        <p className="text-[10.5px] text-mist-600">{evidence.join(" · ")}</p>
      ) : null}
    </li>
  );
}

function ClipCard({
  clip,
  title,
  busy,
  onApprove,
  onRemove,
}: {
  clip: ProductionClip;
  title: string;
  busy: boolean;
  onApprove: (clipId: string, approved: boolean) => void;
  onRemove: (clip: ProductionClip) => void;
}) {
  const review = clip.review ?? {
    summary: "",
    findings: [],
    checks: [],
    model_review: {},
    remaining_warnings: [],
  };
  const findings = clip.findings?.length ? clip.findings : review.findings ?? [];
  const modelReview = (review.model_review ?? {}) as Record<string, unknown>;
  return (
    <article className="rounded-lg border border-white/8 bg-black/20 px-3 py-2.5">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-[12.5px] font-medium text-mist-100">{title}</p>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <Badge
              tone={
                clip.status === "passed"
                  ? "good"
                  : clip.status === "passed_with_warnings"
                    ? "warn"
                    : clip.status === "failed"
                      ? "bad"
                      : "neutral"
              }
            >
              {CLIP_STATUS_LABELS[clip.status] ?? clip.status}
            </Badge>
            <span className="text-[10.5px] text-mist-500">
              {clip.strategy ? clip.strategy.replaceAll("_", " ") : "strategy pending"}
            </span>
            <span className="numeric text-[10.5px] text-mist-600">EDL v{clip.edl_version}</span>
            {clip.repair_attempts > 0 ? (
              <span className="text-[10.5px] text-mist-500">{clip.repair_attempts} repair(s)</span>
            ) : null}
            {clip.technical_errors > 0 ? (
              <span className="text-[10.5px] text-signal-warn">{clip.technical_errors} technical retry</span>
            ) : null}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {clip.render_id ? (
            <>
              <a className="btn btn-ghost btn-sm h-6 px-2 text-[11px]" href={`/api/renders/${clip.render_id}/stream`}>
                <Play className="size-3" strokeWidth={2} />
                Preview
              </a>
              <a className="btn btn-sm h-6 px-2 text-[11px]" href={`/api/renders/${clip.render_id}/download`}>
                <Download className="size-3" strokeWidth={2} />
                MP4
              </a>
            </>
          ) : null}
          {clip.status === "needs_review" && clip.render_id ? (
            <button
              type="button"
              className="btn btn-sm h-6 px-2 text-[11px]"
              disabled={busy}
              onClick={() => onApprove(clip.id, true)}
            >
              <ShieldCheck className="size-3" strokeWidth={2} />
              Approve
            </button>
          ) : null}
          <button
            type="button"
            className="btn btn-ghost btn-sm h-6 px-2 text-[11px] text-mist-500 hover:text-signal-bad"
            disabled={busy}
            title="Remove this clip from the production list. Its exports are kept."
            onClick={() => onRemove(clip)}
          >
            <Trash2 className="size-3" strokeWidth={2} />
          </button>
        </div>
      </div>
      <p className="mt-1.5 text-[11px] leading-relaxed text-mist-500">
        {review.summary || "The rendered file has not been reviewed yet."}
      </p>
      {modelReview?.status ? (
        <p className="mt-1 text-[10.5px] text-mist-600">
          Model review: {String(modelReview.status)}
          {modelReview.basis ? ` · ${String(modelReview.basis)}` : ""}
        </p>
      ) : null}
      {findings.length > 0 ? (
        <details className="mt-2">
          <summary className="cursor-pointer text-[11px] text-mist-400">
            {findings.length} finding(s) and evidence
          </summary>
          <ul className="mt-1">
            {findings.map((finding, index) => (
              <FindingRow key={`${finding.id}-${index}`} finding={finding} />
            ))}
          </ul>
        </details>
      ) : null}
      {review.approved_by_user ? (
        <p className="mt-1 text-[10.5px] text-signal-good">Approved by you after review.</p>
      ) : null}
    </article>
  );
}

function ExportRow({
  asset,
  busy,
  onSave,
  onDelete,
}: {
  asset: ExportAsset;
  busy: boolean;
  onSave: (asset: ExportAsset) => void;
  onDelete: (asset: ExportAsset) => void;
}) {
  const saved = asset.saved ?? [];
  const firstSave = saved[0];
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-white/5 py-1.5 first:border-t-0">
      <div className="min-w-0">
        <p className="truncate text-[11.5px] text-mist-200">{asset.filename}</p>
        <p className="numeric text-[10.5px] text-mist-600">
          v{String(asset.version).padStart(3, "0")} · {asset.review_status ?? "unreviewed"}
          {asset.size_bytes ? ` · ${formatBytes(asset.size_bytes)}` : ""}
          {asset.duration_ms ? ` · ${formatDuration(asset.duration_ms)}` : ""}
          {asset.current ? " · current" : asset.superseded ? " · superseded" : ""}
        </p>
        {firstSave ? (
          <p className="flex items-center gap-1 text-[10.5px] text-signal-good">
            <FolderOpen className="size-3" strokeWidth={2} />
            Saved to {firstSave.directory}/{firstSave.filename}
          </p>
        ) : null}
      </div>
      <div className="flex items-center gap-1.5">
        <a className="btn btn-ghost btn-sm h-6 px-2 text-[11px]" href={`/api/renders/${asset.render_id}/stream`}>
          Preview
        </a>
        <button
          type="button"
          className="btn btn-ghost btn-sm h-6 px-2 text-[11px]"
          disabled={busy}
          title="Save this file to the project export folder"
          onClick={() => onSave(asset)}
        >
          <Save className="size-3" strokeWidth={2} />
        </button>
        <a className="btn btn-sm h-6 px-2 text-[11px]" href={`/api/renders/${asset.render_id}/download`}>
          <Download className="size-3" strokeWidth={2} />
        </a>
        <button
          type="button"
          className="btn btn-ghost btn-sm h-6 px-2 text-[11px] text-mist-500 hover:text-signal-bad"
          disabled={busy}
          title="Delete this export from Clipforge storage. A copy already in the export folder is not removed."
          onClick={() => onDelete(asset)}
        >
          <Trash2 className="size-3" strokeWidth={2} />
        </button>
      </div>
    </div>
  );
}

export function ProductionPanel({
  projectId,
  pipeline,
  exports,
  candidates,
  onRefresh,
}: {
  projectId: string;
  pipeline: PipelineState | null;
  exports: ExportList | null;
  candidates: Candidate[];
  onRefresh: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showOptions, setShowOptions] = useState(false);
  const [clipCount, setClipCount] = useState(5);
  const [aspect, setAspect] = useState<"auto" | "9:16" | "16:9" | "1:1">("auto");
  const [exportDirInput, setExportDirInput] = useState("");

  const run = pipeline?.run ?? null;
  const active = run ? ACTIVE_RUN_STATES.has(run.status) : false;
  const retryable = run ? RETRYABLE_RUN_STATES.has(run.status) : false;
  const titles = useMemo(
    () => new Map(candidates.map((candidate) => [candidate.id, candidate.title])),
    [candidates],
  );
  const stageList = pipeline?.stages ?? [];
  const counts = pipeline?.counts ?? {};
  const currentExports = exports?.current ?? [];
  const superseded = exports?.superseded ?? [];
  const providerNote = run?.provider as Record<string, unknown> | undefined;
  const fallbackMode = providerNote?.mode === "deterministic_fallback";
  const exportSettings = (pipeline?.capabilities?.export_settings ?? {}) as Record<string, unknown>;
  const effectiveExportDir =
    typeof exportSettings.effective_dir === "string" ? exportSettings.effective_dir : null;
  const customExportDir =
    typeof exportSettings.custom_dir === "string" ? exportSettings.custom_dir : "";
  const clipLimit = Math.max(
    1,
    Number(pipeline?.capabilities?.clip_limit) || 8,
  );
  const clipChoices = [1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 30].filter(
    (value) => value <= clipLimit,
  );

  async function start() {
    setBusy(true);
    setError(null);
    try {
      await apiJson(`/api/projects/${projectId}/autoproduce`, {
        method: "POST",
        body: {
          clip_count: clipCount,
          ...(aspect === "auto" ? {} : { aspect }),
        },
      });
      await onRefresh();
    } catch (caught) {
      setError(messageFrom(caught));
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (!run) return;
    setBusy(true);
    setError(null);
    try {
      await apiJson(`/api/pipeline/${run.id}/cancel`, { method: "POST" });
      await onRefresh();
    } catch (caught) {
      setError(messageFrom(caught));
    } finally {
      setBusy(false);
    }
  }

  async function retry() {
    if (!run) return;
    setBusy(true);
    setError(null);
    try {
      await apiJson(`/api/pipeline/${run.id}/retry`, { method: "POST" });
      await onRefresh();
    } catch (caught) {
      setError(messageFrom(caught));
    } finally {
      setBusy(false);
    }
  }

  async function approve(clipId: string, approved: boolean) {
    if (!run) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const response = await apiJson<{ saved?: { filename?: string; directory?: string; error?: string } }>(
        `/api/pipeline/${run.id}/clips/${clipId}/approve`,
        { method: "POST", body: { approved } },
      );
      if (response.saved?.error) {
        setError(response.saved.error);
      } else if (response.saved?.filename) {
        setNotice(`Saved ${response.saved.filename} to ${response.saved.directory}`);
      }
      await onRefresh();
    } catch (caught) {
      setError(messageFrom(caught));
    } finally {
      setBusy(false);
    }
  }

  async function removeClip(clip: ProductionClip) {
    if (!run) return;
    if (
      !window.confirm(
        "Remove this clip from the production list? Its export versions stay available under Export versions.",
      )
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await apiJson(`/api/pipeline/${run.id}/clips/${clip.id}`, { method: "DELETE" });
      await onRefresh();
    } catch (caught) {
      setError(messageFrom(caught));
    } finally {
      setBusy(false);
    }
  }

  async function deleteExport(asset: ExportAsset) {
    if (
      !window.confirm(
        `Delete ${asset.filename} from Clipforge storage? A copy already in the export folder is not removed.`,
      )
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await apiJson(`/api/exports/${asset.render_id}`, { method: "DELETE" });
      await onRefresh();
    } catch (caught) {
      setError(messageFrom(caught));
    } finally {
      setBusy(false);
    }
  }

  async function saveExport(asset: ExportAsset) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const response = await apiJson<{ saved: { filename: string; directory: string } }>(
        `/api/exports/${asset.render_id}/save`,
        { method: "POST" },
      );
      setNotice(`Saved ${response.saved.filename} to ${response.saved.directory}`);
      await onRefresh();
    } catch (caught) {
      setError(messageFrom(caught));
    } finally {
      setBusy(false);
    }
  }

  async function saveAllExports() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const response = await apiJson<{
        saved: Array<{ filename: string; directory: string }>;
        errors: string[];
      }>(`/api/projects/${projectId}/exports/save`, { method: "POST" });
      if (response.errors.length > 0) {
        setError(response.errors.join(" · "));
      }
      setNotice(
        response.saved.length > 0
          ? `Saved ${response.saved.length} clip(s) to ${response.saved[0]?.directory ?? "the export folder"}`
          : "No new files needed saving.",
      );
      await onRefresh();
    } catch (caught) {
      setError(messageFrom(caught));
    } finally {
      setBusy(false);
    }
  }

  async function saveExportDirectory() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const response = await apiJson<{
        export_settings: { effective_dir: string };
      }>(`/api/projects/${projectId}/export-settings`, {
        method: "PUT",
        body: { export_dir: exportDirInput.trim() || null },
      });
      setNotice(`Export folder set to ${response.export_settings.effective_dir}`);
      await onRefresh();
    } catch (caught) {
      setError(messageFrom(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="surface space-y-3 px-4 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="label flex items-center gap-1.5">
            <Sparkles className="size-3.5 text-ember-300" strokeWidth={2} />
            Automatic production
          </p>
          <p className="mt-1 max-w-2xl text-[12px] leading-relaxed text-mist-400">
            Analyses the source, selects moments, configures each edit, renders it, reviews the actual
            output and repairs supported defects. Manual settings and previous exports are kept.
            {effectiveExportDir ? ` Approved clips are copied to ${effectiveExportDir}.` : ""}
          </p>
        </div>
        <div className="flex items-center gap-1.5">
          {active ? (
            <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void cancel()}>
              {busy ? <Loader2 className="size-3.5 animate-spin" /> : null}
              Cancel
            </button>
          ) : (
            <button type="button" className="btn btn-primary btn-sm" disabled={busy} onClick={() => void start()}>
              {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" strokeWidth={2} />}
              {run ? "Produce again" : "Produce clips"}
            </button>
          )}
          {retryable && !active ? (
            <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void retry()}>
              <RefreshCw className="size-3.5" strokeWidth={2} />
              Resume
            </button>
          ) : null}
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            aria-expanded={showOptions}
            onClick={() => setShowOptions((value) => !value)}
          >
            Options
            <ChevronDown className={`size-3.5 transition-transform ${showOptions ? "rotate-180" : ""}`} />
          </button>
        </div>
      </div>

      {error ? <Alert tone="bad">{error}</Alert> : null}
      {notice ? <Alert tone="good">{notice}</Alert> : null}

      {showOptions ? (
        <div className="grid gap-2 rounded-lg border border-white/8 bg-black/20 p-3 sm:grid-cols-2">
          <label className="space-y-1 text-[11px] text-mist-400">
            How many clips
            <select
              className="field h-8 text-[12px]"
              value={clipCount}
              onChange={(event) => setClipCount(Number(event.target.value))}
            >
              {clipChoices.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label className="space-y-1 text-[11px] text-mist-400">
            Frame
            <select
              className="field h-8 text-[12px]"
              value={aspect}
              onChange={(event) => setAspect(event.target.value as typeof aspect)}
            >
              <option value="auto">Project default</option>
              <option value="9:16">Portrait 9:16</option>
              <option value="16:9">Landscape 16:9</option>
              <option value="1:1">Square 1:1</option>
            </select>
          </label>
          <div className="space-y-1 text-[11px] text-mist-400 sm:col-span-2">
            <span>Export folder on the worker machine</span>
            <div className="flex flex-wrap gap-1.5">
              <input
                className="field h-8 min-w-0 flex-1 text-[12px]"
                value={exportDirInput}
                onChange={(event) => setExportDirInput(event.target.value)}
                placeholder={customExportDir || effectiveExportDir || "~/Documents/Clipforge"}
                aria-label="Export folder"
              />
              <button
                type="button"
                className="btn btn-sm h-8"
                disabled={busy}
                onClick={() => void saveExportDirectory()}
              >
                <FolderOpen className="size-3.5" strokeWidth={2} />
                Set folder
              </button>
            </div>
            <p className="text-[10.5px] text-mist-600">
              Leave blank to use the default. Each project gets its own subfolder. Saved copies are
              not deleted when you remove a clip here.
            </p>
          </div>
          <p className="text-[10.5px] leading-relaxed text-mist-600 sm:col-span-2">
            Everything else uses the project and client profile defaults. Selection is an editorial
            estimate from source evidence, not a prediction of reach.
          </p>
        </div>
      ) : null}

      {run ? (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge
                tone={
                  run.status === "completed"
                    ? "good"
                    : run.status === "failed"
                      ? "bad"
                      : run.status === "partially_completed" || run.status === "awaiting_review"
                        ? "warn"
                        : "info"
                }
              >
                {run.status.replaceAll("_", " ")}
              </Badge>
              {run.current_stage ? <Badge>{run.current_stage.replaceAll("_", " ").toLowerCase()}</Badge> : null}
              {fallbackMode ? <Badge tone="neutral">heuristic editorial fallback</Badge> : null}
              {!fallbackMode && providerNote?.model ? (
                <Badge tone="neutral">{String(providerNote.model)}</Badge>
              ) : null}
            </div>
            <p className="text-[11px] text-mist-500">{run.message}</p>
          </div>

          <div>
            <div className="h-1.5 overflow-hidden rounded-full bg-white/8">
              <div
                className="h-full rounded-full bg-ember-400 transition-[width] duration-500"
                style={{ width: `${Math.max(0, Math.min(100, run.progress))}%` }}
              />
            </div>
            <p className="numeric mt-1 text-[10.5px] text-mist-600">
              {run.progress.toFixed(0)}% · progress is reported by the worker, not estimated
            </p>
          </div>

          <ol className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
            {stageList.map((stage) => (
              <li key={stage.name} className="flex items-start gap-1.5 text-[11px]">
                <span className="mt-0.5">{stageIcon(stage)}</span>
                <span className="min-w-0">
                  <span
                    className={
                      stage.status === "running"
                        ? "text-mist-100"
                        : stage.status === "completed"
                          ? "text-mist-300"
                          : "text-mist-600"
                    }
                  >
                    {stage.label}
                  </span>
                  {stage.status === "running" && stage.message ? (
                    <span className="block truncate text-[10.5px] text-mist-500">{stage.message}</span>
                  ) : null}
                  {stage.status === "failed" && stage.error ? (
                    <span className="block text-[10.5px] text-signal-bad">{stage.error}</span>
                  ) : null}
                </span>
              </li>
            ))}
          </ol>

          <div className="flex flex-wrap gap-3 text-[11px] text-mist-500">
            <span>{counts.total ?? 0} planned</span>
            <span>{counts.passed ?? 0} clean</span>
            <span>{counts.passed_with_warnings ?? 0} with warnings</span>
            <span>{counts.needs_review ?? 0} need review</span>
            <span>{counts.failed ?? 0} failed</span>
          </div>

          {run.error_message ? <Alert tone="bad">{run.error_message}</Alert> : null}
        </div>
      ) : null}

      {pipeline?.clips?.length ? (
        <div className="space-y-2">
          <p className="label">Rendered clips</p>
          {pipeline.clips.map((clip) => (
            <ClipCard
              key={clip.id}
              clip={clip}
              title={titles.get(clip.candidate_id ?? "") ?? `Clip ${clip.ordinal + 1}`}
              busy={busy}
              onApprove={(clipId, approved) => void approve(clipId, approved)}
              onRemove={(item) => void removeClip(item)}
            />
          ))}
        </div>
      ) : null}

      {currentExports.length > 0 || superseded.length > 0 ? (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <p className="label">Export versions</p>
            <div className="flex items-center gap-2">
              <span className="text-[10.5px] text-mist-600">
                {currentExports.length} current · {superseded.length} preserved
              </span>
              {currentExports.length > 0 ? (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm h-6 px-2 text-[11px]"
                  disabled={busy}
                  onClick={() => void saveAllExports()}
                >
                  <Save className="size-3" strokeWidth={2} />
                  Save all
                </button>
              ) : null}
            </div>
          </div>
          {[...currentExports, ...superseded].slice(0, 20).map((asset) => (
            <ExportRow
              key={asset.asset_id}
              asset={asset}
              busy={busy}
              onSave={(item) => void saveExport(item)}
              onDelete={(item) => void deleteExport(item)}
            />
          ))}
        </div>
      ) : null}

      {run?.status === "awaiting_review" ? (
        <p className="flex items-start gap-1.5 text-[11px] leading-relaxed text-signal-warn">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" strokeWidth={2} />
          Clips were rendered but did not pass every check. Inspect the findings, repair or approve them;
          nothing is marked complete until the rendered file passes technical validation.
        </p>
      ) : null}
    </section>
  );
}
