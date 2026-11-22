"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowLeft,
  ChevronDown,
  FileAudio,
  RefreshCw,
  Trash2,
  Wand2,
} from "lucide-react";
import type {
  Capabilities,
  Candidate,
  ExportList,
  Job,
  PipelineState,
  ProjectDetail,
  Transcript,
} from "@clipforge/shared";
import { formatBytes, formatDuration, formatTimecode } from "@clipforge/shared";
import { apiJson, messageFrom } from "@/lib/api";
import { Alert, Badge } from "@/components/ui";
import { JobPanel } from "@/components/job-panel";
import { AnalysisPanel } from "./analysis-panel";
import { CandidateRail } from "./candidate-rail";
import { ClipEditor, type CandidatePatch } from "./clip-editor";
import { DiscoveryCounts, DiscoveryPanel } from "./discovery-panel";
import { ProductionPanel } from "./production-panel";
import { SourceImporter } from "./source-importer";

const ACTIVE_STATES = new Set(["queued", "running", "waiting"]);

export function ProjectWorkspace({
  detail: initialDetail,
  capabilities,
  transcript: initialTranscript,
  pipeline: initialPipeline,
  exports: initialExports,
}: {
  detail: ProjectDetail;
  capabilities: Capabilities | null;
  transcript: Transcript | null;
  pipeline: PipelineState | null;
  exports: ExportList | null;
}) {
  const router = useRouter();
  const [detail, setDetail] = useState(initialDetail);
  const [transcript, setTranscript] = useState(initialTranscript);
  const [pipeline, setPipeline] = useState(initialPipeline);
  const [exports, setExports] = useState(initialExports);
  const [selectedId, setSelectedId] = useState<string | null>(initialDetail.candidates[0]?.id ?? null);
  const [showRerun, setShowRerun] = useState(false);
  const [showDiscovery, setShowDiscovery] = useState(false);
  const [replacingSource, setReplacingSource] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasWaveform, setHasWaveform] = useState(false);
  const [railCounts, setRailCounts] = useState({ displayed: initialDetail.candidates.length, selected: 0 });
  const [batchBusy, setBatchBusy] = useState(false);
  const transcriptRef = useRef(initialTranscript);
  const discoveryRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<HTMLDivElement>(null);

  const projectId = detail.project.id;
  const activeJob = detail.jobs.find((job) => ACTIVE_STATES.has(job.state));
  const latestJob = detail.jobs[0] ?? null;
  const selected = useMemo(
    () => detail.candidates.find((candidate) => candidate.id === selectedId) ?? detail.candidates[0] ?? null,
    [detail.candidates, selectedId],
  );
  const convertedIds = useMemo(
    () =>
      new Set(
        detail.renders
          .filter((render) => render.status === "completed" && render.candidate_id)
          .map((render) => render.candidate_id as string),
      ),
    [detail.renders],
  );

  const refresh = useCallback(async () => {
    try {
      const [next, nextPipeline, nextExports] = await Promise.all([
        apiJson<ProjectDetail>(`/api/projects/${projectId}`),
        apiJson<PipelineState>(`/api/projects/${projectId}/pipeline`).catch(() => null),
        apiJson<ExportList>(`/api/projects/${projectId}/exports`).catch(() => null),
      ]);
      setDetail(next);
      if (nextPipeline) setPipeline(nextPipeline);
      if (nextExports) setExports(nextExports);
      setError(null);
      if (next.source) {
        const media = await apiJson<{ waveform: boolean }>(`/api/projects/${projectId}/media`).catch(
          () => null,
        );
        if (media) setHasWaveform(media.waveform);
        if (!transcriptRef.current) {
          const response = await apiJson<{ transcript: Transcript }>(
            `/api/projects/${projectId}/transcript`,
          ).catch(() => null);
          if (response) {
            transcriptRef.current = response.transcript;
            setTranscript(response.transcript);
          }
        }
      }
    } catch (caught) {
      setError(messageFrom(caught));
    }
  }, [projectId]);

  useEffect(() => {
    if (!activeJob) return;
    const timer = window.setInterval(() => {
      void refresh();
    }, 1800);
    return () => window.clearInterval(timer);
  }, [activeJob, refresh]);

  useEffect(() => {
    if (!detail.candidates.some((candidate) => candidate.id === selectedId)) {
      setSelectedId(detail.candidates[0]?.id ?? null);
    }
  }, [detail.candidates, selectedId]);

  async function cancelJob(job: Job) {
    try {
      await apiJson(`/api/jobs/${job.id}/cancel`, { method: "POST" });
      await refresh();
    } catch (caught) {
      setError(messageFrom(caught));
    }
  }

  async function retryJob(job: Job) {
    try {
      await apiJson(`/api/jobs/${job.id}/retry`, { method: "POST" });
      await refresh();
    } catch (caught) {
      setError(messageFrom(caught));
    }
  }

  function applyCandidate(next: Candidate) {
    setDetail((current) => ({
      ...current,
      candidates: current.candidates.map((candidate) => (candidate.id === next.id ? next : candidate)),
    }));
  }

  async function patchCandidate(candidateId: string, patch: CandidatePatch) {
    const response = await apiJson<{ candidate: Candidate }>(
      `/api/projects/${projectId}/candidates/${candidateId}`,
      { method: "PATCH", body: patch },
    );
    setDetail((current) => ({
      ...current,
      candidates: current.candidates.map((candidate) =>
        candidate.id === response.candidate.id ? response.candidate : candidate,
      ),
    }));
  }

  async function renderClip(body: Record<string, unknown>) {
    await apiJson(`/api/projects/${projectId}/renders`, { method: "POST", body });
    await refresh();
  }

  async function createClips(candidates: Candidate[]) {
    if (candidates.length === 0) return;
    setBatchBusy(true);
    setError(null);
    try {
      for (const candidate of candidates) {
        await apiJson(`/api/projects/${projectId}/renders`, {
          method: "POST",
          body: {
            candidate_id: candidate.id,
            include_hook: true,
            apply_editorial: true,
          },
        });
      }
      await refresh();
    } catch (caught) {
      setError(messageFrom(caught));
      await refresh();
    } finally {
      setBatchBusy(false);
    }
  }

  function previewSelected(candidates: Candidate[]) {
    const first = candidates[0];
    if (!first) return;
    setSelectedId(first.id);
    window.requestAnimationFrame(() => {
      editorRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  function openDiscovery() {
    setShowDiscovery(true);
    window.requestAnimationFrame(() => {
      discoveryRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  async function reviewBoundary(
    candidateId: string,
    action: { action: "accept" | "reject" | "reset"; end_ms?: number },
  ) {
    const response = await apiJson<{ candidate: Candidate }>(
      `/api/projects/${projectId}/candidates/${candidateId}/boundary`,
      { method: "POST", body: action },
    );
    setDetail((current) => ({
      ...current,
      candidates: current.candidates.map((candidate) =>
        candidate.id === response.candidate.id ? response.candidate : candidate,
      ),
    }));
  }

  async function reEvaluateEditorial(
    candidateId: string,
    action: { strategy: string; pacing_profile?: string; apply?: boolean },
  ) {
    const response = await apiJson<{ candidate: Candidate }>(
      `/api/projects/${projectId}/candidates/${candidateId}/editorial`,
      { method: "POST", body: action },
    );
    setDetail((current) => ({
      ...current,
      candidates: current.candidates.map((candidate) =>
        candidate.id === response.candidate.id ? response.candidate : candidate,
      ),
    }));
  }

  async function renameProject(name: string) {
    const trimmed = name.trim();
    if (!trimmed || trimmed === detail.project.name) return;
    const response = await apiJson<{ project: ProjectDetail["project"] }>(`/api/projects/${projectId}`, {
      method: "PATCH",
      body: { name: trimmed },
    });
    setDetail((current) => ({ ...current, project: response.project }));
  }

  async function deleteProject() {
    try {
      await apiJson(`/api/projects/${projectId}`, { method: "DELETE" });
      router.push("/");
    } catch (caught) {
      setError(messageFrom(caught));
      setDeleting(false);
    }
  }

  const source = detail.source;
  const analysisRunning = detail.jobs.some((job) => job.type === "analysis" && ACTIVE_STATES.has(job.state));
  const renderRunning = detail.jobs.some((job) => job.type === "render" && ACTIVE_STATES.has(job.state));

  return (
    <div className="space-y-5">
      <div className="space-y-3">
        <Link href="/" className="inline-flex items-center gap-1.5 text-[12px] text-mist-500 hover:text-mist-200">
          <ArrowLeft className="size-3.5" strokeWidth={2} />
          All projects
        </Link>

        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0 space-y-2">
            <ProjectTitle name={detail.project.name} onRename={renameProject} />
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge tone={detail.project.status === "analysing" ? "info" : source ? "good" : "neutral"}>
                {detail.project.status}
              </Badge>
              {source ? (
                <>
                  <Badge>{source.kind === "url" ? "imported link" : "local file"}</Badge>
                  <span className="numeric text-[11.5px] text-mist-500">
                    {formatDuration(source.duration_ms)} · {source.width ?? "?"}×{source.height ?? "?"} ·{" "}
                    {formatBytes(source.size_bytes)}
                  </span>
                </>
              ) : (
                <span className="text-[11.5px] text-mist-500">No source video yet</span>
              )}
              {detail.transcript ? (
                <Badge tone="neutral">
                  <FileAudio className="size-3" strokeWidth={2} />
                  {detail.transcript.word_count} words
                </Badge>
              ) : null}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-1.5">
            <button type="button" className="btn btn-sm" onClick={() => void refresh()}>
              <RefreshCw className="size-3.5" strokeWidth={2} />
              Refresh
            </button>
            {source ? (
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => setReplacingSource((value) => !value)}
              >
                Replace source
              </button>
            ) : null}
            {deleting ? (
              <span className="flex items-center gap-1.5">
                <button type="button" className="btn btn-sm border-signal-bad/40 text-signal-bad" onClick={deleteProject}>
                  Delete permanently
                </button>
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => setDeleting(false)}>
                  Cancel
                </button>
              </span>
            ) : (
              <button
                type="button"
                className="btn btn-ghost btn-sm text-mist-500"
                onClick={() => setDeleting(true)}
                disabled={Boolean(activeJob)}
                title={activeJob ? "Stop the running job first" : "Delete this project and its files"}
              >
                <Trash2 className="size-3.5" strokeWidth={2} />
                Delete
              </button>
            )}
          </div>
        </div>
      </div>

      {error ? <Alert tone="bad">{error}</Alert> : null}

      {latestJob ? (
        <JobPanel job={latestJob} onCancel={cancelJob} onRetry={retryJob} />
      ) : null}

      {!source ? (
        <SourceImporter
          projectId={projectId}
          capabilities={capabilities}
          onImported={async () => {
            await refresh();
          }}
        />
      ) : null}

      {source && replacingSource ? (
        <SourceImporter
          projectId={projectId}
          capabilities={capabilities}
          replacing
          onImported={async () => {
            setReplacingSource(false);
            await refresh();
          }}
        />
      ) : null}

      {source ? <SourceFacts detail={detail} /> : null}

      {source ? (
        <ProductionPanel
          projectId={projectId}
          pipeline={pipeline}
          exports={exports}
          candidates={detail.candidates}
          onRefresh={refresh}
        />
      ) : null}

      {source && source.has_audio && detail.candidates.length > 0 ? (
        <div ref={discoveryRef} className="space-y-2">
          <DiscoveryCounts
            state={detail.discovery}
            displayed={railCounts.displayed}
            selected={railCounts.selected}
          />
          <DiscoveryPanel
            detail={detail}
            capabilities={capabilities}
            expanded={showDiscovery}
            onToggle={() => setShowDiscovery((value) => !value)}
            onStarted={async () => {
              await refresh();
            }}
          />
        </div>
      ) : null}

      {source && !source.has_audio ? (
        <Alert tone="warn" title="This file has no audio track">
          ClipForge ranks spoken moments, so this source cannot be analysed. Import a recording with speech
          instead.
        </Alert>
      ) : null}

      {source && source.has_audio && detail.candidates.length === 0 && !analysisRunning ? (
        <AnalysisPanel
          detail={detail}
          capabilities={capabilities}
          onStarted={async () => {
            await refresh();
          }}
        />
      ) : null}

      {detail.candidates.length > 0 ? (
        <div className="grid gap-4 lg:grid-cols-[minmax(290px,360px)_1fr]">
          <div className="lg:sticky lg:top-[4.5rem] lg:self-start">
            <CandidateRail
              candidates={detail.candidates}
              selectedId={selected?.id ?? null}
              onSelect={(candidate) => setSelectedId(candidate.id)}
              onDecision={(candidate, decision) => {
                void patchCandidate(candidate.id, { decision }).catch((caught) =>
                  setError(messageFrom(caught)),
                );
              }}
              onBatchCreate={createClips}
              onPreviewSelected={previewSelected}
              onFindMore={openDiscovery}
              batchBusy={batchBusy}
              convertedIds={convertedIds}
              onCountsChange={setRailCounts}
            />
          </div>
          <div className="min-w-0 space-y-4" ref={editorRef}>
            {selected && source ? (
              <ClipEditor
                key={selected.id}
                projectId={projectId}
                candidate={selected}
                source={source}
                transcript={transcript}
                capabilities={capabilities}
                hasWaveform={hasWaveform}
                renders={detail.renders}
                onPatch={patchCandidate}
                onBoundary={reviewBoundary}
                onEditorial={reEvaluateEditorial}
                onCandidate={applyCandidate}
                onRender={renderClip}
                renderBusy={renderRunning}
              />
            ) : null}

            <section className="surface px-4 py-3">
              <button
                type="button"
                className="flex w-full items-center justify-between gap-2 text-left"
                onClick={() => setShowRerun((value) => !value)}
                aria-expanded={showRerun}
              >
                <span>
                  <span className="label block">Not finding the right moments?</span>
                  <span className="mt-0.5 block text-[12.5px] text-mist-500">
                    Run the analysis again with a different goal or length. The transcript is reused.
                  </span>
                </span>
                <ChevronDown
                  className={`size-4 shrink-0 text-mist-500 transition-transform ${showRerun ? "rotate-180" : ""}`}
                  strokeWidth={2}
                />
              </button>
            </section>

            {showRerun && !analysisRunning ? (
              <AnalysisPanel
                detail={detail}
                capabilities={capabilities}
                variant="rerun"
                onStarted={async () => {
                  setShowRerun(false);
                  await refresh();
                }}
              />
            ) : null}
          </div>
        </div>
      ) : null}

      {source && source.has_audio && detail.candidates.length === 0 && analysisRunning ? (
        <section className="surface flex items-center gap-3 px-4 py-5">
          <Wand2 className="size-4 text-ember-400" strokeWidth={2} />
          <p className="text-[13px] text-mist-300">
            Transcription and ranking are running on this machine. The first run also downloads the
            speech model, so it takes longer than later passes.
          </p>
        </section>
      ) : null}
    </div>
  );
}

function ProjectTitle({ name, onRename }: { name: string; onRename: (name: string) => Promise<void> }) {
  const [value, setValue] = useState(name);
  const [editing, setEditing] = useState(false);

  useEffect(() => setValue(name), [name]);

  return editing ? (
    <input
      autoFocus
      className="field h-9 max-w-2xl text-[22px] font-semibold"
      value={value}
      maxLength={120}
      onChange={(event) => setValue(event.target.value)}
      onBlur={() => {
        setEditing(false);
        void onRename(value);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.currentTarget.blur();
        }
        if (event.key === "Escape") {
          setValue(name);
          setEditing(false);
        }
      }}
      aria-label="Project name"
    />
  ) : (
    <button type="button" className="block max-w-2xl text-left" onClick={() => setEditing(true)}>
      <h1 className="text-[22px] font-semibold leading-tight text-mist-50 hover:text-ember-200">{name}</h1>
    </button>
  );
}

function SourceFacts({ detail }: { detail: ProjectDetail }) {
  const source = detail.source;
  if (!source) return null;
  const run = detail.analysis;
  const discovery = detail.discovery;
  return (
    <section className="surface flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 text-[11.5px] text-mist-500">
      <span className="min-w-0 max-w-[280px] truncate" title={source.original_name}>
        {source.original_name}
      </span>
      <span className="numeric">
        {source.video_codec ?? "?"} / {source.audio_codec ?? "no audio"} · {source.fps ? `${Math.round(source.fps * 100) / 100} fps` : "—"}
      </span>
      <span>
        {discovery?.counts.total_discovered ?? detail.candidates.length} discovered ·{" "}
        {discovery?.counts.accepted ?? 0} kept · {discovery?.counts.converted ?? 0} clips made
      </span>
      {run ? (
        <span className="numeric">
          last run {new Date(run.created_at).toLocaleString()} · {run.provider ?? "baseline"} · {run.status}
        </span>
      ) : null}
      {run && run.warnings.length > 0 ? (
        <span className="text-signal-warn/90">{run.warnings.join(" · ")}</span>
      ) : null}
      <span className="numeric ml-auto">0:00–{formatTimecode(source.duration_ms)}</span>
    </section>
  );
}
