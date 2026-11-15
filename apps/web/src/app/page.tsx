import Link from "next/link";
import { ArrowUpRight, Clapperboard, Film } from "lucide-react";
import type { Capabilities, Project } from "@clipforge/shared";
import { formatBytes, formatDuration } from "@clipforge/shared";
import { Badge, EmptyState, Stat } from "@/components/ui";
import { errorMessage, workerJson } from "@/lib/worker";

export const dynamic = "force-dynamic";

type ProjectsResponse = { projects: Project[] };

export default async function ProjectsPage() {
  let projects: Project[] = [];
  let capabilities: Capabilities | null = null;
  let failure: string | null = null;

  const [projectsResult, capabilitiesResult] = await Promise.allSettled([
    workerJson<ProjectsResponse>("/api/projects"),
    workerJson<Capabilities>("/api/capabilities"),
  ]);

  if (projectsResult.status === "fulfilled") {
    projects = projectsResult.value.projects;
  } else {
    failure = errorMessage(projectsResult.reason);
  }
  if (capabilitiesResult.status === "fulfilled") {
    capabilities = capabilitiesResult.value;
  }

  const totalCandidates = projects.reduce((sum, project) => sum + (project.candidate_count ?? 0), 0);
  const totalRenders = projects.reduce((sum, project) => sum + (project.render_count ?? 0), 0);

  return (
    <div className="space-y-8">
      <section className="relative overflow-hidden rounded-2xl border border-white/8 bg-ink-900/60 px-6 py-8 sm:px-9 sm:py-10">
        <div
          aria-hidden
          className="pointer-events-none absolute -right-24 -top-32 size-72 rounded-full bg-ember-500/12 blur-3xl"
        />
        <div className="relative max-w-2xl space-y-3">
          <p className="label">Local-first repurposing studio</p>
          <h1 className="text-[26px] font-semibold leading-[1.15] text-mist-50 sm:text-[32px]">
            Find the clips hiding in your long recordings.
          </h1>
          <p className="max-w-xl text-[13.5px] leading-relaxed text-mist-400">
            Import a video, let your machine transcribe it, and review ranked moments with the reasons
            behind each score. You decide what leaves the cutting room.
          </p>
          <div className="flex flex-wrap items-center gap-2 pt-2">
            <Link href="/new" className="btn btn-primary">
              <Film className="size-4" strokeWidth={2} />
              Import a video
            </Link>
            <Link href="/templates" className="btn">
              Browse templates
            </Link>
          </div>
        </div>
      </section>

      {failure ? (
        <div className="rounded-xl border border-signal-warn/30 bg-signal-warn/[0.06] px-4 py-3.5">
          <p className="text-[13px] font-semibold text-signal-warn">The local worker is not available</p>
          <p className="mt-1 text-[12.5px] leading-relaxed text-mist-400">{failure}</p>
          <pre className="mt-2.5 overflow-x-auto rounded-md bg-black/40 px-3 py-2 text-[12px] text-mist-300">
            pnpm worker:dev
          </pre>
        </div>
      ) : null}

      {capabilities ? <EngineStrip capabilities={capabilities} /> : null}

      <section className="space-y-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="space-y-1">
            <h2 className="text-[15px] font-semibold text-mist-100">Projects</h2>
            <p className="text-[12.5px] text-mist-500">
              {projects.length === 0
                ? "Nothing here yet. Your first import takes one file and a few minutes."
                : `${projects.length} project${projects.length === 1 ? "" : "s"} · ${totalCandidates} moments · ${totalRenders} exports`}
            </p>
          </div>
          {projects.length > 0 ? (
            <Link href="/new" className="btn btn-sm">
              Import another
            </Link>
          ) : null}
        </div>

        {projects.length === 0 && !failure ? (
          <EmptyState
            icon={<Clapperboard className="size-7" strokeWidth={1.5} />}
            title="No projects yet"
            description="Start with a podcast, interview, webinar or stream. Clipforge inspects the file, transcribes the speech, then proposes self-contained moments you can tighten and export."
            action={
              <Link href="/new" className="btn btn-primary btn-sm">
                Create your first project
              </Link>
            }
          />
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {projects.map((project) => (
              <ProjectCard key={project.id} project={project} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function EngineStrip({ capabilities }: { capabilities: Capabilities }) {
  const transcription = capabilities.transcription;
  const diskOk = capabilities.storage.free_bytes > capabilities.storage.min_free_bytes;
  return (
    <section className="surface grid gap-5 px-4 py-4 sm:grid-cols-2 lg:grid-cols-4">
      <Stat
        label="Transcription"
        value={transcription.available ? `Whisper ${transcription.default_model}` : "Unavailable"}
        hint={
          transcription.available
            ? `CPU · ${transcription.compute_type} · on demand`
            : "faster-whisper could not load"
        }
      />
      <Stat
        label="Speakers"
        value={capabilities.diarisation.available ? capabilities.diarisation.provider : "Not available"}
        hint={
          capabilities.diarisation.available
            ? "Speaker labels enabled"
            : "Speaker labels are planned, not shipped"
        }
      />
      <Stat
        label="Scoring"
        value="On-device signals"
        hint={capabilities.llm.enabled ? "Optional model ranking is on" : "No model service configured"}
      />
      <Stat
        label="Disk free"
        value={formatBytes(capabilities.storage.free_bytes)}
        hint={diskOk ? "Enough room for one more import" : "Below the safety floor"}
      />
    </section>
  );
}

const STATUS_TONE: Record<string, "neutral" | "good" | "warn" | "info"> = {
  draft: "neutral",
  analysing: "info",
  ready: "good",
  error: "warn",
};

function ProjectCard({ project }: { project: Project }) {
  const detail = [
    project.duration_ms ? formatDuration(project.duration_ms) : null,
    project.width && project.height ? `${project.width}×${project.height}` : null,
    project.has_audio === false ? "no audio" : null,
  ].filter(Boolean);

  return (
    <Link
      href={`/projects/${project.id}`}
      className="group surface-raised flex flex-col gap-3 p-4 transition-colors hover:border-white/15"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-[14px] font-medium text-mist-50">{project.name}</p>
          <p className="mt-0.5 truncate text-[11.5px] text-mist-600">
            {project.original_name ?? "No source imported yet"}
          </p>
        </div>
        <ArrowUpRight
          className="size-4 shrink-0 text-mist-600 transition-colors group-hover:text-ember-400"
          strokeWidth={2}
        />
      </div>

      <div className="flex items-end gap-3">
        <div className="relative h-12 w-20 shrink-0 overflow-hidden rounded-md border border-white/8 bg-ink-800">
          <div
            className="absolute inset-0 opacity-70"
            style={{
              backgroundImage:
                "linear-gradient(135deg, rgba(233,180,76,0.16), transparent 55%), repeating-linear-gradient(90deg, rgba(255,255,255,0.05) 0 1px, transparent 1px 7px)",
            }}
          />
          <span className="numeric absolute bottom-1 left-1.5 text-[10px] text-mist-400">
            {project.duration_ms ? formatDuration(project.duration_ms) : "—"}
          </span>
        </div>
        <div className="flex flex-wrap gap-1.5">
          <Badge tone={STATUS_TONE[project.status] ?? "neutral"}>{project.status}</Badge>
          <Badge>{project.candidate_count ?? 0} moments</Badge>
          <Badge>{project.render_count ?? 0} exports</Badge>
        </div>
      </div>

      <div className="mt-auto flex items-center justify-between border-t border-white/6 pt-2.5 text-[11.5px] text-mist-600">
        <span className="truncate">{detail.length ? detail.join(" · ") : "Waiting for a source"}</span>
        <span className="numeric shrink-0 pl-2">{formatRelative(project.updated_at)}</span>
      </div>
    </Link>
  );
}

function formatRelative(iso: string): string {
  const then = new Date(iso.endsWith("Z") ? iso : `${iso}Z`).getTime();
  if (Number.isNaN(then)) return "";
  const minutes = Math.round((Date.now() - then) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(then).toLocaleDateString();
}
