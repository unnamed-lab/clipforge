import type { Metadata } from "next";
import type { Capabilities } from "@clipforge/shared";
import { formatBytes } from "@clipforge/shared";
import { Alert, Badge, SectionHeading } from "@/components/ui";
import { errorMessage, workerJson } from "@/lib/worker";
import { RefreshDiagnostics } from "./refresh-diagnostics";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Settings" };

type Health = { status: string; version: string; uptime_seconds: number };

const ENVIRONMENT = [
  ["CLIPFORGE_WORKER_URL", "Where the web app finds the worker", "http://127.0.0.1:8788"],
  ["CLIPFORGE_WORKER_TOKEN", "Shared secret; auto-generated into data/worker-token when unset", "hidden"],
  ["CLIPFORGE_DATA_DIR", "Root for the database, media, renders and model cache", "./data"],
  ["CLIPFORGE_MAX_UPLOAD_MB", "Largest accepted source file", "2048"],
  ["CLIPFORGE_MAX_DURATION_MINUTES", "Longest accepted recording", "360"],
  ["CLIPFORGE_MIN_FREE_GB", "Imports stop below this much free disk", "2"],
  ["CLIPFORGE_WHISPER_MODEL", "Default speech model", "small"],
  ["CLIPFORGE_WHISPER_DEVICE", "cpu or cuda", "cpu"],
  ["CLIPFORGE_WHISPER_COMPUTE_TYPE", "int8 keeps CPU inference affordable", "int8"],
  ["CLIPFORGE_LLM_ENABLED", "Opt in to model-assisted rescoring", "false"],
  ["CLIPFORGE_LLM_BASE_URL", "OpenAI-compatible endpoint for rescoring", "empty"],
  ["CLIPFORGE_LLM_MODEL", "Model name sent to that endpoint", "empty"],
] as const;

export default async function SettingsPage() {
  let capabilities: Capabilities | null = null;
  let health: Health | null = null;
  let failure: string | null = null;

  const [capabilitiesResult, healthResult] = await Promise.allSettled([
    workerJson<Capabilities>("/api/capabilities"),
    workerJson<Health>("/api/health"),
  ]);

  if (capabilitiesResult.status === "fulfilled") capabilities = capabilitiesResult.value;
  else failure = errorMessage(capabilitiesResult.reason);
  if (healthResult.status === "fulfilled") health = healthResult.value;

  return (
    <div className="space-y-7">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="space-y-2">
          <p className="label">Settings</p>
          <h1 className="text-[24px] font-semibold leading-tight text-mist-50">Local engine</h1>
          <p className="max-w-2xl text-[13.5px] leading-relaxed text-mist-400">
            Everything Clipforge needs runs on this machine. This page reports what the worker can actually
            do right now — not what it is configured to attempt.
          </p>
        </div>
        <RefreshDiagnostics />
      </div>

      {failure ? (
        <Alert tone="warn" title="The worker is not responding">
          {failure}
          <pre className="mt-2 w-fit rounded bg-black/40 px-2 py-1 text-[12px] text-mist-200">pnpm worker:dev</pre>
        </Alert>
      ) : null}

      {capabilities ? (
        <>
          <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Fact
              label="Worker"
              value={health ? `v${health.version}` : "unknown"}
              detail={health ? `up ${formatUptime(health.uptime_seconds)}` : "health check failed"}
              tone={health ? "good" : "warn"}
            />
            <Fact
              label="ffmpeg"
              value={capabilities.media.ffmpeg.available ? "available" : "missing"}
              detail={shorten(capabilities.media.ffmpeg.version)}
              tone={capabilities.media.ffmpeg.available ? "good" : "bad"}
            />
            <Fact
              label="Speech model"
              value={capabilities.transcription.available ? capabilities.transcription.default_model : "unavailable"}
              detail={`${capabilities.transcription.device} · ${capabilities.transcription.compute_type}`}
              tone={capabilities.transcription.available ? "good" : "bad"}
            />
            <Fact
              label="Free disk"
              value={formatBytes(capabilities.storage.free_bytes)}
              detail={`floor ${formatBytes(capabilities.storage.min_free_bytes)}`}
              tone={
                capabilities.storage.free_bytes > capabilities.storage.min_free_bytes ? "good" : "warn"
              }
            />
          </section>

          <section className="surface space-y-4 p-5">
            <SectionHeading
              eyebrow="Media"
              title="FFmpeg toolchain"
              description="Inspection, audio extraction, waveform rendering, captions and the final encode all go through these binaries."
            />
            <dl className="grid gap-3 sm:grid-cols-2">
              <Row term="ffprobe" value={capabilities.media.ffprobe.version ?? "not found"} mono />
              <Row term="ffmpeg" value={capabilities.media.ffmpeg.version ?? "not found"} mono />
              <Row
                term="Hardware encoders"
                value={
                  capabilities.media.hwaccel.encoders.length > 0
                    ? capabilities.media.hwaccel.encoders.join(", ")
                    : "none detected — encoding uses libx264 on the CPU"
                }
              />
              <Row
                term="Transcription acceleration"
                value={
                  capabilities.media.hwaccel.acceleration_for_transcription
                    ? "GPU inference available"
                    : "CPU only — the GPU is not used for speech recognition"
                }
              />
            </dl>
          </section>

          <section className="surface space-y-4 p-5">
            <SectionHeading
              eyebrow="Speech"
              title="Transcription models"
              description="Models download once into the workspace model cache. Larger models are more accurate and considerably slower on a laptop CPU."
            />
            <div className="overflow-hidden rounded-lg border border-white/8">
              <table className="w-full text-left text-[12.5px]">
                <thead className="bg-white/[0.03] text-mist-500">
                  <tr>
                    <th className="px-3 py-2 font-medium">Model</th>
                    <th className="px-3 py-2 font-medium">Download</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {capabilities.transcription.models.map((model) => (
                    <tr key={model.id} className="border-t border-white/6">
                      <td className="px-3 py-2 text-mist-200">{model.id}</td>
                      <td className="numeric px-3 py-2 text-mist-500">≈ {model.download_mb} MB</td>
                      <td className="px-3 py-2">
                        {model.id === capabilities.transcription.default_model ? (
                          <Badge tone="ember">default</Badge>
                        ) : (
                          <span className="text-mist-600">available on request</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-[11.5px] leading-relaxed text-mist-600">
              The cache lives in <code className="text-mist-400">data/models</code>. Deleting it only costs a
              re-download; transcripts already stored are unaffected.
            </p>
          </section>

          <section className="grid gap-3 lg:grid-cols-2">
            <div className="surface space-y-3 p-5">
              <SectionHeading
                eyebrow="Speakers"
                title="Diarisation"
                description={capabilities.diarisation.detail}
              />
              <div className="flex items-center gap-2">
                <Badge tone={capabilities.diarisation.available ? "good" : "neutral"}>
                  {capabilities.diarisation.available ? "available" : "not available"}
                </Badge>
                <span className="text-[11.5px] text-mist-500">{capabilities.diarisation.provider}</span>
              </div>
              <p className="text-[11.5px] leading-relaxed text-mist-600">
                Speech is transcribed without speaker labels. Clips are never split or merged on the
                assumption that a different person is talking.
              </p>
            </div>

            <div className="surface space-y-3 p-5">
              <SectionHeading
                eyebrow="Ranking"
                title="Optional model assistance"
                description="The deterministic scorer runs on every request. A model service is only consulted if you configure one and consent per run."
              />
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={capabilities.llm.configured ? "good" : "neutral"}>
                  {capabilities.llm.configured ? "configured" : "not configured"}
                </Badge>
                <Badge tone={capabilities.llm.enabled ? "info" : "neutral"}>
                  {capabilities.llm.enabled ? "enabled" : "disabled"}
                </Badge>
                {capabilities.llm.model ? <span className="text-[11.5px] text-mist-400">{capabilities.llm.model}</span> : null}
              </div>
              <p className="text-[11.5px] leading-relaxed text-mist-600">
                Set <code className="text-mist-400">CLIPFORGE_LLM_ENABLED</code>,{" "}
                <code className="text-mist-400">CLIPFORGE_LLM_BASE_URL</code> and{" "}
                <code className="text-mist-400">CLIPFORGE_LLM_MODEL</code>, then restart the worker. When it is
                on, shortlisted transcript text leaves this machine only after you tick the consent box in the
                analysis panel.
              </p>
            </div>
          </section>

          <section className="surface space-y-4 p-5">
            <SectionHeading
              eyebrow="Storage"
              title="Limits and locations"
              description="These are the guards that stop a single import from filling the disk."
            />
            <dl className="grid gap-3 sm:grid-cols-2">
              <Row term="Data root" value={capabilities.storage.data_dir} mono />
              <Row term="Free space" value={formatBytes(capabilities.storage.free_bytes)} />
              <Row term="Minimum free space" value={formatBytes(capabilities.storage.min_free_bytes)} />
              <Row term="Maximum upload" value={formatBytes(capabilities.limits.max_upload_bytes)} />
              <Row
                term="Maximum duration"
                value={`${Math.round(capabilities.limits.max_duration_ms / 60_000)} minutes`}
              />
              <Row
                term="Platform"
                value={`${capabilities.platform.os} ${capabilities.platform.release} · ${capabilities.platform.cpu_count} cores · Python ${capabilities.platform.python}`}
              />
            </dl>
            <p className="text-[11.5px] leading-relaxed text-mist-600">
              Projects, renders and the SQLite database live under the data root. Temporary render files are
              written outside the web app&apos;s public assets and removed after the encode.
            </p>
          </section>

          <section className="surface space-y-4 p-5">
            <SectionHeading
              eyebrow="Configuration"
              title="Environment reference"
              description="Read once at worker start-up. The token value is never sent to the browser."
            />
            <div className="overflow-x-auto">
              <table className="w-full min-w-[560px] text-left text-[12px]">
                <thead className="text-mist-500">
                  <tr>
                    <th className="py-1.5 pr-4 font-medium">Variable</th>
                    <th className="py-1.5 pr-4 font-medium">Purpose</th>
                    <th className="py-1.5 font-medium">Default</th>
                  </tr>
                </thead>
                <tbody>
                  {ENVIRONMENT.map(([name, purpose, fallback]) => (
                    <tr key={name} className="border-t border-white/6 align-top">
                      <td className="py-2 pr-4 font-mono text-[11px] text-mist-300">{name}</td>
                      <td className="py-2 pr-4 text-mist-500">{purpose}</td>
                      <td className="py-2 font-mono text-[11px] text-mist-400">{fallback}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      ) : null}
    </div>
  );
}

function Fact({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail: string;
  tone: "good" | "warn" | "bad";
}) {
  return (
    <div className="surface px-4 py-3.5">
      <p className="label">{label}</p>
      <p className="mt-1 flex items-center gap-2 text-[13.5px] font-medium text-mist-100">
        <span
          className={`size-1.5 rounded-full ${
            tone === "good" ? "bg-signal-good" : tone === "warn" ? "bg-signal-warn" : "bg-signal-bad"
          }`}
        />
        {value}
      </p>
      <p className="mt-0.5 truncate text-[11px] text-mist-600" title={detail}>
        {detail}
      </p>
    </div>
  );
}

function Row({ term, value, mono = false }: { term: string; value: string; mono?: boolean }) {
  return (
    <div className="space-y-0.5">
      <dt className="text-[11px] uppercase tracking-wide text-mist-600">{term}</dt>
      <dd className={`break-words text-[12px] text-mist-300 ${mono ? "font-mono text-[11px]" : ""}`}>{value}</dd>
    </div>
  );
}

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function shorten(version: string | null): string {
  if (!version) return "not found";
  return version.length > 46 ? `${version.slice(0, 46)}…` : version;
}
