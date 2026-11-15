import { notFound } from "next/navigation";
import type { Capabilities, ProjectDetail, Transcript } from "@clipforge/shared";
import { ProjectWorkspace } from "./project-workspace";
import { errorMessage, WorkerError, workerJson } from "@/lib/worker";

export const dynamic = "force-dynamic";

export default async function ProjectPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;

  let detail: ProjectDetail;
  try {
    detail = await workerJson<ProjectDetail>(`/api/projects/${id}`);
  } catch (error) {
    if (error instanceof WorkerError && error.status === 404) notFound();
    return (
      <div className="mx-auto max-w-2xl space-y-3 py-16 text-center">
        <h1 className="text-lg font-semibold text-mist-50">This project cannot be opened</h1>
        <p className="text-[13px] leading-relaxed text-mist-500">{errorMessage(error)}</p>
        <pre className="mx-auto mt-2 w-fit rounded-md bg-black/40 px-3 py-2 text-[12px] text-mist-300">
          pnpm worker:dev
        </pre>
      </div>
    );
  }

  const [capabilities, transcript] = await Promise.all([
    workerJson<Capabilities>("/api/capabilities").catch(() => null),
    detail.source
      ? workerJson<{ transcript: Transcript }>(`/api/projects/${id}/transcript`)
          .then((response) => response.transcript)
          .catch(() => null)
      : Promise.resolve(null),
  ]);

  return <ProjectWorkspace detail={detail} capabilities={capabilities} transcript={transcript} />;
}
