import Link from "next/link";
import { AlertTriangle } from "lucide-react";
import type { Capabilities } from "@clipforge/shared";
import { NewProjectWizard } from "./new-project-wizard";
import { errorMessage, workerJson } from "@/lib/worker";

export const dynamic = "force-dynamic";
export const metadata = { title: "New project" };

export default async function NewProjectPage() {
  let capabilities: Capabilities | null = null;
  let failure: string | null = null;

  try {
    capabilities = await workerJson<Capabilities>("/api/capabilities");
  } catch (error) {
    failure = errorMessage(error);
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div className="space-y-2">
        <p className="label">Step 1 of 4</p>
        <h1 className="text-[24px] font-semibold leading-tight text-mist-50">Bring in a recording</h1>
        <p className="max-w-xl text-[13.5px] leading-relaxed text-mist-400">
          Use an original file or a link you have the rights to reuse. Clipforge copies the file into its
          local workspace, verifies it is really a video, and reads its duration before anything else
          happens.
        </p>
      </div>

      {failure ? (
        <div className="rounded-xl border border-signal-warn/30 bg-signal-warn/[0.06] px-4 py-4">
          <p className="flex items-center gap-2 text-[13px] font-semibold text-signal-warn">
            <AlertTriangle className="size-4" strokeWidth={2} />
            The local worker is not running
          </p>
          <p className="mt-1.5 text-[12.5px] leading-relaxed text-mist-400">{failure}</p>
          <p className="mt-2 text-[12.5px] text-mist-400">
            Start it with <code className="rounded bg-black/40 px-1.5 py-0.5 text-mist-200">pnpm worker:dev</code>,
            then reload this page.
          </p>
          <Link href="/settings" className="btn btn-sm mt-3">
            Open diagnostics
          </Link>
        </div>
      ) : null}

      <NewProjectWizard capabilities={capabilities} />
    </div>
  );
}
