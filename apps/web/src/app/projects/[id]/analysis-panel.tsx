"use client";

import { useState } from "react";
import { Loader2, Sparkles, Wand2 } from "lucide-react";
import type { Capabilities, ProjectDetail } from "@clipforge/shared";
import { DURATION_PRESETS, EDITORIAL_GENRES, EDITORIAL_STRATEGIES, EDITORIAL_STYLES, GOALS, HOOK_PREFERENCES, LOOP_ENDINGS, PACING_PROFILES, PATTERN_INTERRUPTS, QUALITY_PROFILES, REACTION_SHOTS } from "@clipforge/shared";
import { apiJson, messageFrom } from "@/lib/api";
import { Alert, Badge } from "@/components/ui";
import { cn } from "@/lib/utils";

type GoalId = (typeof GOALS)[number]["id"];
type DurationPresetId = (typeof DURATION_PRESETS)[number]["id"];
type QualityId = (typeof QUALITY_PROFILES)[number]["id"];
type EditorialStyleId = (typeof EDITORIAL_STYLES)[number]["id"];
type EditorialStrategyId = (typeof EDITORIAL_STRATEGIES)[number]["id"];

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
  const goals = detail.project.goals as {
    goal?: string;
    clip_count?: number;
    duration_band?: string;
    duration_preset?: DurationPresetId;
    duration_min_ms?: number;
    duration_max_ms?: number;
    editorial_style?: EditorialStyleId;
    editorial_strategy?: EditorialStrategyId;
    genre?: string;
    pacing?: string;
    flashback?: string;
    hook_preference?: string;
    pattern_interrupts?: string;
    reaction_shots?: string;
    loop_ending?: string;
  };
  const [goal, setGoal] = useState<GoalId>((goals.goal as GoalId) ?? "best_moments");
  const [clipCount, setClipCount] = useState<number>(goals.clip_count ?? 8);
  const [durationPreset, setDurationPreset] = useState<DurationPresetId>(
    goals.duration_preset ?? "extended",
  );
  const preset = DURATION_PRESETS.find((entry) => entry.id === durationPreset) ?? DURATION_PRESETS[2];
  const [customMin, setCustomMin] = useState<number>(
    Math.round((goals.duration_min_ms ?? preset.min_ms) / 1000),
  );
  const [customMax, setCustomMax] = useState<number>(
    Math.round((goals.duration_max_ms ?? preset.max_ms) / 1000),
  );
  const [editorialStyle, setEditorialStyle] = useState<EditorialStyleId>(
    goals.editorial_style ?? "automatic",
  );
  const [editorialStrategy, setEditorialStrategy] = useState<EditorialStrategyId>(
    goals.editorial_strategy ?? "automatic",
  );
  const [genre, setGenre] = useState<string>(goals.genre ?? "auto");
  const [pacing, setPacing] = useState<string>(goals.pacing ?? "balanced");
  const [flashback, setFlashback] = useState<string>(goals.flashback ?? "automatic");
  const [hookPreference, setHookPreference] = useState<string>(goals.hook_preference ?? "auto");
  const [patternInterrupts, setPatternInterrupts] = useState<string>(
    goals.pattern_interrupts ?? "balanced",
  );
  const [reactionShots, setReactionShots] = useState<string>(goals.reaction_shots ?? "automatic");
  const [loopEnding, setLoopEnding] = useState<string>(goals.loop_ending ?? "automatic");
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
          duration_band: "auto",
          quality,
          vad,
          use_llm: useLlm && llmReady && consent,
          llm_consent: consent,
          instructions: instructions.trim() ? instructions.trim() : null,
          brief: {
            duration_preset: durationPreset,
            min_duration_ms: customMin * 1000,
            max_duration_ms: customMax * 1000,
            editorial: {
              style: editorialStyle,
              preferred_strategy: editorialStrategy,
              genre,
              flashback,
              hook_preference: hookPreference,
              pattern_interrupt_intensity: patternInterrupts,
              reaction_shots: reactionShots,
              loop_ending: loopEnding,
            },
            pacing: { profile: pacing },
          },
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
          <p className="label">Editorial target length</p>
          <div className="segmented flex-wrap">
            {DURATION_PRESETS.map((entry) => (
              <button
                key={entry.id}
                type="button"
                aria-pressed={durationPreset === entry.id}
                data-active={durationPreset === entry.id}
                onClick={() => {
                  setDurationPreset(entry.id);
                  if (entry.id !== "custom") {
                    setCustomMin(Math.round(entry.min_ms / 1000));
                    setCustomMax(Math.round(entry.max_ms / 1000));
                  }
                }}
                title={entry.hint}
              >
                {entry.label}
              </button>
            ))}
          </div>
          <p className="text-[11.5px] text-mist-600">{preset.hint}</p>
          {durationPreset === "custom" ? (
            <div className="flex items-center gap-2">
              <label className="sr-only" htmlFor="duration-min">
                Minimum seconds
              </label>
              <input
                id="duration-min"
                className="field h-8 w-20 text-[12px]"
                type="number"
                min={2}
                max={900}
                value={customMin}
                onChange={(event) => setCustomMin(Number(event.target.value))}
              />
              <span className="text-[11.5px] text-mist-500">to</span>
              <label className="sr-only" htmlFor="duration-max">
                Maximum seconds
              </label>
              <input
                id="duration-max"
                className="field h-8 w-20 text-[12px]"
                type="number"
                min={3}
                max={900}
                value={customMax}
                onChange={(event) => setCustomMax(Number(event.target.value))}
              />
              <span className="text-[11.5px] text-mist-500">seconds</span>
            </div>
          ) : null}
          <p className="text-[11.5px] text-mist-600">
            A complete moment is never padded or truncated to hit this range. No fit is reported with
            the closest coherent alternatives.
          </p>
        </div>
      </div>

      <div className="space-y-3 rounded-lg border border-white/8 bg-black/20 p-4">
        <div className="space-y-1">
          <p className="label">Editorial direction</p>
          <p className="text-[11.5px] leading-relaxed text-mist-600">
            Clipforge chooses an explainable strategy the source can support. Reordering needs review
            before export; single-camera footage is never described as multi-camera or B-roll.
          </p>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <label htmlFor="editorial-style" className="label">
              Structure
            </label>
            <select
              id="editorial-style"
              className="field h-8 text-[12px]"
              value={editorialStyle}
              onChange={(event) => setEditorialStyle(event.target.value as EditorialStyleId)}
            >
              {EDITORIAL_STYLES.map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {entry.label} — {entry.hint}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="editorial-strategy" className="label">
              Preferred strategy
            </label>
            <select
              id="editorial-strategy"
              className="field h-8 text-[12px]"
              value={editorialStrategy}
              onChange={(event) => setEditorialStrategy(event.target.value as EditorialStrategyId)}
            >
              {EDITORIAL_STRATEGIES.map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {entry.label}
                  {entry.reorders ? " (reorders; needs review)" : ""}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="editorial-genre" className="label">
              Genre preset
            </label>
            <select
              id="editorial-genre"
              className="field h-8 text-[12px]"
              value={genre}
              onChange={(event) => setGenre(event.target.value)}
            >
              <option value="auto">Automatic</option>
              {EDITORIAL_GENRES.map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {entry.label}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="editorial-pacing" className="label">
              Pacing
            </label>
            <select
              id="editorial-pacing"
              className="field h-8 text-[12px]"
              value={pacing}
              onChange={(event) => setPacing(event.target.value)}
            >
              {PACING_PROFILES.map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {entry.label} — {entry.hint}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="editorial-flashback" className="label">
              Flashbacks
            </label>
            <select
              id="editorial-flashback"
              className="field h-8 text-[12px]"
              value={flashback}
              onChange={(event) => setFlashback(event.target.value)}
            >
              <option value="automatic">Automatic</option>
              <option value="prefer">Prefer cold opens</option>
              <option value="avoid">Never reorder</option>
              <option value="manual">Only when I choose it</option>
            </select>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="editorial-hook" className="label">
              Hook preference
            </label>
            <select
              id="editorial-hook"
              className="field h-8 text-[12px]"
              value={hookPreference}
              onChange={(event) => setHookPreference(event.target.value)}
            >
              {HOOK_PREFERENCES.map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {entry.label}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="editorial-interrupts" className="label">
              Pattern interrupts
            </label>
            <select
              id="editorial-interrupts"
              className="field h-8 text-[12px]"
              value={patternInterrupts}
              onChange={(event) => setPatternInterrupts(event.target.value)}
            >
              {PATTERN_INTERRUPTS.map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {entry.label}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="editorial-reactions" className="label">
              Reaction shots
            </label>
            <select
              id="editorial-reactions"
              className="field h-8 text-[12px]"
              value={reactionShots}
              onChange={(event) => setReactionShots(event.target.value)}
            >
              {REACTION_SHOTS.map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {entry.label}
                </option>
              ))}
            </select>
            <p className="text-[10.5px] text-mist-600">
              Only applies when the source actually contains a reaction; it never invents coverage.
            </p>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="editorial-loop" className="label">
              Loop ending
            </label>
            <select
              id="editorial-loop"
              className="field h-8 text-[12px]"
              value={loopEnding}
              onChange={(event) => setLoopEnding(event.target.value)}
            >
              {LOOP_ENDINGS.map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {entry.label}
                </option>
              ))}
            </select>
            <p className="text-[10.5px] text-mist-600">
              Loop endings are reported unavailable until a repeat is edited explicitly.
            </p>
          </div>
        </div>
        <p className="text-[11.5px] text-mist-600">
          These settings shape the next candidate generation. Changing them does not re-transcribe,
          does not wipe manual trims, captions, branding or composition overrides, and does not change
          an already-rendered clip.
        </p>
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
