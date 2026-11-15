"use client";

import { useState } from "react";
import { Loader2, Sparkles, Wand2 } from "lucide-react";
import type { Capabilities, ProjectDetail } from "@clipforge/shared";
import { DURATION_BANDS, GOALS, QUALITY_PROFILES } from "@clipforge/shared";
import { apiJson, messageFrom } from "@/lib/api";
import { Alert, Badge } from "@/components/ui";
import { cn } from "@/lib/utils";

type GoalId = (typeof GOALS)[number]["id"];
type BandId = (typeof DURATION_BANDS)[number]["id"];
type QualityId = (typeof QUALITY_PROFILES)[number]["id"];

export function AnalysisPanel({
  detail,
  capabilities,
  onStarted,
  variant = "primary",
}: {
  detail: ProjectDetail;
  capabilities: Capabilities | null;
  onStarted: () => void | Promise<void>;
  variant?: "primary" | "rerun";
}) {
  const goals = detail.project.goals as { goal?: string; clip_count?: number; duration_band?: string };
  const [goal, setGoal] = useState<GoalId>((goals.goal as GoalId) ?? "best_moments");
  const [clipCount, setClipCount] = useState<number>(goals.clip_count ?? 8);
  const [band, setBand] = useState<BandId>((goals.duration_band as BandId) ?? "auto");
  const [quality, setQuality] = useState<QualityId>("balanced");
  const [vad, setVad] = useState(true);
  const [useLlm, setUseLlm] = useState(false);
  const [consent, setConsent] = useState(false);
  const [instructions, setInstructions] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const llmReady = Boolean(capabilities?.llm.configured);
  const model = capabilities?.transcription.default_model ?? "small";
  const qualityModel: Record<QualityId, string> = {
    fast: capabilities?.transcription.models.find((entry) => entry.id === "base")?.id ?? "base",
    balanced: model,
    best: "medium",
  };
  const downloadMb: Record<QualityId, number> = {
    fast: capabilities?.transcription.models.find((entry) => entry.id === "base")?.download_mb ?? 150,
    balanced: capabilities?.transcription.models.find((entry) => entry.id === model)?.download_mb ?? 480,
    best: capabilities?.transcription.models.find((entry) => entry.id === "medium")?.download_mb ?? 1500,
  };

  async function start() {
    setBusy(true);
    setError(null);
    try {
      await apiJson(`/api/projects/${detail.project.id}/analysis`, {
        method: "POST",
        body: {
          goal,
          clip_count: clipCount,
          duration_band: band,
          quality,
          vad,
          use_llm: useLlm && llmReady && consent,
          llm_consent: consent,
          instructions: instructions.trim() ? instructions.trim() : null,
        },
      });
      await onStarted();
    } catch (caught) {
      setError(messageFrom(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={cn("surface space-y-5 p-5", variant === "rerun" && "bg-transparent p-0 border-0")}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h2 className="text-[15px] font-semibold text-mist-50">
            {variant === "primary" ? "What are you looking for?" : "Run the analysis again"}
          </h2>
          <p className="max-w-xl text-[12.5px] leading-relaxed text-mist-500">
            The goal changes how each moment is weighted. Nothing here promises reach — the score is a
            ranking aid, and you still choose what to keep.
          </p>
        </div>
        <Badge tone="neutral">{detail.transcript ? "Transcript cached" : `Whisper ${model} · CPU`}</Badge>
      </div>

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {GOALS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            onClick={() => setGoal(entry.id)}
            aria-pressed={goal === entry.id}
            className={cn(
              "rounded-lg border px-3 py-2.5 text-left transition-colors",
              goal === entry.id
                ? "border-ember-500/50 bg-ember-500/[0.08]"
                : "border-white/8 bg-black/20 hover:border-white/16",
            )}
          >
            <span className="block text-[12.5px] font-medium text-mist-100">{entry.label}</span>
            <span className="mt-0.5 block text-[11.5px] leading-snug text-mist-500">{entry.description}</span>
          </button>
        ))}
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <div className="space-y-2">
          <div className="flex items-baseline justify-between">
            <label htmlFor="clip-count" className="label">
              Moments to propose
            </label>
            <span className="numeric text-[12px] text-mist-300">{clipCount}</span>
          </div>
          <input
            id="clip-count"
            type="range"
            min={3}
            max={20}
            step={1}
            value={clipCount}
            onChange={(event) => setClipCount(Number(event.target.value))}
            className="w-full accent-ember-500"
          />
          <p className="text-[11.5px] text-mist-600">
            Fewer, stronger candidates are easier to review. You can ask for more later without
            re-transcribing.
          </p>
        </div>

        <div className="space-y-2">
          <p className="label">Target length</p>
          <div className="segmented flex-wrap">
            {DURATION_BANDS.map((entry) => (
              <button
                key={entry.id}
                type="button"
                aria-pressed={band === entry.id}
                data-active={band === entry.id}
                onClick={() => setBand(entry.id)}
                title={entry.hint}
              >
                {entry.label}
              </button>
            ))}
          </div>
          <p className="text-[11.5px] text-mist-600">
            {DURATION_BANDS.find((entry) => entry.id === band)?.hint}
          </p>
        </div>
      </div>

      <div className="space-y-2">
        <p className="label">Accuracy</p>
        <div className="grid gap-2 sm:grid-cols-3">
          {QUALITY_PROFILES.map((entry) => (
            <button
              key={entry.id}
              type="button"
              onClick={() => setQuality(entry.id)}
              aria-pressed={quality === entry.id}
              className={cn(
                "rounded-lg border px-3 py-2 text-left transition-colors",
                quality === entry.id
                  ? "border-ember-500/50 bg-ember-500/[0.08]"
                  : "border-white/8 bg-black/20 hover:border-white/16",
              )}
            >
              <span className="block text-[12.5px] font-medium text-mist-100">{entry.label}</span>
              <span className="block text-[11px] text-mist-500">{entry.hint}</span>
              <span className="numeric mt-1 block text-[10.5px] text-mist-600">
                {qualityModel[entry.id]} · ≈{downloadMb[entry.id]} MB
              </span>
            </button>
          ))}
        </div>
        <p className="text-[11.5px] text-mist-600">
          Models download once into this machine&apos;s cache. The first run therefore takes longer.
        </p>
      </div>

      <div className="space-y-3 border-t border-white/6 pt-4">
        <label className="flex items-start gap-2.5 text-[12.5px] text-mist-300">
          <input
            type="checkbox"
            className="mt-0.5 size-3.5 accent-ember-500"
            checked={vad}
            onChange={(event) => setVad(event.target.checked)}
          />
          <span>
            Skip silence with voice activity detection
            <span className="mt-0.5 block text-[11.5px] text-mist-600">
              Cuts hallucinated captions in quiet sections. Recommended.
            </span>
          </span>
        </label>

        <label
          className={cn(
            "flex items-start gap-2.5 text-[12.5px]",
            llmReady ? "text-mist-300" : "text-mist-600",
          )}
        >
          <input
            type="checkbox"
            className="mt-0.5 size-3.5 accent-ember-500"
            checked={useLlm && llmReady}
            onChange={(event) => {
              setUseLlm(event.target.checked);
              if (!event.target.checked) setConsent(false);
            }}
            disabled={!llmReady}
          />
          <span>
            Re-rank the shortlist with a language model
            <span className="mt-0.5 block text-[11.5px] text-mist-600">
              {llmReady
                ? "The deterministic scorer still runs first. Only the shortlist's transcript text is sent to the configured model."
                : "No model service is configured, so this stays off. The on-device scorer is the only ranker."}
            </span>
          </span>
        </label>

        {useLlm && llmReady ? (
          <div className="space-y-2 rounded-lg border border-signal-info/25 bg-signal-info/[0.06] px-3 py-2.5">
            <p className="text-[12px] font-medium text-signal-info">Transcript text will leave this machine</p>
            <p className="text-[11.5px] leading-relaxed text-mist-400">
              Clipforge sends shortlisted transcript excerpts — not video or audio — to the model service
              you configured. Nothing is sent unless you confirm below.
            </p>
            <label className="flex items-start gap-2 text-[12px] text-mist-200">
              <input
                type="checkbox"
                className="mt-0.5 size-3.5 accent-ember-500"
                checked={consent}
                onChange={(event) => setConsent(event.target.checked)}
              />
              I understand and want model-assisted ranking for this run.
            </label>
            <input
              className="field h-8 text-[12px]"
              value={instructions}
              maxLength={600}
              onChange={(event) => setInstructions(event.target.value)}
              placeholder="Optional: anything specific to favour, e.g. 'the part about hiring'"
            />
          </div>
        ) : null}
      </div>

      {error ? <Alert tone="bad">{error}</Alert> : null}

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-white/6 pt-4">
        <p className="flex items-center gap-1.5 text-[11.5px] text-mist-600">
          <Sparkles className="size-3.5" strokeWidth={2} />
          {detail.transcript
            ? "Reuses the stored transcript — no re-transcription."
            : "Transcription runs once and is cached for every later pass."}
        </p>
        <button
          type="button"
          className="btn btn-primary"
          onClick={start}
          disabled={busy || (useLlm && llmReady && !consent)}
        >
          {busy ? <Loader2 className="size-4 animate-spin" strokeWidth={2} /> : <Wand2 className="size-4" strokeWidth={2} />}
          {variant === "primary" ? "Find moments" : "Run again"}
        </button>
      </div>
    </section>
  );
}
