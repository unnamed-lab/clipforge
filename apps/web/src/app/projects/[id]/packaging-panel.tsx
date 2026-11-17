"use client";

import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  Copy,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Sparkles,
  Trash2,
  TrendingUp,
} from "lucide-react";
import type {
  Candidate,
  ClipMetadata,
  PackagingConfig,
  PackagingDocument,
  PackagingHashtag,
  PackagingKeyword,
  TrendRecommendation,
} from "@clipforge/shared";
import { Badge } from "@/components/ui";
import { apiJson } from "@/lib/api";
import { cn } from "@/lib/utils";

const PLATFORMS = [
  { id: "cross_platform", label: "Cross-platform" },
  { id: "tiktok", label: "TikTok" },
  { id: "youtube_shorts", label: "YouTube Shorts" },
  { id: "instagram_reels", label: "Instagram Reels" },
] as const;

type PlatformId = (typeof PLATFORMS)[number]["id"];

const STATUS_TONE = {
  strong_match: "good",
  relevant_adjacent: "info",
  broad: "warn",
  not_recommended: "bad",
} as const;

const TREND_STATE_TONE = {
  live: "good",
  cached: "info",
  stale: "warn",
  quota_exhausted: "warn",
  rate_limited: "warn",
  auth_missing: "warn",
  manual_required: "warn",
  no_results: "neutral",
  unavailable: "bad",
  disabled: "neutral",
} as const;

function emptyDocument(): PackagingDocument {
  return {
    version: 1,
    platform: "cross_platform",
    metadata: null,
    trend: null,
    settings: {},
    dismissed_trend_ids: [],
    saved_trends: [],
    manual_trends: [],
    updated_at: "",
    warnings: [],
  };
}

function documentFor(candidate: Candidate): PackagingDocument {
  return candidate.packaging ?? emptyDocument();
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function number(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function tag(value: string): string {
  const cleaned = value.replace(/[^a-zA-Z0-9]/g, "");
  return cleaned ? `#${cleaned}` : "";
}

async function copyText(value: string, label: string, onDone: (message: string) => void) {
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    onDone(`${label} copied.`);
  } catch {
    onDone("The browser blocked clipboard access. Select the text and copy it manually.");
  }
}

export function PackagingPanel({
  projectId,
  candidate,
  onCandidate,
}: {
  projectId: string;
  candidate: Candidate;
  onCandidate: (candidate: Candidate) => void;
}) {
  const initial = useMemo(() => documentFor(candidate), [candidate]);
  const [document, setDocument] = useState<PackagingDocument>(initial);
  const [draft, setDraft] = useState<ClipMetadata | null>(initial.metadata ?? null);
  const [platform, setPlatform] = useState<PlatformId>((initial.platform as PlatformId) || "cross_platform");
  const [settings, setSettings] = useState<Record<string, unknown>>(initial.settings ?? {});
  const [config, setConfig] = useState<PackagingConfig | null>(null);
  const [topic, setTopic] = useState("");
  const [subtopics, setSubtopics] = useState("");
  const [freshness, setFreshness] = useState(30);
  const [useLlm, setUseLlm] = useState(true);
  const [checkAfterGenerate, setCheckAfterGenerate] = useState(false);
  const [regenerateWithTrend, setRegenerateWithTrend] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newHashtag, setNewHashtag] = useState("");
  const [newKeyword, setNewKeyword] = useState("");
  const [manual, setManual] = useState({
    name: "",
    source_url: "",
    evidence: "",
    region: "",
    category: "",
    observed_at: "",
    direction: "unknown",
  });

  useEffect(() => {
    let alive = true;
    apiJson<PackagingConfig>(`/api/projects/${projectId}/packaging/config`)
      .then((loaded) => {
        if (!alive) return;
        setConfig(loaded);
        setSettings((current) => (Object.keys(current).length > 0 ? current : loaded.settings));
      })
      .catch(() => {
        // Configuration is optional; the panel still works with stored defaults.
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  const platformProfile = config?.platforms?.[platform] ?? {};
  const captionLimit = number(platformProfile.caption_max, 2_200);
  const captionCount = draft?.caption?.text?.length ?? 0;
  const metadata = draft;

  function flash(message: string) {
    setNotice(message);
    window.setTimeout(() => setNotice(null), 3_200);
  }

  function applyResponse(response: { candidate: Candidate; packaging: PackagingDocument }) {
    setDocument(response.packaging);
    setDraft(response.packaging.metadata ?? null);
    setPlatform((response.packaging.platform as PlatformId) || "cross_platform");
    setSettings(response.packaging.settings ?? {});
    onCandidate(response.candidate);
  }

  function updateMetadata(update: (current: ClipMetadata) => ClipMetadata) {
    setDraft((current) => (current ? update(current) : current));
  }

  function updateSetting(key: string, value: unknown) {
    setSettings((current) => ({ ...current, [key]: value }));
  }

  async function generate() {
    setBusy("generate");
    setError(null);
    try {
      const response = await apiJson<{ candidate: Candidate; packaging: PackagingDocument }>(
        `/api/projects/${projectId}/candidates/${candidate.id}/packaging`,
        {
          method: "POST",
          body: {
            platform,
            topic: topic || undefined,
            subtopics: subtopics
              .split(",")
              .map((value) => value.trim())
              .filter(Boolean),
            use_llm: useLlm,
            check_trend: checkAfterGenerate,
            settings,
          },
        },
      );
      applyResponse(response);
      flash(
        response.packaging.metadata?.provenance?.method === "llm"
          ? "Grounding-checked LLM metadata generated."
          : "Grounded metadata generated locally.",
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Metadata could not be generated.");
    } finally {
      setBusy(null);
    }
  }

  async function saveMetadata() {
    setBusy("save");
    setError(null);
    try {
      const response = await apiJson<{ candidate: Candidate; packaging: PackagingDocument }>(
        `/api/projects/${projectId}/candidates/${candidate.id}/packaging`,
        {
          method: "PATCH",
          body: { platform, metadata: draft, settings },
        },
      );
      applyResponse(response);
      flash("Packaging saved. The video was not re-rendered or re-transcribed.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Packaging could not be saved.");
    } finally {
      setBusy(null);
    }
  }

  async function saveAsDefaults() {
    setBusy("defaults");
    setError(null);
    try {
      const current = await apiJson<{ overrides?: Record<string, unknown> }>(
        `/api/projects/${projectId}/settings`,
      );
      await apiJson(`/api/projects/${projectId}/settings`, {
        method: "PATCH",
        body: { overrides: { ...(current.overrides ?? {}), packaging: settings } },
      });
      flash("Packaging defaults saved for this project. Existing metadata is unchanged.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save project defaults.");
    } finally {
      setBusy(null);
    }
  }

  async function checkTrends(force = false) {
    setBusy("trends");
    setError(null);
    try {
      const response = await apiJson<{ candidate: Candidate; packaging: PackagingDocument }>(
        `/api/projects/${projectId}/candidates/${candidate.id}/packaging/trend-check`,
        {
          method: "POST",
          body: {
            platform,
            topic: topic || undefined,
            subtopics: subtopics
              .split(",")
              .map((value) => value.trim())
              .filter(Boolean),
            freshness_days: freshness,
            region: text(settings.region, "US"),
            language: text(settings.language, "en"),
            niche: text(settings.niche),
            audience: text(settings.audience),
            force,
            settings,
          },
        },
      );
      applyResponse(response);
      flash(
        response.packaging.trend
          ? `Trend check: ${response.packaging.trend.status.replaceAll("_", " ")}.`
          : "Trend check finished.",
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Trend check failed.");
    } finally {
      setBusy(null);
    }
  }

  async function trendAction(action: "use" | "dismiss" | "restore" | "save", id: string) {
    setBusy(`${action}:${id}`);
    setError(null);
    try {
      const response = await apiJson<{ candidate: Candidate; packaging: PackagingDocument }>(
        `/api/projects/${projectId}/candidates/${candidate.id}/packaging`,
        {
          method: "PATCH",
          body: {
            trend_action: action,
            trend_id: id,
            regenerate_with_trend: action === "use" ? regenerateWithTrend : false,
          },
        },
      );
      applyResponse(response);
      flash(
        action === "use"
          ? "Trend marked as used. Only hashtags/caption may have changed."
          : action === "dismiss"
            ? "Trend dismissed."
            : action === "restore"
              ? "Trend restored."
              : "Trend saved.",
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Trend action failed.");
    } finally {
      setBusy(null);
    }
  }

  async function addManualTrend() {
    if (!manual.name.trim()) return;
    setBusy("manual");
    setError(null);
    try {
      const response = await apiJson<{ candidate: Candidate; packaging: PackagingDocument }>(
        `/api/projects/${projectId}/candidates/${candidate.id}/packaging`,
        {
          method: "PATCH",
          body: {
            trend_action: "manual",
            manual_trend: {
              ...manual,
              name: manual.name.trim(),
              source_url: manual.source_url.trim() || undefined,
              observed_at: manual.observed_at || undefined,
            },
          },
        },
      );
      applyResponse(response);
      setManual({ name: "", source_url: "", evidence: "", region: "", category: "", observed_at: "", direction: "unknown" });
      flash("Manual trend reference added with no invented metrics.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Manual trend could not be added.");
    } finally {
      setBusy(null);
    }
  }

  function addHashtag() {
    const value = tag(newHashtag);
    if (!value || !draft) return;
    if (draft.hashtags.some((item) => item.tag.toLowerCase() === value.toLowerCase())) {
      setNewHashtag("");
      return;
    }
    const entry: PackagingHashtag = {
      tag: value,
      category: "topic",
      reason: "Added manually. Review relevance before publishing.",
      provenance: "profile",
      relevance: 0.5,
    };
    updateMetadata((current) => ({ ...current, hashtags: [...current.hashtags, entry] }));
    setNewHashtag("");
  }

  function addKeyword() {
    const value = newKeyword.trim();
    if (!value || !draft) return;
    if (draft.keywords.some((item) => item.phrase.toLowerCase() === value.toLowerCase())) {
      setNewKeyword("");
      return;
    }
    const entry: PackagingKeyword = {
      phrase: value,
      reason: "Added manually. Not verified platform search data.",
      source: "ai_generated",
      verified: false,
    };
    updateMetadata((current) => ({ ...current, keywords: [...current.keywords, entry] }));
    setNewKeyword("");
  }

  const providers =
    ((config?.providers as { providers?: Array<Record<string, unknown>> } | undefined)?.providers) ?? [];
  const trend = document.trend;
  const trendState = trend?.status ?? "unavailable";
  const trendTone = TREND_STATE_TONE[trendState as keyof typeof TREND_STATE_TONE] ?? "neutral";
  const issues = metadata?.validation?.issues ?? [];
  const warnings = [
    ...(metadata?.validation?.warnings ?? []),
    ...(document.warnings ?? []),
  ].filter((value, index, list) => value && list.indexOf(value) === index);

  return (
    <div className="space-y-4">
      <section className="surface space-y-4 p-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <p className="label">AI packaging</p>
            <p className="mt-1 max-w-2xl text-[12.5px] leading-relaxed text-mist-500">
              Grounded in the final clip transcript and editorial structure. Nothing here changes the
              video. Titles, descriptions and hashtags are suggestions you can edit.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {metadata?.provenance?.method ? (
              <Badge tone={metadata.provenance.method === "llm" ? "info" : "neutral"}>
                {metadata.provenance.method === "llm" ? "LLM checked" : "Local deterministic"}
              </Badge>
            ) : null}
            {platformProfile.label ? <Badge tone="neutral">{String(platformProfile.label)}</Badge> : null}
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <label className="space-y-1.5">
            <span className="label">Platform</span>
            <select
              className="field"
              value={platform}
              onChange={(event) => setPlatform(event.target.value as PlatformId)}
            >
              {PLATFORMS.map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {entry.label}
                </option>
              ))}
            </select>
          </label>
          <label className="space-y-1.5">
            <span className="label">Topic</span>
            <input
              className="field"
              value={topic}
              placeholder={text(settings.niche, "Defaults to the profile niche")}
              onChange={(event) => setTopic(event.target.value)}
            />
          </label>
          <label className="space-y-1.5">
            <span className="label">Subtopics</span>
            <input
              className="field"
              value={subtopics}
              placeholder="comma, separated"
              onChange={(event) => setSubtopics(event.target.value)}
            />
          </label>
          <label className="space-y-1.5">
            <span className="label">Region</span>
            <input
              className="field"
              value={text(settings.region, "US")}
              onChange={(event) => updateSetting("region", event.target.value.toUpperCase())}
            />
          </label>
        </div>

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <label className="space-y-1.5">
            <span className="label">Niche</span>
            <input
              className="field"
              value={text(settings.niche)}
              placeholder="e.g. strength training"
              onChange={(event) => updateSetting("niche", event.target.value)}
            />
          </label>
          <label className="space-y-1.5">
            <span className="label">Audience</span>
            <input
              className="field"
              value={text(settings.audience)}
              placeholder="e.g. new lifters"
              onChange={(event) => updateSetting("audience", event.target.value)}
            />
          </label>
          <label className="space-y-1.5">
            <span className="label">Language</span>
            <input
              className="field"
              value={text(settings.language, "en")}
              onChange={(event) => updateSetting("language", event.target.value)}
            />
          </label>
          <label className="space-y-1.5">
            <span className="label">Hashtag style</span>
            <select
              className="field"
              value={text(settings.hashtag_style, "balanced")}
              onChange={(event) => updateSetting("hashtag_style", event.target.value)}
            >
              <option value="minimal">Minimal</option>
              <option value="balanced">Balanced</option>
              <option value="descriptive">Descriptive</option>
            </select>
          </label>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-[12px] text-mist-400">
            <input
              type="checkbox"
              checked={useLlm}
              onChange={(event) => setUseLlm(event.target.checked)}
            />
            Use the configured LLM when available
          </label>
          <label className="flex items-center gap-2 text-[12px] text-mist-400">
            <input
              type="checkbox"
              checked={checkAfterGenerate}
              onChange={(event) => setCheckAfterGenerate(event.target.checked)}
            />
            Check trends after generating
          </label>
          <span className="text-[11.5px] text-mist-600">
            Offline generation still works; no provider is required.
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn btn-primary" onClick={generate} disabled={busy !== null}>
            {busy === "generate" ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />}
            Generate packaging
          </button>
          <button type="button" className="btn" onClick={saveMetadata} disabled={busy !== null || !draft}>
            {busy === "save" ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
            Save edits
          </button>
          <button type="button" className="btn" onClick={saveAsDefaults} disabled={busy !== null}>
            {busy === "defaults" ? <Loader2 className="size-3.5 animate-spin" /> : null}
            Save as project defaults
          </button>
          {notice ? <span className="text-[11.5px] text-signal-info">{notice}</span> : null}
        </div>

        {error ? (
          <div className="flex items-start gap-2 rounded-md border border-signal-bad/25 bg-signal-bad/[0.06] px-3 py-2 text-[12px] text-signal-bad">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
            <span>{error}</span>
          </div>
        ) : null}

        {!metadata ? (
          <div className="rounded-md border border-white/8 bg-black/20 px-3 py-4 text-[12.5px] text-mist-500">
            No packaging yet. Generate metadata from this clip&apos;s transcript to get title
            alternatives, descriptions, a platform caption, hashtags and search keywords.
          </div>
        ) : (
          <div className="space-y-4">
            {issues.length > 0 ? (
              <div className="rounded-md border border-signal-bad/25 bg-signal-bad/[0.06] px-3 py-2.5">
                <p className="text-[12px] font-medium text-signal-bad">Grounding or limit issues</p>
                <ul className="mt-1 space-y-0.5 text-[12px] leading-relaxed text-mist-300">
                  {issues.map((issue) => (
                    <li key={issue}>· {issue}</li>
                  ))}
                </ul>
              </div>
            ) : null}

            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <p className="label">Title alternatives</p>
                <span className="text-[11px] text-mist-600">
                  {number(platformProfile.title_max, 100)} character platform limit
                </span>
              </div>
              <div className="space-y-1.5">
                {metadata.titles.map((entry, index) => (
                  <div key={`${entry.text}-${index}`} className="flex items-start gap-2">
                    <input
                      className="field flex-1"
                      value={entry.text}
                      onChange={(event) =>
                        updateMetadata((current) => ({
                          ...current,
                          titles: current.titles.map((item, itemIndex) =>
                            itemIndex === index ? { ...item, text: event.target.value } : item,
                          ),
                        }))
                      }
                    />
                    <button
                      type="button"
                      className={cn("btn shrink-0", index === 0 && "border-ember-500/40 text-ember-300")}
                      onClick={() =>
                        updateMetadata((current) => {
                          const titles = [...current.titles];
                          const selected = titles[index];
                          if (!selected) return current;
                          titles.splice(index, 1);
                          return { ...current, titles: [selected, ...titles] };
                        })
                      }
                    >
                      {index === 0 ? <Check className="size-3.5" /> : null}
                      {index === 0 ? "Primary" : "Use"}
                    </button>
                    <button
                      type="button"
                      className="btn shrink-0"
                      onClick={() => copyText(entry.text, "Title", flash)}
                      aria-label="Copy title"
                    >
                      <Copy className="size-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            </div>

            <div className="grid gap-3 lg:grid-cols-2">
              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <span className="label">Short description</span>
                  <button
                    type="button"
                    className="btn size-7 p-0"
                    onClick={() => copyText(metadata.descriptions.short, "Short description", flash)}
                    aria-label="Copy short description"
                  >
                    <Copy className="size-3.5" />
                  </button>
                </div>
                <textarea
                  className="field min-h-20 resize-y"
                  value={metadata.descriptions.short}
                  onChange={(event) =>
                    updateMetadata((current) => ({
                      ...current,
                      descriptions: { ...current.descriptions, short: event.target.value },
                    }))
                  }
                />
              </div>
              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <span className="label">Long description</span>
                  <button
                    type="button"
                    className="btn size-7 p-0"
                    onClick={() => copyText(metadata.descriptions.long, "Long description", flash)}
                    aria-label="Copy long description"
                  >
                    <Copy className="size-3.5" />
                  </button>
                </div>
                <textarea
                  className="field min-h-20 resize-y"
                  value={metadata.descriptions.long}
                  onChange={(event) =>
                    updateMetadata((current) => ({
                      ...current,
                      descriptions: { ...current.descriptions, long: event.target.value },
                    }))
                  }
                />
              </div>
            </div>

            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <span className="label">Platform caption</span>
                <span
                  className={cn(
                    "numeric text-[11px]",
                    captionCount > captionLimit ? "text-signal-bad" : "text-mist-600",
                  )}
                >
                  {captionCount} / {captionLimit}
                </span>
              </div>
              <textarea
                className="field min-h-24 resize-y"
                value={metadata.caption.text}
                onChange={(event) =>
                  updateMetadata((current) => ({
                    ...current,
                    caption: {
                      ...current.caption,
                      text: event.target.value,
                      char_count: event.target.value.length,
                      within_limit: event.target.value.length <= captionLimit,
                    },
                  }))
                }
              />
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  className="btn"
                  onClick={() => copyText(metadata.caption.text, "Caption", flash)}
                >
                  <Copy className="size-3.5" /> Copy caption
                </button>
                <span className="text-[11px] text-mist-600">
                  Hashtags and keywords are suggestions, not popularity evidence.
                </span>
              </div>
            </div>

            <div className="grid gap-3 lg:grid-cols-2">
              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <p className="label">Hashtags</p>
                  <button
                    type="button"
                    className="btn size-7 p-0"
                    onClick={() =>
                      copyText(
                        metadata.hashtags.map((item) => item.tag).join(" "),
                        "Hashtags",
                        flash,
                      )
                    }
                    aria-label="Copy all hashtags"
                  >
                    <Copy className="size-3.5" />
                  </button>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {metadata.hashtags.map((entry, index) => (
                    <span
                      key={`${entry.tag}-${index}`}
                      className="inline-flex items-center gap-1 rounded-full border border-white/10 bg-white/[0.04] px-2 py-1 text-[11.5px] text-mist-200"
                      title={`${entry.category}: ${entry.reason}`}
                    >
                      {entry.tag}
                      <span className="text-[9.5px] uppercase tracking-wide text-mist-600">{entry.category}</span>
                      <button
                        type="button"
                        aria-label={`Remove ${entry.tag}`}
                        onClick={() =>
                          updateMetadata((current) => ({
                            ...current,
                            hashtags: current.hashtags.filter((_, itemIndex) => itemIndex !== index),
                          }))
                        }
                      >
                        <Trash2 className="size-3 text-mist-600" />
                      </button>
                    </span>
                  ))}
                </div>
                <div className="flex items-center gap-1.5">
                  <input
                    className="field"
                    value={newHashtag}
                    placeholder="#addhashtag"
                    onChange={(event) => setNewHashtag(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        addHashtag();
                      }
                    }}
                  />
                  <button type="button" className="btn" onClick={addHashtag}>
                    <Plus className="size-3.5" />
                  </button>
                </div>
              </div>

              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <p className="label">Search keywords</p>
                  <button
                    type="button"
                    className="btn size-7 p-0"
                    onClick={() =>
                      copyText(
                        metadata.keywords.map((item) => item.phrase).join(", "),
                        "Search keywords",
                        flash,
                      )
                    }
                    aria-label="Copy all search keywords"
                  >
                    <Copy className="size-3.5" />
                  </button>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {metadata.keywords.map((entry, index) => (
                    <span
                      key={`${entry.phrase}-${index}`}
                      className="inline-flex items-center gap-1 rounded-full border border-white/10 bg-white/[0.04] px-2 py-1 text-[11.5px] text-mist-200"
                      title={entry.reason}
                    >
                      {entry.phrase}
                      {!entry.verified ? (
                        <span className="text-[9.5px] uppercase tracking-wide text-mist-600">AI</span>
                      ) : null}
                      <button
                        type="button"
                        aria-label={`Remove ${entry.phrase}`}
                        onClick={() =>
                          updateMetadata((current) => ({
                            ...current,
                            keywords: current.keywords.filter((_, itemIndex) => itemIndex !== index),
                          }))
                        }
                      >
                        <Trash2 className="size-3 text-mist-600" />
                      </button>
                    </span>
                  ))}
                </div>
                <div className="flex items-center gap-1.5">
                  <input
                    className="field"
                    value={newKeyword}
                    placeholder="natural language search phrase"
                    onChange={(event) => setNewKeyword(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        addKeyword();
                      }
                    }}
                  />
                  <button type="button" className="btn" onClick={addKeyword}>
                    <Plus className="size-3.5" />
                  </button>
                </div>
              </div>
            </div>

            <div className="grid gap-3 lg:grid-cols-3">
              <label className="space-y-1.5">
                <span className="label">CTA</span>
                <input
                  className="field"
                  value={metadata.cta ?? ""}
                  onChange={(event) =>
                    updateMetadata((current) => ({ ...current, cta: event.target.value || null }))
                  }
                />
              </label>
              <label className="space-y-1.5">
                <span className="label">Thumbnail text</span>
                <input
                  className="field"
                  value={metadata.thumbnail_text?.text ?? ""}
                  onChange={(event) =>
                    updateMetadata((current) => ({
                      ...current,
                      thumbnail_text: {
                        text: event.target.value,
                        limit: current.thumbnail_text?.limit ?? number(platformProfile.thumbnail_text_max, 40),
                        within_limit:
                          event.target.value.length <=
                          (current.thumbnail_text?.limit ?? number(platformProfile.thumbnail_text_max, 40)),
                        reason: current.thumbnail_text?.reason ?? "Edited manually.",
                      },
                    }))
                  }
                />
              </label>
              <div className="space-y-1.5">
                <span className="label">Source &amp; method</span>
                <p className="text-[11.5px] leading-relaxed text-mist-500">
                  {metadata.provenance.method} · {metadata.provenance.provider ?? "local"} ·{" "}
                  {metadata.provenance.timeline_applied ? "editorial timeline" : "approved clip window"}
                </p>
                <p className="text-[11px] text-mist-600">
                  {metadata.provenance.generated_at
                    ? new Date(metadata.provenance.generated_at).toLocaleString()
                    : "Not recorded"}
                </p>
              </div>
            </div>

            {warnings.length > 0 ? (
              <div className="rounded-md border border-signal-warn/20 bg-signal-warn/[0.04] px-3 py-2.5">
                <p className="text-[12px] font-medium text-signal-warn">Warnings</p>
                <ul className="mt-1 space-y-0.5 text-[11.5px] leading-relaxed text-mist-400">
                  {warnings.map((warning) => (
                    <li key={warning}>· {warning}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        )}
      </section>

      <section className="surface space-y-4 p-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <p className="label">Trend check</p>
            <p className="mt-1 max-w-2xl text-[12.5px] leading-relaxed text-mist-500">
              Optional, provider-aware research. Missing credentials, quotas and approvals are
              reported honestly; no search volume or growth number is invented.
            </p>
          </div>
          <Badge tone={trendTone}>{trendState.replaceAll("_", " ")}</Badge>
        </div>

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <label className="space-y-1.5">
            <span className="label">Freshness window</span>
            <select
              className="field"
              value={freshness}
              onChange={(event) => setFreshness(Number(event.target.value))}
            >
              <option value={7}>Last 7 days</option>
              <option value={30}>Last 30 days</option>
              <option value={90}>Last 90 days</option>
              <option value={180}>Last 180 days</option>
            </select>
          </label>
          <label className="space-y-1.5">
            <span className="label">Region</span>
            <input
              className="field"
              value={text(settings.region, "US")}
              onChange={(event) => updateSetting("region", event.target.value.toUpperCase())}
            />
          </label>
          <label className="space-y-1.5">
            <span className="label">Niche</span>
            <input
              className="field"
              value={text(settings.niche)}
              onChange={(event) => updateSetting("niche", event.target.value)}
            />
          </label>
          <label className="space-y-1.5">
            <span className="label">Audience</span>
            <input
              className="field"
              value={text(settings.audience)}
              onChange={(event) => updateSetting("audience", event.target.value)}
            />
          </label>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn btn-primary" onClick={() => checkTrends(false)} disabled={busy !== null}>
            {busy === "trends" ? <Loader2 className="size-3.5 animate-spin" /> : <TrendingUp className="size-3.5" />}
            Check trends
          </button>
          <button type="button" className="btn" onClick={() => checkTrends(true)} disabled={busy !== null}>
            <RefreshCw className="size-3.5" /> Refresh
          </button>
          <label className="flex items-center gap-2 text-[12px] text-mist-400">
            <input
              type="checkbox"
              checked={regenerateWithTrend}
              onChange={(event) => setRegenerateWithTrend(event.target.checked)}
            />
            When using a trend, add only its hashtag suggestion
          </label>
        </div>

        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          {providers.map((provider) => (
            <div key={String(provider.provider)} className="rounded-md border border-white/8 bg-black/20 px-2.5 py-2">
              <p className="text-[12px] text-mist-200">{String(provider.label ?? provider.provider)}</p>
              <p className="mt-0.5 text-[11px] leading-relaxed text-mist-600">{String(provider.detail ?? "")}</p>
            </div>
          ))}
        </div>

        {trend ? (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-[11.5px] text-mist-500">
              <span>Provider: {trend.provider}</span>
              <span>·</span>
              <span>{trend.cached ? "Cached" : "Live call"}</span>
              <span>·</span>
              <span>{trend.stale ? "Stale fallback" : "Not stale"}</span>
              <span>·</span>
              <span>{trend.checked_at ? new Date(trend.checked_at).toLocaleString() : "No timestamp"}</span>
            </div>

            {trend.error_message ? (
              <div className="rounded-md border border-signal-warn/25 bg-signal-warn/[0.05] px-3 py-2 text-[12px] text-signal-warn">
                {trend.error_message}
              </div>
            ) : null}

            {trend.results.length === 0 ? (
              <div className="rounded-md border border-white/8 bg-black/20 px-3 py-3 text-[12.5px] text-mist-500">
                No provider-verified trend recommendations for this query. Add a manual reference
                below or keep the factual packaging you already have.
              </div>
            ) : (
              <div className="space-y-2">
                {trend.results.map((entry) => (
                  <TrendCard
                    key={entry.id}
                    trend={entry}
                    busy={busy}
                    onUse={() => trendAction("use", entry.id)}
                    onDismiss={() => trendAction("dismiss", entry.id)}
                    onSave={() => trendAction("save", entry.id)}
                  />
                ))}
              </div>
            )}

            {trend.notes.length > 0 || trend.limitations.length > 0 ? (
              <details className="rounded-md border border-white/8 bg-black/20 px-3 py-2">
                <summary className="cursor-pointer text-[12px] text-mist-300">
                  Provider notes and limitations
                </summary>
                <ul className="mt-1.5 space-y-0.5 text-[11.5px] leading-relaxed text-mist-500">
                  {[...trend.notes, ...trend.limitations].map((note) => (
                    <li key={note}>· {note}</li>
                  ))}
                </ul>
              </details>
            ) : null}

            {document.dismissed_trend_ids.length > 0 ? (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[11px] text-mist-600">Dismissed:</span>
                {document.dismissed_trend_ids.map((id) => (
                  <button
                    key={id}
                    type="button"
                    className="text-[11px] text-mist-500 underline decoration-white/20"
                    onClick={() => trendAction("restore", id)}
                  >
                    Restore {id.slice(0, 16)}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        ) : (
          <div className="rounded-md border border-white/8 bg-black/20 px-3 py-3 text-[12.5px] text-mist-500">
            Run a trend check to see provider-verified recommendations. Trend checking is optional
            and never changes the video.
          </div>
        )}

        {document.saved_trends.length > 0 ? (
          <div className="space-y-2">
            <p className="label">Saved trends</p>
            {document.saved_trends.map((entry) => (
              <TrendCard
                key={`saved-${entry.id}`}
                trend={entry}
                busy={busy}
                saved
                onUse={() => trendAction("use", entry.id)}
                onDismiss={() => trendAction("dismiss", entry.id)}
                onSave={() => trendAction("save", entry.id)}
              />
            ))}
          </div>
        ) : null}

        <details className="rounded-md border border-white/8 bg-black/20 px-3 py-2">
          <summary className="cursor-pointer text-[12px] text-mist-300">Add a manual trend reference</summary>
          <p className="mt-1 text-[11.5px] leading-relaxed text-mist-600">
            Use this for a title, hashtag or topic you found yourself. No volume, ranking or growth is
            inferred from it.
          </p>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            <input
              className="field"
              placeholder="Trend or topic name"
              value={manual.name}
              onChange={(event) => setManual((current) => ({ ...current, name: event.target.value }))}
            />
            <input
              className="field"
              placeholder="Source URL (optional)"
              value={manual.source_url}
              onChange={(event) => setManual((current) => ({ ...current, source_url: event.target.value }))}
            />
            <input
              className="field"
              placeholder="Category (optional)"
              value={manual.category}
              onChange={(event) => setManual((current) => ({ ...current, category: event.target.value }))}
            />
            <input
              className="field"
              placeholder="Region (optional)"
              value={manual.region}
              onChange={(event) => setManual((current) => ({ ...current, region: event.target.value }))}
            />
            <input
              className="field"
              placeholder="Observed at (ISO date, optional)"
              value={manual.observed_at}
              onChange={(event) => setManual((current) => ({ ...current, observed_at: event.target.value }))}
            />
            <select
              className="field"
              value={manual.direction}
              onChange={(event) => setManual((current) => ({ ...current, direction: event.target.value }))}
            >
              <option value="unknown">Direction unknown</option>
              <option value="rising">Rising</option>
              <option value="flat">Flat</option>
              <option value="falling">Falling</option>
            </select>
            <textarea
              className="field min-h-20 resize-y sm:col-span-2"
              placeholder="What you observed, and where"
              value={manual.evidence}
              onChange={(event) => setManual((current) => ({ ...current, evidence: event.target.value }))}
            />
          </div>
          <button type="button" className="btn mt-2" onClick={addManualTrend} disabled={busy !== null || !manual.name.trim()}>
            {busy === "manual" ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />}
            Add reference
          </button>
        </details>
      </section>
    </div>
  );
}

function TrendCard({
  trend,
  busy,
  saved = false,
  onUse,
  onDismiss,
  onSave,
}: {
  trend: TrendRecommendation;
  busy: string | null;
  saved?: boolean;
  onUse: () => void;
  onDismiss: () => void;
  onSave: () => void;
}) {
  const tone = STATUS_TONE[trend.status] ?? "neutral";
  const score = Math.round(trend.scores.weighted * 100);
  return (
    <div className="rounded-md border border-white/8 bg-black/20 px-3 py-2.5">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-[12.5px] font-medium text-mist-100">{trend.name}</p>
            <Badge tone={tone}>{trend.status.replaceAll("_", " ")}</Badge>
            {trend.manual ? <Badge tone="neutral">Manual</Badge> : null}
            {trend.used ? <Badge tone="good">Used</Badge> : null}
            {saved ? <Badge tone="info">Saved</Badge> : null}
          </div>
          <p className="mt-1 text-[11.5px] leading-relaxed text-mist-400">{trend.status_reason}</p>
          <p className="mt-0.5 text-[11.5px] leading-relaxed text-mist-500">{trend.adaptation}</p>
        </div>
        <span className="numeric text-[11px] text-mist-500">fit {score}%</span>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-mist-600">
        <span>{trend.provider}</span>
        <span>{trend.platform}</span>
        <span>{trend.freshness}</span>
        <span>{trend.direction}</span>
        {typeof trend.volume === "number" ? (
          <span>provider views: {trend.volume.toLocaleString()}</span>
        ) : (
          <span>no provider volume</span>
        )}
        {typeof trend.ranking === "number" ? <span>rank {trend.ranking}</span> : null}
        {typeof trend.growth === "number" ? <span>growth {trend.growth}%</span> : <span>no growth metric</span>}
        {trend.source_url ? (
          <a
            className="underline decoration-white/20 hover:text-mist-300"
            href={trend.source_url}
            target="_blank"
            rel="noreferrer"
          >
            source
          </a>
        ) : null}
      </div>

      {trend.limitations.length > 0 ? (
        <p className="mt-1.5 text-[11px] leading-relaxed text-mist-600">
          {trend.limitations.join(" ")}
        </p>
      ) : null}

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <button type="button" className="btn" onClick={onUse} disabled={busy !== null || trend.used}>
          Use trend
        </button>
        <button type="button" className="btn" onClick={onSave} disabled={busy !== null}>
          Save
        </button>
        <button type="button" className="btn" onClick={onDismiss} disabled={busy !== null}>
          Dismiss
        </button>
      </div>
    </div>
  );
}
