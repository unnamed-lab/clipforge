"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, Download, Film, Loader2, Sparkles, Type } from "lucide-react";
import type {
  Capabilities,
  EnhancementProfile,
  EnhancementSettings,
  Render,
  Source,
} from "@clipforge/shared";
import {
  ASPECTS,
  CAPTION_ALIGNMENTS,
  CAPTION_FONTS,
  CAPTION_POSITIONS,
  CAPTION_PRESETS,
  COMPOSITION_MODES,
  DEFAULT_CAPTION_ALIGNMENT,
  DEFAULT_CAPTION_MARGIN_V,
  DEFAULT_CAPTION_POSITION,
  DEFAULT_CAPTION_POSITION_PCT,
  DEFAULT_CAPTION_PRESET,
  DEFAULT_FACE_CAM_POSITION,
  FACE_CAM_POSITIONS,
  GAMING_PRESETS,
  formatBytes,
  formatDuration,
  formatTimecode,
} from "@clipforge/shared";
import { Alert, Badge, EmptyState } from "@/components/ui";
import { apiJson } from "@/lib/api";
import {
  QUALITY_PROFILE_FALLBACK,
  TARGET_OPTIONS,
  estimatedEnhancementSeconds,
  formatSeconds,
  resolveTarget,
  type TargetMode,
} from "@/lib/quality";
import { cn } from "@/lib/utils";

type AspectId = (typeof ASPECTS)[number]["id"];
type CaptionPresetId = (typeof CAPTION_PRESETS)[number]["id"];
type CaptionPositionId = (typeof CAPTION_POSITIONS)[number]["id"];
type CaptionAlignmentId = (typeof CAPTION_ALIGNMENTS)[number]["id"];
type CompositionModeId = (typeof COMPOSITION_MODES)[number]["id"];
type FaceCamPositionId = (typeof FACE_CAM_POSITIONS)[number]["id"];
type CompressionPresetId = "smallest" | "balanced" | "higher" | "custom";

const COMPRESSION_FALLBACK: Record<
  CompressionPresetId,
  { label: string; note: string }
> = {
  smallest: { label: "Smallest file", note: "CRF 28, up to 720p, 96 kbps audio." },
  balanced: { label: "Balanced", note: "CRF 23, up to 1080p, 128 kbps audio." },
  higher: { label: "Higher quality", note: "CRF 18, full resolution, 192 kbps audio." },
  custom: { label: "Custom", note: "Set the encoder values yourself." },
};

const COMPOSITION_IDS = new Set<string>(COMPOSITION_MODES.map((mode) => mode.id));
const FACE_CAM_POSITION_IDS = new Set<string>(FACE_CAM_POSITIONS.map((entry) => entry.id));

type FaceCamRect = { x_pct: number; y_pct: number; width_pct: number; height_pct: number };
type FaceCamOverrideDraft = {
  start_ms: number;
  end_ms: number;
  rect: FaceCamRect;
  mode: "lock" | "track";
  reason: string;
};

type DetectedFacecam = {
  rect: FaceCamRect;
  state: string | null;
  confidence: number | null;
  start_ms: number;
  end_ms: number;
};

type CompositionPreviewResponse = {
  image: string;
  width: number;
  height: number;
  fingerprint: string;
  composition: string;
  face_cam_position: string;
  stack_split_pct: number;
  segment: { start_ms: number; end_ms: number; duration_ms: number };
  decision: {
    mode: string;
    reason: string;
    caption_band: { top_pct: number; bottom_pct: number } | null;
    warnings: string[];
  } | null;
  facecam: {
    detected: boolean;
    rect: Record<string, number> | null;
    state?: string | null;
    confidence?: number | null;
    reason?: string | null;
    review_required?: boolean;
    detector?: string | null;
    detector_available?: boolean | null;
    analysis_height?: number | null;
    ranges?: Array<{
      start_ms: number;
      end_ms: number;
      state: string;
      rect: Record<string, number> | null;
      face_rect?: Record<string, number> | null;
      confidence: number;
      reason: string;
    }>;
    observations?: Array<{
      time_ms: number;
      state: string;
      rect: Record<string, number> | null;
      face_rect?: Record<string, number> | null;
      confidence: number;
      reason: string;
    }>;
    warnings?: string[];
  };
  gameplay_rect: Record<string, number> | null;
  facecam_panel_rect: Record<string, number> | null;
  fallback: boolean;
  warnings: string[];
};

function compressionBodyFor(
  preset: CompressionPresetId,
  custom: { crf: number; audio: number; width: number; fps: number | null },
): Record<string, unknown> {
  if (preset !== "custom") return { preset };
  return {
    preset: "custom",
    crf: custom.crf,
    audio_bitrate_kbps: custom.audio,
    resolution_cap: custom.width,
    max_fps: custom.fps,
  };
}

function normalizeFaceCamOverrides(value: unknown): FaceCamOverrideDraft[] {
  if (!Array.isArray(value)) return [];
  const result: FaceCamOverrideDraft[] = [];
  for (const entry of value) {
    if (!entry || typeof entry !== "object") continue;
    const raw = entry as Record<string, unknown>;
    const rect = raw.rect;
    if (!rect || typeof rect !== "object") continue;
    const box = rect as Record<string, unknown>;
    const start = Number(raw.start_ms);
    const end = Number(raw.end_ms);
    const x = Number(box.x_pct);
    const y = Number(box.y_pct);
    const width = Number(box.width_pct);
    const height = Number(box.height_pct);
    if (![start, end, x, y, width, height].every(Number.isFinite)) continue;
    if (end <= start || width <= 0 || height <= 0) continue;
    result.push({
      start_ms: Math.max(0, Math.round(start)),
      end_ms: Math.max(1, Math.round(end)),
      rect: {
        x_pct: Math.min(1, Math.max(0, x)),
        y_pct: Math.min(1, Math.max(0, y)),
        width_pct: Math.min(1, Math.max(0.01, width)),
        height_pct: Math.min(1, Math.max(0.01, height)),
      },
      mode: raw.mode === "track" ? "track" : "lock",
      reason: typeof raw.reason === "string" ? raw.reason.slice(0, 200) : "Manual face-cam region",
    });
  }
  return result;
}

type QualityPreview = {
  original: string;
  enhanced: string;
  width: number;
  height: number;
  timestamp_ms: number;
  source: { width: number; height: number };
  target: [number, number];
  intermediate: [number, number] | null;
  profile: EnhancementProfile;
  applied_model: string | null;
  engine: string;
  device: string | null;
  available: boolean;
  estimated_frame_seconds: number | null;
  warnings: string[];
  notes: string[];
};

export function RenderPanel({
  projectId,
  candidateId,
  source,
  capabilities,
  durationMs,
  renders,
  busy,
  onRender,
  disabled,
  disabledReason,
}: {
  projectId: string;
  candidateId: string;
  source: Source;
  capabilities: Capabilities | null;
  durationMs: number;
  renders: Render[];
  busy: boolean;
  onRender: (body: Record<string, unknown>) => Promise<void>;
  disabled: boolean;
  disabledReason: string;
}) {
  const [aspect, setAspect] = useState<AspectId>("9:16");
  const [targetMode, setTargetMode] = useState<TargetMode>("1080");
  const [targetWidth, setTargetWidth] = useState(source.width || 1080);
  const [targetHeight, setTargetHeight] = useState(source.height || 1920);
  const [exportFps, setExportFps] = useState<number>(source.fps ? Math.round(source.fps) : 30);
  const [qualityProfile, setQualityProfile] = useState<EnhancementProfile>("standard");
  const [enhancementModel, setEnhancementModel] = useState<EnhancementSettings["model_id"]>("auto");
  const [denoise, setDenoise] = useState(0);
  const [restoration, setRestoration] = useState(1);
  const [allowFallback, setAllowFallback] = useState(true);
  const [temporalStabilize, setTemporalStabilize] = useState(false);
  const [qualityPreview, setQualityPreview] = useState<QualityPreview | null>(null);
  const [qualityPreviewBusy, setQualityPreviewBusy] = useState(false);
  const [qualityPreviewError, setQualityPreviewError] = useState<string | null>(null);
  const [comparisonPct, setComparisonPct] = useState(50);
  const [modelInstallJob, setModelInstallJob] = useState<string | null>(null);
  const [installedNow, setInstalledNow] = useState<string[]>([]);
  const [installMessage, setInstallMessage] = useState<string | null>(null);
  const [installError, setInstallError] = useState<string | null>(null);
  const [composition, setComposition] = useState<CompositionModeId>("auto");
  const [faceCamPosition, setFaceCamPosition] = useState<FaceCamPositionId>(DEFAULT_FACE_CAM_POSITION);
  const [stackSplit, setStackSplit] = useState(60);
  const [adaptiveStack, setAdaptiveStack] = useState(false);
  const [faceCamOverrides, setFaceCamOverrides] = useState<FaceCamOverrideDraft[]>([]);
  const [detectedFacecam, setDetectedFacecam] = useState<DetectedFacecam | null>(null);
  const handleDetectedFacecam = useCallback((value: DetectedFacecam | null) => {
    setDetectedFacecam(value);
  }, []);
  const [cropX, setCropX] = useState(50);
  const [cropY, setCropY] = useState(50);
  const [captionPreset, setCaptionPreset] = useState<CaptionPresetId>(DEFAULT_CAPTION_PRESET);
  const [captionFont, setCaptionFont] = useState<string>("Montserrat");
  const [captionPosition, setCaptionPosition] = useState<CaptionPositionId>(DEFAULT_CAPTION_POSITION);
  const [captionPositionPct, setCaptionPositionPct] = useState(DEFAULT_CAPTION_POSITION_PCT);
  const [captionAlignment, setCaptionAlignment] = useState<CaptionAlignmentId>(DEFAULT_CAPTION_ALIGNMENT);
  const [captionMarginV, setCaptionMarginV] = useState(DEFAULT_CAPTION_MARGIN_V);
  const [captionSaveState, setCaptionSaveState] = useState<"loading" | "saving" | "saved" | "error">("loading");
  const [captions, setCaptions] = useState(true);
  const [normalize, setNormalize] = useState(true);
  const [showPreview, setShowPreview] = useState(false);
  const [compression, setCompression] = useState<CompressionPresetId>("balanced");
  const [customCrf, setCustomCrf] = useState(23);
  const [customAudio, setCustomAudio] = useState(128);
  const [customWidth, setCustomWidth] = useState(1080);
  const [customFps, setCustomFps] = useState<number | null>(30);
  const [estimate, setEstimate] = useState<{
    estimated_bytes: number;
    width: number;
    height: number;
    fps: number;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const hydratedRef = useRef(false);
  const savedSnapshotRef = useRef("");

  const compressionBody = useMemo(
    () => compressionBodyFor(compression, { crf: customCrf, audio: customAudio, width: customWidth, fps: customFps }),
    [compression, customCrf, customAudio, customWidth, customFps],
  );

  const targetInfo = useMemo(
    () =>
      resolveTarget({
        mode: targetMode,
        aspect,
        classResolution: 1080,
        source,
        customWidth: targetWidth,
        customHeight: targetHeight,
      }),
    [targetMode, aspect, source, targetWidth, targetHeight],
  );

  const resolution = targetInfo.resolution;

  const enhancement = useMemo<EnhancementSettings>(
    () => ({
      profile: qualityProfile,
      model_id: enhancementModel,
      denoise,
      restoration,
      allow_fallback: allowFallback,
      temporal_stabilize: temporalStabilize,
      compare_first: qualityProfile === "ai_plus",
    }),
    [qualityProfile, enhancementModel, denoise, restoration, allowFallback, temporalStabilize],
  );

  const profileMeta =
    capabilities?.enhancement?.profiles.find((entry) => entry.id === qualityProfile) ??
    {
      ...QUALITY_PROFILE_FALLBACK[qualityProfile],
      id: qualityProfile,
      warnings: [],
      uses_ai:
        qualityProfile === "ai_fast" || qualityProfile === "ai" || qualityProfile === "ai_plus",
      default_denoise: QUALITY_PROFILE_FALLBACK[qualityProfile].denoise,
      default_restoration: QUALITY_PROFILE_FALLBACK[qualityProfile].restoration,
    };
  const enhancementModels = capabilities?.enhancement?.models ?? [];
  const neededScale = Math.max(
    targetInfo.width / Math.max(1, source.width || targetInfo.width),
    targetInfo.height / Math.max(1, source.height || targetInfo.height),
  );
  const autoModelId: EnhancementSettings["model_id"] =
    qualityProfile === "ai_fast"
      ? "espcn-x2"
      : (qualityProfile === "ai_plus" && neededScale > 1.35) || neededScale > 2.25
        ? "espcn-x4"
        : "espcn-x2";
  const estimateModelId = enhancementModel !== "auto" ? enhancementModel : autoModelId;
  const estimateModel = enhancementModels.find((model) => model.id === estimateModelId);
  const selectedModel = enhancementModels.find((model) => model.id === enhancementModel);
  const modelInstalled =
    enhancementModel === "auto" ||
    Boolean(selectedModel?.installed) ||
    installedNow.includes(enhancementModel);
  const aiRuntimeReady =
    enhancementModel === "realesrgan-x2"
      ? Boolean(capabilities?.enhancement?.devices.onnxruntime)
      : Boolean(capabilities?.enhancement?.devices.opencv_dnn_superres);
  const requiresComparison = qualityProfile === "ai_plus" && qualityPreview === null;
  const enhancementEstimate = estimatedEnhancementSeconds({
    profile: qualityProfile,
    benchmarkSecondsPerMegapixel: estimateModel?.benchmark?.seconds_per_input_megapixel,
    sourceWidth: source.width || targetInfo.width,
    sourceHeight: source.height || targetInfo.height,
    durationMs,
    fps: exportFps,
    maxFps: customFps,
  });

  const settingsSnapshot = useMemo(
    () =>
      JSON.stringify({
        preset: captionPreset,
        position: captionPosition,
        position_pct: captionPositionPct,
        alignment: captionAlignment,
        margin_v: captionMarginV,
        export_compression: compressionBody,
        target_mode: targetMode,
        quality_profile: qualityProfile,
        enhancement_model: enhancementModel,
        denoise,
        restoration,
        allow_fallback: allowFallback,
        temporal_stabilize: temporalStabilize,
        composition,
        face_cam_position: faceCamPosition,
        stack_split_pct: stackSplit,
        adaptive_stack: adaptiveStack,
        face_cam_overrides: faceCamOverrides,
        crop_x_pct: cropX,
        crop_y_pct: cropY,
      }),
    [
      captionPreset,
      captionPosition,
      captionPositionPct,
      captionAlignment,
      captionMarginV,
      compressionBody,
      targetMode,
      qualityProfile,
      enhancementModel,
      denoise,
      restoration,
      allowFallback,
      temporalStabilize,
      composition,
      faceCamPosition,
      stackSplit,
      adaptiveStack,
      faceCamOverrides,
      cropX,
      cropY,
    ],
  );
  const captionControlsDisabled = !captions || captionSaveState === "loading";

  useEffect(() => {
    let alive = true;
    hydratedRef.current = false;
    apiJson<{
      resolved?: { captions?: Record<string, unknown>; export?: Record<string, unknown> };
      overrides?: Record<string, unknown>;
    }>(`/api/projects/${projectId}/settings`)
      .then((loaded) => {
        if (!alive) return;
        const stored = (loaded.resolved?.captions ?? {}) as Record<string, unknown>;
        const preset =
          stored.preset === "custom" ||
          (typeof stored.preset === "string" && CAPTION_PRESETS.some((entry) => entry.id === stored.preset))
            ? (stored.preset as CaptionPresetId)
            : DEFAULT_CAPTION_PRESET;
        const position =
          stored.position === "top" ||
          stored.position === "center" ||
          stored.position === "bottom" ||
          stored.position === "custom"
            ? stored.position
            : DEFAULT_CAPTION_POSITION;
        const positionPct =
          typeof stored.position_pct === "number" ? stored.position_pct : DEFAULT_CAPTION_POSITION_PCT;
        const alignment =
          stored.alignment === "left" || stored.alignment === "right" || stored.alignment === "center"
            ? stored.alignment
            : DEFAULT_CAPTION_ALIGNMENT;
        const margin = typeof stored.margin_v === "number" ? stored.margin_v : DEFAULT_CAPTION_MARGIN_V;
        setCaptionPreset(preset);
        setCaptionPosition(position);
        setCaptionPositionPct(positionPct);
        setCaptionAlignment(alignment);
        setCaptionMarginV(margin);

        // The resolved export section already includes profile defaults, so the
        // controls show what the encoder will actually use on first paint.
        const resolvedExport = (loaded.resolved?.export ?? {}) as Record<string, unknown>;
        const storedComposition =
          typeof resolvedExport.composition === "string" && COMPOSITION_IDS.has(resolvedExport.composition)
            ? (resolvedExport.composition as CompositionModeId)
            : "auto";
        const storedFaceCamPosition =
          typeof resolvedExport.face_cam_position === "string" &&
          FACE_CAM_POSITION_IDS.has(resolvedExport.face_cam_position)
            ? (resolvedExport.face_cam_position as FaceCamPositionId)
            : DEFAULT_FACE_CAM_POSITION;
        const storedSplit =
          typeof resolvedExport.stack_split_pct === "number"
            ? Math.min(80, Math.max(35, resolvedExport.stack_split_pct))
            : 60;
        const storedCropX = typeof resolvedExport.crop_x_pct === "number" ? resolvedExport.crop_x_pct : 50;
        const storedCropY = typeof resolvedExport.crop_y_pct === "number" ? resolvedExport.crop_y_pct : 50;
        const storedAdaptive = resolvedExport.adaptive_stack === true;
        const storedFps = typeof resolvedExport.fps === "number" ? resolvedExport.fps : 30;
        const storedOverrides = normalizeFaceCamOverrides(resolvedExport.face_cam_overrides);
        setExportFps(storedFps);
        setComposition(storedComposition);
        setFaceCamPosition(storedFaceCamPosition);
        setStackSplit(storedSplit);
        setAdaptiveStack(storedAdaptive);
        setFaceCamOverrides(storedOverrides);
        setCropX(storedCropX);
        setCropY(storedCropY);

        const storedExport = (loaded.overrides?.export ?? {}) as Record<string, unknown>;
        const savedCompression = storedExport.compression;
        let nextPreset: CompressionPresetId = "balanced";
        let nextCustom = { crf: 23, audio: 128, width: 1080, fps: 30 as number | null };
        if (typeof savedCompression === "string" && savedCompression in COMPRESSION_FALLBACK) {
          nextPreset = savedCompression as CompressionPresetId;
        } else if (savedCompression && typeof savedCompression === "object") {
          const raw = savedCompression as Record<string, unknown>;
          const preset = raw.preset;
          if (typeof preset === "string" && preset in COMPRESSION_FALLBACK) nextPreset = preset as CompressionPresetId;
          nextCustom = {
            crf: typeof raw.crf === "number" ? raw.crf : nextCustom.crf,
            audio: typeof raw.audio_bitrate_kbps === "number" ? raw.audio_bitrate_kbps : nextCustom.audio,
            width: typeof raw.resolution_cap === "number" ? raw.resolution_cap : nextCustom.width,
            fps: raw.max_fps === null || typeof raw.max_fps === "number" ? (raw.max_fps as number | null) : nextCustom.fps,
          };
        }
        setCompression(nextPreset);
        setCustomCrf(nextCustom.crf);
        setCustomAudio(nextCustom.audio);
        setCustomWidth(nextCustom.width);
        setCustomFps(nextCustom.fps);

        const targetModes: TargetMode[] = ["source", "720", "1080", "1440", "2160", "custom"];
        const storedTargetMode = targetModes.includes(storedExport.resolution_mode as TargetMode)
          ? (storedExport.resolution_mode as TargetMode)
          : "1080";
        const storedEnhancement = (
          typeof storedExport.enhancement === "object" && storedExport.enhancement !== null
            ? (storedExport.enhancement as Record<string, unknown>)
            : {}
        ) as Record<string, unknown>;
        const storedProfile =
          storedEnhancement.profile === "clean" ||
          storedEnhancement.profile === "ai" ||
          storedEnhancement.profile === "ai_plus" ||
          storedEnhancement.profile === "standard"
            ? (storedEnhancement.profile as EnhancementProfile)
            : "standard";
        const storedModel =
          storedEnhancement.model_id === "espcn-x2" ||
          storedEnhancement.model_id === "espcn-x4" ||
          storedEnhancement.model_id === "realesrgan-x2"
            ? (storedEnhancement.model_id as EnhancementSettings["model_id"])
            : "auto";
        setTargetMode(storedTargetMode);
        setQualityProfile(storedProfile);
        setEnhancementModel(storedModel);
        setDenoise(typeof storedEnhancement.denoise === "number" ? storedEnhancement.denoise : 0);
        setRestoration(
          typeof storedEnhancement.restoration === "number" ? storedEnhancement.restoration : 1,
        );
        setAllowFallback(storedEnhancement.allow_fallback !== false);
        setTemporalStabilize(storedEnhancement.temporal_stabilize === true);
        if (typeof storedExport.custom_width === "number") setTargetWidth(storedExport.custom_width);
        if (typeof storedExport.custom_height === "number") setTargetHeight(storedExport.custom_height);

        savedSnapshotRef.current = JSON.stringify({
          preset,
          position,
          position_pct: positionPct,
          alignment,
          margin_v: margin,
          export_compression: compressionBodyFor(nextPreset, nextCustom),
          target_mode: storedTargetMode,
          quality_profile: storedProfile,
          enhancement_model: storedModel,
          denoise: typeof storedEnhancement.denoise === "number" ? storedEnhancement.denoise : 0,
          restoration:
            typeof storedEnhancement.restoration === "number" ? storedEnhancement.restoration : 1,
          allow_fallback: storedEnhancement.allow_fallback !== false,
          temporal_stabilize: storedEnhancement.temporal_stabilize === true,
          composition: storedComposition,
          face_cam_position: storedFaceCamPosition,
          stack_split_pct: storedSplit,
          adaptive_stack: storedAdaptive,
          face_cam_overrides: storedOverrides,
          crop_x_pct: storedCropX,
          crop_y_pct: storedCropY,
        });
        hydratedRef.current = true;
        setCaptionSaveState("saved");
      })
      .catch(() => {
        if (!alive) return;
        hydratedRef.current = true;
        savedSnapshotRef.current = JSON.stringify({
          preset: DEFAULT_CAPTION_PRESET,
          position: DEFAULT_CAPTION_POSITION,
          position_pct: DEFAULT_CAPTION_POSITION_PCT,
          alignment: DEFAULT_CAPTION_ALIGNMENT,
          margin_v: DEFAULT_CAPTION_MARGIN_V,
          export_compression: compressionBodyFor("balanced", { crf: 23, audio: 128, width: 1080, fps: 30 }),
          target_mode: "1080",
          quality_profile: "standard",
          enhancement_model: "auto",
          denoise: 0,
          restoration: 1,
          allow_fallback: true,
          temporal_stabilize: false,
          composition: "auto",
          face_cam_position: DEFAULT_FACE_CAM_POSITION,
          stack_split_pct: 60,
          adaptive_stack: false,
          face_cam_overrides: [],
          crop_x_pct: 50,
          crop_y_pct: 50,
        });
        setCaptionSaveState("error");
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  useEffect(() => {
    if (!hydratedRef.current || settingsSnapshot === savedSnapshotRef.current) return;
    setCaptionSaveState("saving");
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const current = await apiJson<{ overrides?: Record<string, unknown> }>(
            `/api/projects/${projectId}/settings`,
          );
          const base = current.overrides ?? {};
          const existing =
            typeof base.captions === "object" && base.captions !== null
              ? (base.captions as Record<string, unknown>)
              : {};
          const baseExport =
            typeof base.export === "object" && base.export !== null
              ? (base.export as Record<string, unknown>)
              : {};
          // The saved mode is class/source/custom. A target mode such as "1080"
          // belongs in `resolution`; writing it here makes automatic production
          // reject every clip.
          const exportOverrides: {
            resolution_mode: "class" | "source" | "custom";
            [key: string]: unknown;
          } = {
            ...baseExport,
            compression: compressionBody,
            resolution_mode: targetInfo.resolution_mode,
            custom_width: targetInfo.custom_width,
            custom_height: targetInfo.custom_height,
            resolution,
            enhancement,
            composition,
            face_cam_position: faceCamPosition,
            stack_split_pct: stackSplit,
            adaptive_stack: adaptiveStack,
            face_cam_overrides: faceCamOverrides,
            crop_x_pct: cropX,
            crop_y_pct: cropY,
          };
          await apiJson(`/api/projects/${projectId}/settings`, {
            method: "PATCH",
            body: {
              overrides: {
                ...base,
                captions: {
                  ...existing,
                  preset: captionPreset,
                  position: captionPosition,
                  position_pct: captionPositionPct,
                  alignment: captionAlignment,
                  margin_v: captionMarginV,
                },
                export: exportOverrides,
              },
            },
          });
          savedSnapshotRef.current = settingsSnapshot;
          setCaptionSaveState("saved");
        } catch {
          setCaptionSaveState("error");
        }
      })();
    }, 450);
    return () => window.clearTimeout(timer);
  }, [
    captionAlignment,
    captionMarginV,
    captionPosition,
    captionPositionPct,
    captionPreset,
    compressionBody,
    composition,
    cropX,
    cropY,
    faceCamPosition,
    adaptiveStack,
    faceCamOverrides,
    projectId,
    settingsSnapshot,
    stackSplit,
    targetMode,
    targetInfo.resolution_mode,
    targetInfo.custom_width,
    targetInfo.custom_height,
    resolution,
    enhancement,
  ]);

  useEffect(() => {
    // A comparison is evidence for one exact configuration. Any setting that
    // changes the frame invalidates it, including the AI Enhance+ gate.
    setQualityPreview(null);
    setQualityPreviewError(null);
  }, [
    aspect,
    targetMode,
    targetWidth,
    targetHeight,
    qualityProfile,
    enhancementModel,
    denoise,
    restoration,
    temporalStabilize,
    composition,
    faceCamPosition,
    stackSplit,
    adaptiveStack,
    cropX,
    cropY,
  ]);

  useEffect(() => {
    let alive = true;
    const timer = window.setTimeout(() => {
      apiJson<{ estimated_bytes: number; width: number; height: number; fps: number }>(
        `/api/projects/${projectId}/renders/estimate`,
        {
          method: "POST",
          body: {
            aspect,
            resolution,
            resolution_mode: targetInfo.resolution_mode,
            custom_width: targetInfo.custom_width,
            custom_height: targetInfo.custom_height,
            duration_ms: durationMs,
            fps: exportFps,
            compression: compressionBody,
            enhancement,
          },
        },
      )
        .then((value) => {
          if (alive) setEstimate(value);
        })
        .catch(() => {
          if (alive) setEstimate(null);
        });
    }, 350);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [
    aspect,
    resolution,
    targetInfo.resolution_mode,
    targetInfo.custom_width,
    targetInfo.custom_height,
    durationMs,
    exportFps,
    compressionBody,
    enhancement,
    projectId,
  ]);

  function resetCaptionPosition() {
    setCaptionPosition(DEFAULT_CAPTION_POSITION);
    setCaptionPositionPct(DEFAULT_CAPTION_POSITION_PCT);
    setCaptionAlignment(DEFAULT_CAPTION_ALIGNMENT);
    setCaptionMarginV(DEFAULT_CAPTION_MARGIN_V);
  }

  const latestComplete = useMemo(
    () => renders.find((render) => render.status === "completed"),
    [renders],
  );
  const activeGamingPreset = GAMING_PRESETS.find((preset) => preset.composition === composition);

  function chooseProfile(next: EnhancementProfile) {
    setQualityProfile(next);
    const meta =
      capabilities?.enhancement?.profiles.find((entry) => entry.id === next) ??
      QUALITY_PROFILE_FALLBACK[next];
    setDenoise("default_denoise" in meta ? meta.default_denoise : meta.denoise);
    setRestoration("default_restoration" in meta ? meta.default_restoration : meta.restoration);
    setTemporalStabilize(next === "ai_plus");
    setQualityPreview(null);
    setQualityPreviewError(null);
  }

  async function installSelectedModel() {
    if (enhancementModel === "auto") return;
    setInstallError(null);
    setInstallMessage("Queueing the one-time model download...");
    try {
      const result = await apiJson<{ job?: { id: string } | null; installed?: boolean }>(
        `/api/enhancement/models/${enhancementModel}/install`,
        { method: "POST" },
      );
      if (result.installed) {
        setInstalledNow((current) => [...new Set([...current, enhancementModel])]);
        setInstallMessage("The model is ready.");
        setModelInstallJob(null);
        return;
      }
      setModelInstallJob(result.job?.id ?? null);
      setInstallMessage("Downloading weights only. Your video never leaves this machine.");
    } catch (caught) {
      setInstallError(caught instanceof Error ? caught.message : "The model download could not be queued.");
      setInstallMessage(null);
    }
  }

  useEffect(() => {
    if (!modelInstallJob) return;
    let alive = true;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const payload = await apiJson<{
            job: { state: string; message?: string | null; progress?: number | null; error_message?: string | null };
          }>(`/api/jobs/${modelInstallJob}`);
          if (!alive) return;
          const job = payload.job;
          if (job.message) setInstallMessage(job.message);
          if (job.state === "completed") {
            setInstalledNow((current) => [...new Set([...current, enhancementModel])]);
            setInstallMessage("The model is ready and was benchmarked on this machine.");
            setModelInstallJob(null);
            window.clearInterval(timer);
          } else if (job.state === "failed" || job.state === "cancelled") {
            setInstallError(job.error_message || "The model download did not finish.");
            setInstallMessage(null);
            setModelInstallJob(null);
            window.clearInterval(timer);
          }
        } catch {
          // A transient polling failure is not a failed download.
        }
      })();
    }, 1500);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [modelInstallJob, enhancementModel]);

  async function runQualityPreview() {
    setQualityPreviewBusy(true);
    setQualityPreviewError(null);
    try {
      const result = await apiJson<QualityPreview>(`/api/projects/${projectId}/renders/quality-preview`, {
        method: "POST",
        body: {
          candidate_id: candidateId,
          aspect,
          resolution,
          resolution_mode: targetInfo.resolution_mode,
          custom_width: targetInfo.custom_width,
          custom_height: targetInfo.custom_height,
          enhancement,
          fps: exportFps,
          compression: compressionBody,
          composition,
          face_cam_position: faceCamPosition,
          stack_split_pct: stackSplit,
          adaptive_stack: adaptiveStack,
          face_cam_overrides: faceCamOverrides,
          crop_x_pct: cropX,
          crop_y_pct: cropY,
        },
      });
      setQualityPreview(result);
      setComparisonPct(50);
    } catch (caught) {
      setQualityPreviewError(
        caught instanceof Error ? caught.message : "The comparison frame could not be rendered.",
      );
    } finally {
      setQualityPreviewBusy(false);
    }
  }

  async function submit() {
    setError(null);
    try {
      await onRender({
        aspect,
        resolution,
        resolution_mode: targetInfo.resolution_mode,
        custom_width: targetInfo.custom_width,
        custom_height: targetInfo.custom_height,
        enhancement,
        fps: exportFps,
        composition,
        face_cam_position: faceCamPosition,
        stack_split_pct: stackSplit,
        adaptive_stack: adaptiveStack,
        face_cam_overrides: faceCamOverrides,
        crop_x_pct: cropX,
        crop_y_pct: cropY,
        caption_preset: captionPreset,
        captions_enabled: captions,
        captions: {
          font_family: captionFont,
          position: captionPosition,
          position_pct: captionPositionPct,
          alignment: captionAlignment,
          margin_v: captionMarginV,
        },
        normalize_audio: normalize,
        compression: compressionBody,
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The render could not be queued.");
    }
  }

  return (
    <section className="surface space-y-4 p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="label">Export</p>
          <p className="mt-1 text-[12.5px] text-mist-500">
            Rendered locally with ffmpeg from your source file. {formatDuration(durationMs)} of footage.
          </p>
        </div>
        <Badge tone="neutral">{capabilities?.media.ffmpeg.available === false ? "ffmpeg missing" : "H.264 · AAC"}</Badge>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <p className="label">Frame</p>
          <div className="grid gap-1.5">
            {ASPECTS.map((entry) => (
              <button
                key={entry.id}
                type="button"
                onClick={() => setAspect(entry.id)}
                aria-pressed={aspect === entry.id}
                className={cn(
                  "flex items-center justify-between rounded-md border px-2.5 py-1.5 text-left transition-colors",
                  aspect === entry.id
                    ? "border-ember-500/50 bg-ember-500/[0.08]"
                    : "border-white/8 bg-black/20 hover:border-white/16",
                )}
              >
                <span className="text-[12.5px] text-mist-200">{entry.label}</span>
                <span className="text-[10.5px] text-mist-600">{entry.hint}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="space-y-3">
          <div className="space-y-1.5">
            <p className="label">Target resolution</p>
            <div className="grid grid-cols-2 gap-1.5">
              {TARGET_OPTIONS.map((option) => (
                <button
                  key={option.id}
                  type="button"
                  onClick={() => setTargetMode(option.id)}
                  aria-pressed={targetMode === option.id}
                  className={cn(
                    "rounded-md border px-2 py-1.5 text-left transition-colors",
                    targetMode === option.id
                      ? "border-ember-500/50 bg-ember-500/[0.08]"
                      : "border-white/8 bg-black/20 hover:border-white/16",
                  )}
                >
                  <span className="block text-[12px] text-mist-200">{option.label}</span>
                  <span className="block text-[10px] text-mist-600">{option.hint}</span>
                </button>
              ))}
            </div>
            {targetMode === "custom" ? (
              <div className="grid grid-cols-2 gap-1.5">
                <label className="space-y-0.5 text-[10.5px] text-mist-500">
                  <span>Width</span>
                  <input
                    type="number"
                    min={320}
                    max={3840}
                    step={2}
                    value={targetWidth}
                    onChange={(event) => setTargetWidth(Number(event.target.value))}
                    className="field h-8 w-full text-[12px]"
                  />
                </label>
                <label className="space-y-0.5 text-[10.5px] text-mist-500">
                  <span>Height</span>
                  <input
                    type="number"
                    min={320}
                    max={4320}
                    step={2}
                    value={targetHeight}
                    onChange={(event) => setTargetHeight(Number(event.target.value))}
                    className="field h-8 w-full text-[12px]"
                  />
                </label>
              </div>
            ) : null}
            <p className={cn("text-[10.5px] leading-relaxed", targetInfo.valid ? "text-mist-600" : "text-signal-bad/90")}>
              {targetInfo.valid
                ? `Source ${source.width}×${source.height} → output ${targetInfo.width}×${targetInfo.height} (${targetInfo.resolution_mode}).`
                : targetInfo.error}
            </p>
          </div>
          <div className="space-y-1.5">
            <p className="label">How to fill the frame</p>
            <div className="grid gap-1.5">
              {COMPOSITION_MODES.map((mode) => (
                <button
                  key={mode.id}
                  type="button"
                  onClick={() => setComposition(mode.id)}
                  aria-pressed={composition === mode.id}
                  className={cn(
                    "flex items-center justify-between gap-2 rounded-md border px-2.5 py-1.5 text-left transition-colors",
                    composition === mode.id
                      ? "border-ember-500/50 bg-ember-500/[0.08]"
                      : "border-white/8 bg-black/20 hover:border-white/16",
                    mode.id === "split" && "opacity-60",
                  )}
                >
                  <span className="text-[12.5px] text-mist-200">{mode.label}</span>
                  <span className="text-right text-[10.5px] text-mist-600">{mode.hint}</span>
                </button>
              ))}
            </div>
          </div>
          <div className="space-y-1.5">
            <p className="label">Gaming layouts</p>
            <div className="grid gap-1.5">
              {GAMING_PRESETS.map((preset) => (
                <button
                  key={preset.id}
                  type="button"
                  onClick={() => setComposition(preset.composition)}
                  aria-pressed={activeGamingPreset?.id === preset.id}
                  className={cn(
                    "flex items-center justify-between gap-2 rounded-md border px-2.5 py-1.5 text-left transition-colors",
                    activeGamingPreset?.id === preset.id
                      ? "border-signal-info/50 bg-signal-info/[0.08]"
                      : "border-white/8 bg-black/20 hover:border-white/16",
                  )}
                >
                  <span className="text-[12.5px] text-mist-200">{preset.label}</span>
                  <span className="text-right text-[10.5px] text-mist-600">
                    {COMPOSITION_MODES.find((mode) => mode.id === preset.composition)?.label}
                  </span>
                </button>
              ))}
            </div>
            {activeGamingPreset ? (
              <p className="text-[10.5px] leading-snug text-mist-500">
                {activeGamingPreset.description} {activeGamingPreset.warns}
              </p>
            ) : null}
          </div>
          <div className="space-y-1.5">
            <label className="flex items-center gap-2 text-[12.5px] text-mist-300">
              <input
                type="checkbox"
                className="size-3.5 accent-ember-500"
                checked={normalize}
                onChange={(event) => setNormalize(event.target.checked)}
              />
              Normalise loudness
            </label>
          </div>
        </div>
      </div>

      <div className="space-y-3 border-t border-white/6 pt-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <p className="label flex items-center gap-1.5">
              <Sparkles className="size-3" strokeWidth={2} />
              Video quality
            </p>
            <p className="mt-1 text-[11.5px] leading-relaxed text-mist-500">
              Enhancement is optional. It runs locally from the retained source; no video is uploaded.
            </p>
          </div>
          <Badge
            tone={
              qualityProfile === "ai_fast" || qualityProfile === "ai" || qualityProfile === "ai_plus"
                ? "info"
                : "neutral"
            }
          >
            {profileMeta.engine}
          </Badge>
        </div>

        <div className="grid gap-1.5 sm:grid-cols-2">
          {capabilities?.enhancement?.profiles.map((profile) => (
            <button
              key={profile.id}
              type="button"
              onClick={() => chooseProfile(profile.id)}
              aria-pressed={qualityProfile === profile.id}
              className={cn(
                "rounded-md border px-2.5 py-2 text-left transition-colors",
                qualityProfile === profile.id
                  ? "border-ember-500/50 bg-ember-500/[0.08]"
                  : "border-white/8 bg-black/20 hover:border-white/16",
              )}
            >
              <span className="flex items-center justify-between gap-2">
                <span className="text-[12.5px] font-medium text-mist-100">{profile.label}</span>
                {profile.uses_ai ? (
                  <span className="text-[10px] uppercase tracking-wide text-ember-300">AI</span>
                ) : null}
              </span>
              <span className="mt-0.5 block text-[11px] leading-snug text-mist-500">{profile.summary}</span>
            </button>
          )) ??
            (Object.keys(QUALITY_PROFILE_FALLBACK) as EnhancementProfile[]).map((profile) => (
              <button
                key={profile}
                type="button"
                onClick={() => chooseProfile(profile)}
                aria-pressed={qualityProfile === profile}
                className={cn(
                  "rounded-md border px-2.5 py-2 text-left transition-colors",
                  qualityProfile === profile
                    ? "border-ember-500/50 bg-ember-500/[0.08]"
                    : "border-white/8 bg-black/20 hover:border-white/16",
                )}
              >
                <span className="text-[12.5px] font-medium text-mist-100">
                  {QUALITY_PROFILE_FALLBACK[profile].label}
                </span>
                <span className="mt-0.5 block text-[11px] leading-snug text-mist-500">
                  {QUALITY_PROFILE_FALLBACK[profile].summary}
                </span>
              </button>
            ))}
        </div>

        <div className="grid gap-2 rounded-md border border-white/8 bg-black/20 p-2.5 sm:grid-cols-2">
          <div className="space-y-1">
            <p className="text-[11px] text-mist-500">Source → target</p>
            <p className="numeric text-[12px] text-mist-200">
              {source.width}×{source.height} → {targetInfo.width}×{targetInfo.height} · {exportFps} fps
            </p>
            <p className="text-[10.5px] leading-relaxed text-mist-600">
              {qualityProfile === "standard"
                ? "Lanczos resizing only."
                : qualityProfile === "clean"
                  ? `Denoise ${denoise}, then lanczos resizing. No AI model.`
                  : qualityProfile === "ai_fast"
                    ? `${selectedModel?.label ?? "ESPCN 2×"}, no denoise or blend. Fast mode may let composition lanczos the model result to the target.`
                    : `${selectedModel?.label ?? "AI model"} with ${Math.round(restoration * 100)}% model blend.`}
              {estimate && (estimate.width !== targetInfo.width || estimate.height !== targetInfo.height)
                ? ` Compression caps the encoded frame at ${estimate.width}×${estimate.height}.`
                : ""}
            </p>
          </div>
          <div className="space-y-1">
            <p className="text-[11px] text-mist-500">AI acceleration</p>
            <p className="text-[12px] text-mist-200">
              {capabilities?.enhancement?.devices.device ?? "CPU"}
              {capabilities?.enhancement?.devices.gpu_inference
                ? " · GPU available"
                : capabilities?.enhancement?.devices.device === "CPU"
                  ? " only"
                  : " · CPU fallback"}
            </p>
            <p className="text-[10.5px] leading-relaxed text-mist-600">
              {qualityProfile === "ai" || qualityProfile === "ai_plus" || qualityProfile === "ai_fast"
                ? enhancementEstimate === null
                  ? "Processing time has not been measured on this machine. The first AI preview measures it."
                  : `≈ ${formatSeconds(enhancementEstimate)} for this clip, from measured enhancement throughput on this machine. This is an estimate.`
                : "No AI estimate applies to this profile."}
            </p>
          </div>
        </div>

        {qualityProfile !== "standard" ? (
          <div className="grid gap-2 rounded-md border border-white/8 bg-black/20 p-2.5 sm:grid-cols-2">
            {qualityProfile !== "ai_fast" ? (
              <label className="block space-y-1 text-[11px] text-mist-500">
                <span className="flex justify-between">
                  <span>Denoise strength</span>
                  <span className="numeric text-mist-300">{denoise}</span>
                </span>
                <input
                  type="range"
                  min={0}
                  max={100}
                  step={5}
                  value={denoise}
                  onChange={(event) => setDenoise(Number(event.target.value))}
                  className="w-full accent-ember-500"
                />
                <span className="block text-[10.5px] text-mist-600">
                  Conservative hqdn3d before the model. 0 disables it.
                </span>
              </label>
            ) : (
              <p className="self-end pb-1 text-[10.5px] leading-relaxed text-mist-600">
                Fast mode fixes denoise at 0 and the model blend at 100%. It writes the native
                model-size intermediate for a quicker encode.
              </p>
            )}
            {qualityProfile === "ai" || qualityProfile === "ai_plus" ? (
              <label className="block space-y-1 text-[11px] text-mist-500">
                <span className="flex justify-between">
                  <span>Restoration blend</span>
                  <span className="numeric text-mist-300">{Math.round(restoration * 100)}%</span>
                </span>
                <input
                  type="range"
                  min={0}
                  max={100}
                  step={5}
                  value={Math.round(restoration * 100)}
                  onChange={(event) => setRestoration(Number(event.target.value) / 100)}
                  className="w-full accent-ember-500"
                />
                <span className="block text-[10.5px] text-mist-600">
                  Lower keeps more of a clean resize and reduces invented texture.
                </span>
              </label>
            ) : null}
            {qualityProfile === "ai" || qualityProfile === "ai_plus" || qualityProfile === "ai_fast" ? (
              <>
                <label className="space-y-1 text-[11px] text-mist-500">
                  <span>Model</span>
                  <select
                    className="field h-8 w-full text-[12px]"
                    value={enhancementModel}
                    onChange={(event) =>
                      setEnhancementModel(event.target.value as EnhancementSettings["model_id"])
                    }
                  >
                    <option value="auto">Auto (smallest model that covers the scale)</option>
                    {enhancementModels.map((model) => (
                      <option key={model.id} value={model.id}>
                        {model.label}
                        {model.installed || installedNow.includes(model.id) ? "" : " — not installed"}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="flex flex-col justify-end gap-1">
                  <label className="flex items-start gap-2 text-[11px] text-mist-400">
                    <input
                      type="checkbox"
                      className="mt-0.5 size-3.5 accent-ember-500"
                      checked={temporalStabilize}
                      onChange={(event) => setTemporalStabilize(event.target.checked)}
                    />
                    <span>
                      Stabilise static HUD/text frames
                      <span className="block text-[10.5px] text-mist-600">
                        Averages only near-static consecutive frames. It does not interpolate motion.
                      </span>
                    </span>
                  </label>
                  <label className="flex items-start gap-2 text-[11px] text-mist-400">
                    <input
                      type="checkbox"
                      className="mt-0.5 size-3.5 accent-ember-500"
                      checked={allowFallback}
                      onChange={(event) => setAllowFallback(event.target.checked)}
                    />
                    <span>
                      Fall back to resizing if the model cannot run
                      <span className="block text-[10.5px] text-mist-600">
                        The render report will say that no AI model ran.
                      </span>
                    </span>
                  </label>
                </div>
              </>
            ) : null}
          </div>
        ) : null}

        {qualityProfile === "ai_fast" || qualityProfile === "ai" || qualityProfile === "ai_plus" ? (
          <div className="space-y-2 rounded-md border border-white/8 bg-black/20 p-2.5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <p className="label">Before / after</p>
                <p className="text-[10.5px] leading-relaxed text-mist-600">
                  Same source frame, timestamp and composition. Drag the divider.
                </p>
              </div>
              <div className="flex items-center gap-2">
                {!modelInstalled ? (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm h-7 px-2 text-[11px]"
                    onClick={() => void installSelectedModel()}
                    disabled={Boolean(modelInstallJob)}
                  >
                    <Download className="size-3" strokeWidth={2} />
                    {modelInstallJob ? "Downloading" : "Install model"}
                  </button>
                ) : null}
                <button
                  type="button"
                  className="btn btn-ghost btn-sm h-7 px-2 text-[11px]"
                  onClick={() => void runQualityPreview()}
                  disabled={qualityPreviewBusy}
                >
                  {qualityPreviewBusy ? (
                    <Loader2 className="size-3 animate-spin" strokeWidth={2} />
                  ) : (
                    <Sparkles className="size-3" strokeWidth={2} />
                  )}
                  {qualityPreviewBusy ? "Rendering" : "Compare a frame"}
                </button>
              </div>
            </div>
            {installMessage ? <p className="text-[10.5px] text-mist-400">{installMessage}</p> : null}
            {installError ? <Alert tone="bad">{installError}</Alert> : null}
            {!aiRuntimeReady ? (
              <p className="text-[10.5px] text-signal-warn/90">
                The AI runtime is not available in this worker. Standard and Clean still work.
              </p>
            ) : null}
            {qualityPreviewError ? <Alert tone="bad">{qualityPreviewError}</Alert> : null}
            {qualityPreview ? (
              <div className="space-y-2">
                <div className="relative overflow-hidden rounded border border-white/10 bg-black">
                  <img src={qualityPreview.original} alt="Original frame" className="block w-full" />
                  <div
                    className="absolute inset-0 overflow-hidden"
                    style={{ clipPath: `inset(0 0 0 ${comparisonPct}%)` }}
                  >
                    <img src={qualityPreview.enhanced} alt="Enhanced frame" className="block w-full" />
                  </div>
                  <div
                    className="pointer-events-none absolute inset-y-0 w-px bg-ember-400/80"
                    style={{ left: `${comparisonPct}%` }}
                  />
                  <span className="absolute left-2 top-2 rounded bg-black/70 px-1.5 py-0.5 text-[10px] text-mist-200">
                    Enhanced
                  </span>
                  <span className="absolute right-2 top-2 rounded bg-black/70 px-1.5 py-0.5 text-[10px] text-mist-200">
                    Original
                  </span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={comparisonPct}
                  onChange={(event) => setComparisonPct(Number(event.target.value))}
                  className="w-full accent-ember-500"
                  aria-label="Before and after divider"
                />
                <p className="text-[10.5px] leading-relaxed text-mist-600">
                  {qualityPreview.applied_model
                    ? `${qualityPreview.engine} on ${qualityPreview.device ?? "this device"}. `
                    : "No AI model ran for this comparison; the right side is conventional resizing. "}
                  {qualityPreview.estimated_frame_seconds
                    ? `Measured model time ≈ ${qualityPreview.estimated_frame_seconds.toFixed(2)} s per source frame. `
                    : ""}
                  AI can invent texture. If this frame looks worse, choose Standard.
                </p>
                {qualityPreview.warnings.map((warning) => (
                  <p key={warning} className="text-[10.5px] leading-relaxed text-signal-warn/85">
                    {warning}
                  </p>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}

        <p className="text-[11px] leading-relaxed text-mist-600">
          Upscaling reconstructs pixels; it does not restore native detail the source never captured. Captions,
          crop, face-cam tracking and audio settings are unchanged by the enhancement profile.
        </p>
      </div>

      <div className="grid gap-4 border-t border-white/6 pt-3 lg:grid-cols-[220px_1fr]">
        <CompositionPreview
          projectId={projectId}
          candidateId={candidateId}
          aspect={aspect}
          resolution={resolution}
          resolutionMode={targetInfo.resolution_mode}
          customWidth={targetInfo.custom_width}
          customHeight={targetInfo.custom_height}
          mode={composition}
          faceCamPosition={faceCamPosition}
          stackSplit={stackSplit}
          adaptiveStack={adaptiveStack}
          faceCamOverrides={faceCamOverrides}
          cropX={cropX}
          cropY={cropY}
          enabled={captionSaveState !== "loading"}
          onDetected={handleDetectedFacecam}
        />
        <div className="space-y-2">
          <p className="label">Layout preview</p>
          <p className="text-[11.5px] leading-relaxed text-mist-500">
            {composition === "auto"
              ? "Scene changes are detected before encoding. Slides, tables and screen recordings are contained; a centred speaker is cropped."
              : COMPOSITION_MODES.find((mode) => mode.id === composition)?.hint}
          </p>
          {composition === "stack" ? (
            <div className="space-y-2 rounded-md border border-white/8 bg-black/20 p-2.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="label">Face-cam position</p>
                <div className="segmented" role="group" aria-label="Face-cam position">
                  {FACE_CAM_POSITIONS.map((entry) => (
                    <button
                      key={entry.id}
                      type="button"
                      data-active={faceCamPosition === entry.id}
                      aria-pressed={faceCamPosition === entry.id}
                      onClick={() => setFaceCamPosition(entry.id)}
                    >
                      {entry.label}
                    </button>
                  ))}
                </div>
              </div>
              <label className="block space-y-1 text-[11px] text-mist-500">
                <span className="flex justify-between">
                  <span>Gameplay panel</span>
                  <span className="numeric text-mist-300">{stackSplit}% of the frame</span>
                </span>
                <input
                  type="range"
                  min={40}
                  max={75}
                  step={5}
                  value={stackSplit}
                  onChange={(event) => setStackSplit(Number(event.target.value))}
                  className="w-full accent-ember-500"
                />
                <span className="block text-[10.5px] leading-relaxed text-mist-600">
                  The face-cam takes the rest. Captions stay inside the gameplay panel, never over the face-cam.
                </span>
              </label>
              <label className="flex items-start gap-2 text-[11px] text-mist-400">
                <input
                  type="checkbox"
                  className="mt-0.5 size-3.5 accent-ember-500"
                  checked={adaptiveStack}
                  onChange={(event) => setAdaptiveStack(event.target.checked)}
                />
                <span>
                  Let the split follow the face-cam size
                  <span className="block text-[10.5px] leading-relaxed text-mist-600">
                    Off by default: the panels stay fixed while the source crops follow the face-cam. On, the
                    split shifts by up to 8 points per tracked range.
                  </span>
                </span>
              </label>
            </div>
          ) : null}
          {composition === "crop" || composition === "manual" ? (
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="space-y-1 text-[11px] text-mist-500">
                <span className="flex justify-between">
                  <span>Crop focus X</span>
                  <span className="numeric text-mist-300">{Math.round(cropX)}%</span>
                </span>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={cropX}
                  onChange={(event) => setCropX(Number(event.target.value))}
                  className="w-full accent-ember-500"
                />
              </label>
              <label className="space-y-1 text-[11px] text-mist-500">
                <span className="flex justify-between">
                  <span>Crop focus Y</span>
                  <span className="numeric text-mist-300">{Math.round(cropY)}%</span>
                </span>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={cropY}
                  onChange={(event) => setCropY(Number(event.target.value))}
                  className="w-full accent-ember-500"
                />
              </label>
            </div>
          ) : null}
          {composition === "stack" || composition === "facecam_focus" ? (
            <FaceCamCorrectionEditor
              overrides={faceCamOverrides}
              onChange={setFaceCamOverrides}
              durationMs={durationMs}
              detected={detectedFacecam}
            />
          ) : null}
          <p className="text-[11px] text-mist-600">
            The preview is one real frame drawn by the export filters. Layout and face-cam choices are saved on
            this project, so the next render and a reload use the same composition.
          </p>
        </div>
      </div>

      <div className="space-y-2 border-t border-white/6 pt-3">
        <div className="flex items-center justify-between">
          <p className="label flex items-center gap-1.5">
            <Type className="size-3" strokeWidth={2} />
            Captions
          </p>
          <label className="flex items-center gap-2 text-[12px] text-mist-300">
            <input
              type="checkbox"
              className="size-3.5 accent-ember-500"
              checked={captions}
              onChange={(event) => setCaptions(event.target.checked)}
            />
            Burn into the video
          </label>
        </div>
        <div className={cn("grid gap-1.5 sm:grid-cols-2", !captions && "pointer-events-none opacity-45")}>
          <label className="space-y-1 text-[11px] text-mist-500 sm:col-span-2">
            <span>Caption face</span>
            <select
              className="field h-8 w-full text-[12px]"
              value={captionFont}
              onChange={(event) => setCaptionFont(event.target.value)}
              disabled={!captions}
            >
              {CAPTION_FONTS.map((font) => (
                <option key={font.id} value={font.id}>
                  {font.label} — {font.note}
                </option>
              ))}
            </select>
          </label>
          {CAPTION_PRESETS.map((preset) => (
            <button
              key={preset.id}
              type="button"
              onClick={() => setCaptionPreset(preset.id)}
              aria-pressed={captionPreset === preset.id}
              disabled={captionControlsDisabled}
              className={cn(
                "rounded-md border px-2.5 py-2 text-left transition-colors",
                captionPreset === preset.id
                  ? "border-ember-500/50 bg-ember-500/[0.08]"
                  : "border-white/8 bg-black/20 hover:border-white/16",
              )}
            >
              <span className="flex items-center justify-between gap-2">
                <span className="text-[12.5px] font-medium text-mist-100">{preset.label}</span>
                <span
                  aria-hidden
                  className="size-2 rounded-full"
                  style={{ backgroundColor: preset.sample.highlight }}
                />
              </span>
              <span className="mt-0.5 block text-[11px] leading-snug text-mist-500">{preset.description}</span>
            </button>
          ))}
        </div>

        <div
          className={cn(
            "space-y-2 rounded-md border border-white/8 bg-black/20 p-2.5",
            !captions && "pointer-events-none opacity-45",
          )}
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="label">Placement</p>
            <button
              type="button"
              className="btn btn-ghost btn-sm h-6 px-1.5 text-[11px]"
              onClick={resetCaptionPosition}
              disabled={
                captionControlsDisabled ||
                (captionPosition === DEFAULT_CAPTION_POSITION &&
                  captionPositionPct === DEFAULT_CAPTION_POSITION_PCT &&
                  captionAlignment === DEFAULT_CAPTION_ALIGNMENT &&
                  captionMarginV === DEFAULT_CAPTION_MARGIN_V)
              }
            >
              Reset to bottom
            </button>
          </div>
          <div className="segmented w-full sm:w-auto" role="group" aria-label="Caption position">
            {CAPTION_POSITIONS.map((position) => (
              <button
                key={position.id}
                type="button"
                data-active={captionPosition === position.id}
                aria-pressed={captionPosition === position.id}
                onClick={() => setCaptionPosition(position.id)}
                disabled={captionControlsDisabled}
              >
                {position.label}
              </button>
            ))}
          </div>
          <p className="text-[11px] leading-relaxed text-mist-600">
            {CAPTION_POSITIONS.find((position) => position.id === captionPosition)?.hint}
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[11px] text-mist-500">Horizontal</span>
            <div className="segmented" role="group" aria-label="Caption alignment">
              {CAPTION_ALIGNMENTS.map((alignment) => (
                <button
                  key={alignment.id}
                  type="button"
                  data-active={captionAlignment === alignment.id}
                  aria-pressed={captionAlignment === alignment.id}
                  onClick={() => setCaptionAlignment(alignment.id)}
                  disabled={captionControlsDisabled}
                >
                  {alignment.label}
                </button>
              ))}
            </div>
          </div>
          {captionPosition === "bottom" ? (
            <label className="block space-y-1 text-[11px] text-mist-500">
              <span className="flex justify-between">
                <span>Bottom inset</span>
                <span className="numeric text-mist-300">{captionMarginV}px</span>
              </span>
              <input
                type="range"
                min={40}
                max={420}
                step={10}
                value={captionMarginV}
                onChange={(event) => setCaptionMarginV(Number(event.target.value))}
                className="w-full accent-ember-500"
                disabled={captionControlsDisabled}
              />
              <span className="block text-[10.5px] leading-relaxed text-mist-600">
                The export keeps captions above the platform-safe bottom edge.
              </span>
            </label>
          ) : null}
          {captionPosition === "custom" ? (
            <label className="block space-y-1 text-[11px] text-mist-500">
              <span className="flex justify-between">
                <span>Vertical position</span>
                <span className="numeric text-mist-300">{captionPositionPct}%</span>
              </span>
              <input
                type="range"
                min={0}
                max={100}
                step={1}
                value={captionPositionPct}
                onChange={(event) => setCaptionPositionPct(Number(event.target.value))}
                className="w-full accent-ember-500"
                disabled={captionControlsDisabled}
              />
              <span className="block text-[10.5px] leading-relaxed text-mist-600">
                0% is the top edge and 100% is the bottom. The renderer clamps the block inside the safe area.
              </span>
            </label>
          ) : null}
          <p
            aria-live="polite"
            className={cn(
              "text-[10.5px] leading-relaxed",
              captionSaveState === "error" ? "text-signal-bad/90" : "text-mist-600",
            )}
          >
            {captionSaveState === "loading"
              ? "Loading saved defaults..."
              : captionSaveState === "saving"
                ? "Saving defaults..."
                : captionSaveState === "error"
                  ? "Saved defaults could not be updated. This render still uses the selected options."
                  : "Caption and compression defaults are saved for this project."}
          </p>
        </div>

        <p className="text-[11px] text-mist-600">
          Captions come from the stored word timings, so the highlighted word always matches the audio.
        </p>
      </div>

      <div className="space-y-2 border-t border-white/6 pt-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="label">Compression</p>
          <span className="numeric text-[11px] text-mist-500">
            {estimate
              ? `≈ ${formatBytes(estimate.estimated_bytes)} · ${estimate.width}×${estimate.height} · ${Math.round(estimate.fps)} fps`
              : "size unavailable"}
            <span className="text-mist-600"> · approximate</span>
          </span>
        </div>
        <div className="grid gap-1.5 sm:grid-cols-2">
          {(Object.keys(COMPRESSION_FALLBACK) as CompressionPresetId[]).map((presetId) => {
            const known = capabilities?.compression?.presets.find((entry) => entry.id === presetId);
            const label = known?.label ?? COMPRESSION_FALLBACK[presetId].label;
            const note = known?.note ?? COMPRESSION_FALLBACK[presetId].note;
            return (
              <button
                key={presetId}
                type="button"
                aria-pressed={compression === presetId}
                onClick={() => setCompression(presetId)}
                className={cn(
                  "rounded-md border px-2.5 py-2 text-left transition-colors",
                  compression === presetId
                    ? "border-ember-500/50 bg-ember-500/[0.08]"
                    : "border-white/8 bg-black/20 hover:border-white/16",
                )}
              >
                <span className="text-[12.5px] font-medium text-mist-100">{label}</span>
                <span className="mt-0.5 block text-[11px] leading-snug text-mist-500">{note}</span>
              </button>
            );
          })}
        </div>
        {compression === "custom" ? (
          <div className="grid gap-2 rounded-md border border-white/8 bg-black/20 p-2.5 sm:grid-cols-2">
            <label className="block space-y-1 text-[11px] text-mist-500">
              <span className="flex justify-between">
                <span>Quality (CRF)</span>
                <span className="numeric text-mist-300">{customCrf}</span>
              </span>
              <input
                type="range"
                min={0}
                max={51}
                value={customCrf}
                onChange={(event) => setCustomCrf(Number(event.target.value))}
                className="w-full accent-ember-500"
              />
              <span className="block text-[10.5px] text-mist-600">Lower is better quality. 18 is high, 28 is small.</span>
            </label>
            <label className="block space-y-1 text-[11px] text-mist-500">
              <span className="flex justify-between">
                <span>Audio bitrate</span>
                <span className="numeric text-mist-300">{customAudio} kbps</span>
              </span>
              <input
                type="range"
                min={64}
                max={320}
                step={32}
                value={customAudio}
                onChange={(event) => setCustomAudio(Number(event.target.value))}
                className="w-full accent-ember-500"
              />
              <span className="block text-[10.5px] text-mist-600">64–320 kbps. Speech is fine at 128.</span>
            </label>
            <label className="block space-y-1 text-[11px] text-mist-500">
              <span>Resolution cap</span>
              <select
                className="field h-8 w-full text-[12px]"
                value={customWidth}
                onChange={(event) => setCustomWidth(Number(event.target.value))}
              >
                {[480, 720, 1080, 1440, 1920, 2160, 2560, 3840].map((width) => (
                  <option key={width} value={width}>
                    {width}p
                  </option>
                ))}
              </select>
              <span className="block text-[10.5px] text-mist-600">Caps the short edge; aspect is preserved.</span>
            </label>
            <label className="block space-y-1 text-[11px] text-mist-500">
              <span>Frame rate cap</span>
              <select
                className="field h-8 w-full text-[12px]"
                value={customFps ?? "source"}
                onChange={(event) =>
                  setCustomFps(event.target.value === "source" ? null : Number(event.target.value))
                }
              >
                <option value="source">Keep source rate</option>
                {[24, 25, 30, 60].map((fps) => (
                  <option key={fps} value={fps}>
                    {fps} fps
                  </option>
                ))}
              </select>
            </label>
          </div>
        ) : null}
        <p className="text-[11px] leading-relaxed text-mist-600">
          Compression is separate from resolution: a 1080p frame can still be a small file. The size above is
          estimated from duration and bitrate, not a guarantee.
        </p>
      </div>

      {error ? <Alert tone="bad">{error}</Alert> : null}

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-white/6 pt-3">
        <p className="text-[11.5px] text-mist-600">
          {disabled
            ? disabledReason
            : requiresComparison
              ? "Generate a before/after comparison before rendering AI Enhance+."
              : "Rendering keeps the source untouched and writes a new MP4."}
        </p>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          onClick={submit}
          disabled={busy || disabled || requiresComparison}
        >
          {busy ? <Loader2 className="size-3.5 animate-spin" strokeWidth={2} /> : <Film className="size-3.5" strokeWidth={2} />}
          {busy ? "Queued" : "Render clip"}
        </button>
      </div>

      <div className="space-y-2 border-t border-white/6 pt-3">
        <div className="flex items-center justify-between">
          <p className="label">Exports for this clip</p>
          {latestComplete ? (
            <button type="button" className="btn btn-ghost btn-sm h-6 px-1.5 text-[11px]" onClick={() => setShowPreview((value) => !value)}>
              {showPreview ? "Hide preview" : "Preview"}
            </button>
          ) : null}
        </div>

        {renders.length === 0 ? (
          <EmptyState
            title="No renders yet"
            description="Set the frame and captions, then render. The MP4 lands in this project's exports folder."
            className="py-7"
          />
        ) : (
          <ul className="space-y-1.5">
            {renders.map((render) => (
              <RenderRow key={render.id} render={render} />
            ))}
          </ul>
        )}

        {showPreview && latestComplete ? (
          <video
            key={latestComplete.id}
            src={`/api/renders/${latestComplete.id}/stream`}
            controls
            className="aspect-video w-full rounded-lg border border-white/10 bg-black"
          />
        ) : null}

        <p className="text-[11px] text-mist-600">
          Project media lives in <span className="numeric">data/projects/{projectId.slice(0, 6)}…/</span> on this machine.
          Downloads are served by the local worker, not a CDN.
        </p>
      </div>
    </section>
  );
}

function FaceCamCorrectionEditor({
  overrides,
  onChange,
  durationMs,
  detected,
}: {
  overrides: FaceCamOverrideDraft[];
  onChange: (value: FaceCamOverrideDraft[]) => void;
  durationMs: number;
  detected: DetectedFacecam | null;
}) {
  function update(index: number, patch: Partial<FaceCamOverrideDraft>) {
    onChange(overrides.map((entry, position) => (position === index ? { ...entry, ...patch } : entry)));
  }

  function updateRect(index: number, patch: Partial<FaceCamRect>) {
    const entry = overrides[index];
    if (!entry) return;
    update(index, { rect: { ...entry.rect, ...patch } });
  }

  function add() {
    const start = 0;
    const end = Math.max(start + 1_000, Math.min(5_000, Math.max(1_000, durationMs)));
    onChange([
      ...overrides,
      {
        start_ms: start,
        end_ms: end,
        rect: { x_pct: 0.66, y_pct: 0.62, width_pct: 0.3, height_pct: 0.34 },
        mode: "lock",
        reason: "Manual webcam region",
      },
    ]);
  }

  function addDetected() {
    if (!detected) return;
    const start = Math.max(0, detected.start_ms);
    const end = Math.max(start + 1_000, detected.end_ms);
    onChange([
      ...overrides,
      {
        start_ms: start,
        end_ms: end,
        rect: detected.rect,
        mode: "lock",
        reason: `Detected webcam panel (${detected.state ?? "detected"})`,
      },
    ]);
  }

  return (
    <div className="space-y-2 rounded-md border border-white/8 bg-black/20 p-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="label">Webcam region corrections</p>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="text-[11px] text-ember-300 hover:text-ember-200 disabled:cursor-not-allowed disabled:opacity-40"
            onClick={addDetected}
            disabled={!detected}
            title={detected ? "Create a locked correction from the detected panel" : "No detected panel to copy yet"}
          >
            Use detected region
          </button>
          <button type="button" className="text-[11px] text-ember-300 hover:text-ember-200" onClick={add}>
            Add correction
          </button>
        </div>
      </div>
      <p className="text-[10.5px] leading-relaxed text-mist-600">
        Corrections use source-frame percentages and source time. Locked region keeps the rectangle for the
        whole span; Track from region uses it as the starting rectangle and only allows a sustained, visible
        panel move. Both survive reload, preview and export.
      </p>
      {overrides.length === 0 ? (
        <p className="text-[10.5px] text-mist-600">
          Automatic webcam-panel tracking is used as-is. Add a correction when a game character is mistaken for
          the webcam, when the panel is missed, or when the panel genuinely moves.
        </p>
      ) : (
        <div className="space-y-2">
          {overrides.map((entry, index) => (
            <div key={`${entry.start_ms}-${index}`} className="space-y-1.5 rounded border border-white/8 p-2">
              <div className="grid grid-cols-2 gap-1.5">
                <label className="space-y-0.5 text-[10px] text-mist-500">
                  <span>Start (s)</span>
                  <input
                    type="number"
                    min={0}
                    step={0.1}
                    value={(entry.start_ms / 1000).toFixed(1)}
                    onChange={(event) => {
                      const start = Math.max(0, Math.round(Number(event.target.value) * 1000));
                      update(index, { start_ms: start, end_ms: Math.max(start + 1000, entry.end_ms) });
                    }}
                    className="field h-7 w-full text-[11px]"
                  />
                </label>
                <label className="space-y-0.5 text-[10px] text-mist-500">
                  <span>End (s)</span>
                  <input
                    type="number"
                    min={0.1}
                    step={0.1}
                    value={(entry.end_ms / 1000).toFixed(1)}
                    onChange={(event) => {
                      const end = Math.max(entry.start_ms + 1000, Math.round(Number(event.target.value) * 1000));
                      update(index, { end_ms: end });
                    }}
                    className="field h-7 w-full text-[11px]"
                  />
                </label>
              </div>
              <div className="grid grid-cols-4 gap-1.5">
                {(
                  [
                    ["X", "x_pct"],
                    ["Y", "y_pct"],
                    ["W", "width_pct"],
                    ["H", "height_pct"],
                  ] as const
                ).map(([label, key]) => (
                  <label key={key} className="space-y-0.5 text-[10px] text-mist-500">
                    <span>{label} %</span>
                    <input
                      type="number"
                      min={0}
                      max={100}
                      step={1}
                      value={Math.round(entry.rect[key] * 100)}
                      onChange={(event) => {
                        const raw = Math.min(100, Math.max(0, Number(event.target.value)));
                        updateRect(index, { [key]: raw / 100 });
                      }}
                      className="field h-7 w-full text-[11px]"
                    />
                  </label>
                ))}
              </div>
              <div className="grid grid-cols-2 gap-1.5">
                <label className="space-y-0.5 text-[10px] text-mist-500">
                  <span>Region mode</span>
                  <select
                    className="field h-7 w-full text-[11px]"
                    value={entry.mode}
                    onChange={(event) =>
                      update(index, { mode: event.target.value === "track" ? "track" : "lock" })
                    }
                  >
                    <option value="lock">Locked region</option>
                    <option value="track">Track from region</option>
                  </select>
                </label>
                <p className="self-end pb-1 text-[10px] leading-tight text-mist-600">
                  {entry.mode === "lock"
                    ? "Uses this rectangle for the whole span."
                    : "Seeds tracking; genuine panel moves may update it."}
                </p>
              </div>
              <div className="flex items-center justify-between gap-2">
                <input
                  type="text"
                  value={entry.reason}
                  maxLength={200}
                  onChange={(event) => update(index, { reason: event.target.value })}
                  className="field h-7 min-w-0 flex-1 text-[11px]"
                  aria-label="Correction reason"
                />
                <button
                  type="button"
                  className="text-[10.5px] text-signal-bad/90 hover:text-signal-bad"
                  onClick={() => onChange(overrides.filter((_, position) => position !== index))}
                >
                  Remove
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function CompositionPreview({
  projectId,
  candidateId,
  aspect,
  resolution,
  resolutionMode,
  customWidth,
  customHeight,
  mode,
  faceCamPosition,
  stackSplit,
  adaptiveStack,
  faceCamOverrides,
  cropX,
  cropY,
  enabled,
  onDetected,
}: {
  projectId: string;
  candidateId: string;
  aspect: AspectId;
  resolution: 720 | 1080 | 1440 | 2160;
  resolutionMode: "class" | "source" | "custom";
  customWidth?: number;
  customHeight?: number;
  mode: CompositionModeId;
  faceCamPosition: FaceCamPositionId;
  stackSplit: number;
  adaptiveStack: boolean;
  faceCamOverrides: FaceCamOverrideDraft[];
  cropX: number;
  cropY: number;
  enabled: boolean;
  onDetected: (value: DetectedFacecam | null) => void;
}) {
  const [preview, setPreview] = useState<CompositionPreviewResponse | null>(null);
  const [status, setStatus] = useState<"loading" | "refreshing" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setStatus((current) => (current === "ready" || current === "refreshing" ? "refreshing" : "loading"));
      setError(null);
      apiJson<CompositionPreviewResponse>(
        `/api/projects/${projectId}/renders/composition-preview`,
        {
          method: "POST",
          signal: controller.signal,
          body: {
            candidate_id: candidateId,
            aspect,
            resolution,
            resolution_mode: resolutionMode,
            custom_width: customWidth,
            custom_height: customHeight,
            composition: mode,
            face_cam_position: faceCamPosition,
            stack_split_pct: stackSplit,
            adaptive_stack: adaptiveStack,
            face_cam_overrides: faceCamOverrides,
            crop_x_pct: cropX,
            crop_y_pct: cropY,
            captions_enabled: false,
          },
        },
      )
        .then((value) => {
          if (cancelled) return;
          setPreview(value);
          setStatus("ready");
          const rect = value.facecam.rect;
          onDetected(
            rect &&
              typeof rect.x_pct === "number" &&
              typeof rect.y_pct === "number" &&
              typeof rect.width_pct === "number" &&
              typeof rect.height_pct === "number"
              ? {
                  rect: {
                    x_pct: rect.x_pct,
                    y_pct: rect.y_pct,
                    width_pct: rect.width_pct,
                    height_pct: rect.height_pct,
                  },
                  state: value.facecam.state ?? null,
                  confidence: value.facecam.confidence ?? null,
                  start_ms: value.segment.start_ms,
                  end_ms: value.segment.end_ms,
                }
              : null,
          );
        })
        .catch((caught: unknown) => {
          if (cancelled || controller.signal.aborted) return;
          setStatus("error");
          setError(caught instanceof Error ? caught.message : "The preview frame could not be rendered.");
        });
    }, 500);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [
    aspect,
    candidateId,
    cropX,
    cropY,
    enabled,
    faceCamPosition,
    adaptiveStack,
    faceCamOverrides,
    mode,
    onDetected,
    projectId,
    resolution,
    resolutionMode,
    customWidth,
    customHeight,
    stackSplit,
  ]);

  const ratio =
    preview !== null
      ? `${preview.width} / ${preview.height}`
      : aspect === "9:16"
        ? "9 / 16"
        : aspect === "1:1"
          ? "1 / 1"
          : "16 / 9";
  const band = preview?.decision?.caption_band ?? null;
  const requestedMode = mode;
  const renderedMode = preview?.decision?.mode ?? null;
  const busy = status === "loading" || status === "refreshing";

  return (
    <div className="space-y-1.5">
      <div
        className="relative overflow-hidden rounded-lg border border-white/10 bg-ink-950"
        style={{ aspectRatio: ratio }}
        aria-label={`Layout preview: ${requestedMode}`}
        aria-busy={busy}
      >
        {preview ? (
          // The frame comes from the same FFmpeg chain as the export, so it is
          // evidence of the composition rather than a diagram of the intent.
          <img
            src={preview.image}
            alt={`Rendered ${renderedMode ?? requestedMode} frame at ${formatTimecode(preview.segment.start_ms)}`}
            className="absolute inset-0 size-full object-contain"
          />
        ) : (
          <div className="absolute inset-0 bg-[linear-gradient(145deg,#171b22,#0b0d11)]" />
        )}

        {band ? (
          <div
            className="pointer-events-none absolute border-y border-dashed border-ember-500/50 bg-ember-500/[0.07]"
            style={{
              left: "7%",
              right: "7%",
              top: `${band.top_pct * 100}%`,
              height: `${Math.max(4, (band.bottom_pct - band.top_pct) * 100)}%`,
            }}
          >
            <span className="absolute inset-0 grid place-items-center text-[8px] uppercase tracking-[0.2em] text-ember-300/80">
              caption band
            </span>
          </div>
        ) : null}

        {busy ? (
          <div className="absolute inset-0 grid place-items-center bg-black/45 text-center">
            <span className="flex items-center gap-1.5 rounded bg-black/60 px-2 py-1 text-[10px] text-mist-300">
              <Loader2 className="size-3 animate-spin" strokeWidth={2} />
              {preview ? "Updating frame" : "Rendering a real frame"}
            </span>
          </div>
        ) : null}

        {preview ? (
          <span className="absolute left-1.5 top-1.5 rounded bg-black/60 px-1 py-0.5 text-[8.5px] uppercase tracking-wide text-mist-300">
            {renderedMode ?? requestedMode}
          </span>
        ) : null}
        {preview?.fallback ? (
          <span className="absolute right-1.5 top-1.5 rounded bg-ember-500/20 px-1 py-0.5 text-[8.5px] uppercase tracking-wide text-ember-200">
            fallback
          </span>
        ) : null}
      </div>

      <p
        aria-live="polite"
        className={cn(
          "text-[10px] leading-snug",
          status === "error" ? "text-signal-bad/90" : "text-mist-600",
        )}
      >
        {status === "error"
          ? error
          : preview
            ? `${preview.width}×${preview.height} · source ${formatTimecode(preview.segment.start_ms)}–${formatTimecode(preview.segment.end_ms)} · ${
                preview.facecam.detected
                  ? `webcam panel ${preview.facecam.state ?? "detected"}${
                      typeof preview.facecam.confidence === "number"
                        ? ` (${Math.round(preview.facecam.confidence * 100)}% confidence)`
                        : ""
                    }`
                  : "no webcam panel detected"
              }`
            : "Waiting for the project settings before rendering a frame."}
      </p>

      {preview?.facecam.detector_available === false ? (
        <p className="text-[10px] leading-snug text-ember-300/90">
          Webcam detection is unavailable in this worker. Set a manual region before exporting.
        </p>
      ) : null}

      {preview?.facecam.review_required ? (
        <p className="text-[10px] leading-snug text-ember-300/90">
          Review this interval: the webcam panel is uncertain
          {preview.facecam.reason ? ` — ${preview.facecam.reason}` : ""}. Lock a region in the correction
          editor before exporting.
        </p>
      ) : null}

      {preview?.facecam.ranges?.length ? (
        <p className="text-[10px] leading-snug text-mist-600">
          {preview.facecam.ranges.length} tracked range
          {preview.facecam.ranges.length === 1 ? "" : "s"}:{" "}
          {preview.facecam.ranges
            .slice(0, 3)
            .map(
              (range) =>
                `${formatTimecode(range.start_ms)}–${formatTimecode(range.end_ms)} ${range.state}`,
            )
            .join(" · ")}
          {preview.facecam.ranges.length > 3 ? " · …" : ""}
        </p>
      ) : null}

      {preview?.fallback ? (
        <p className="text-[10px] leading-snug text-ember-300/90">
          {requestedMode} could not be drawn for this source
          {renderedMode ? `, so the encoder fell back to ${renderedMode}` : ""}.
          {preview.warnings[0] ? ` ${preview.warnings[0]}` : ""}
        </p>
      ) : null}
    </div>
  );
}

function RenderRow({ render }: { render: Render }) {
  const [open, setOpen] = useState(false);
  const review = render.review;
  const tone =
    render.status === "completed"
      ? "good"
      : render.status === "failed"
        ? "bad"
        : render.status === "cancelled"
          ? "neutral"
          : "ember";
  const gateStatus = review?.status;
  const reviewTone = gateStatus === "fail" ? "bad" : gateStatus === "warn" ? "ember" : "good";

  return (
    <li className="rounded-md border border-white/8 bg-black/20 px-2.5 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={tone} className="h-5 px-2 text-[10.5px]">
          {render.status}
        </Badge>
        {gateStatus ? (
          <Badge tone={reviewTone} className="h-5 px-2 text-[10.5px]">
            gate {gateStatus}
          </Badge>
        ) : null}
        <span className="numeric text-[11.5px] text-mist-400">
          {render.width && render.height ? `${render.width}×${render.height}` : "—"}
          {render.duration_ms ? ` · ${formatTimecode(render.duration_ms)}` : ""}
          {render.size_bytes ? ` · ${formatBytes(render.size_bytes)}` : ""}
        </span>
        {render.status === "failed" && render.error_message ? (
          <span className="w-full text-[11.5px] text-signal-bad/90">{render.error_message}</span>
        ) : null}
        <div className="ml-auto flex items-center gap-1.5">
          {review?.checks?.length ? (
            <button
              type="button"
              className="btn btn-ghost btn-sm h-6 px-1.5 text-[11px]"
              onClick={() => setOpen((value) => !value)}
              aria-expanded={open}
            >
              Review
              <ChevronDown className={cn("size-3 transition-transform", open && "rotate-180")} strokeWidth={2} />
            </button>
          ) : null}
          {render.status === "completed" ? (
            <a className="btn btn-sm h-6 px-2 text-[11px]" href={`/api/renders/${render.id}/download`}>
              <Download className="size-3" strokeWidth={2} />
              MP4
            </a>
          ) : null}
          {review?.draft_relative_path ? (
            <a className="btn btn-ghost btn-sm h-6 px-2 text-[11px]" href={`/api/renders/${render.id}/draft`}>
              <Download className="size-3" strokeWidth={2} />
              Draft
            </a>
          ) : null}
        </div>
      </div>

      {open && review ? (
        <div className="mt-2 space-y-2 border-t border-white/6 pt-2">
          <p className="text-[11.5px] text-mist-300">{review.summary}</p>
          <ul className="space-y-1">
            {review.checks.map((check) => (
              <li key={check.id} className="flex gap-2 text-[11.5px] leading-snug">
                <Badge
                  tone={check.status === "fail" ? "bad" : check.status === "warn" ? "ember" : "neutral"}
                  className="mt-0.5 h-4 shrink-0 px-1.5 text-[9.5px]"
                >
                  {check.status}
                </Badge>
                <span className="text-mist-400">
                  <span className="text-mist-200">{check.label}:</span> {check.detail}
                </span>
              </li>
            ))}
          </ul>
          <EditDecisionList edl={review.edl} />
          <CaptionMeasurements review={review} />
        </div>
      ) : null}
    </li>
  );
}

function EditDecisionList({ edl }: { edl?: Record<string, unknown> }) {
  const segments = Array.isArray(edl?.segments) ? (edl.segments as Array<Record<string, unknown>>) : [];
  if (segments.length === 0) return null;
  return (
    <div className="space-y-1">
      <p className="label">Source → output</p>
      <div className="max-h-40 overflow-y-auto rounded border border-white/6">
        <table className="w-full border-collapse text-[10.5px]">
          <thead className="sticky top-0 bg-ink-900 text-mist-500">
            <tr>
              <th className="px-1.5 py-1 text-left font-medium">#</th>
              <th className="px-1.5 py-1 text-left font-medium">Source</th>
              <th className="px-1.5 py-1 text-left font-medium">Output</th>
              <th className="px-1.5 py-1 text-left font-medium">Layout</th>
            </tr>
          </thead>
          <tbody className="numeric text-mist-400">
            {segments.map((segment, index) => {
              const composition = segment.composition as Record<string, unknown> | null | undefined;
              return (
                <tr key={index} className="border-t border-white/5">
                  <td className="px-1.5 py-0.5">{index + 1}</td>
                  <td className="px-1.5 py-0.5">
                    {formatTimecode(Number(segment.source_start_ms ?? 0))} –{" "}
                    {formatTimecode(Number(segment.source_end_ms ?? 0))}
                  </td>
                  <td className="px-1.5 py-0.5">
                    {formatTimecode(Number(segment.output_start_ms ?? 0))} –{" "}
                    {formatTimecode(Number(segment.output_end_ms ?? 0))}
                  </td>
                  <td className="px-1.5 py-0.5">{String(composition?.mode ?? "legacy")}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CaptionMeasurements({ review }: { review: NonNullable<Render["review"]> }) {
  const check = review.checks.find((entry) => entry.id === "captions");
  const report = (check as Record<string, unknown> | undefined)?.report as Record<string, unknown> | undefined;
  if (!report || !report.used) return null;
  const width = Number(report.max_line_width_px ?? 0);
  const limit = Number(report.max_line_width_limit_px ?? 0);
  return (
    <p className="numeric text-[10.5px] text-mist-500">
      Captions: widest measured line {width.toFixed(0)}px
      {limit ? ` of ${limit.toFixed(0)}px safe width` : ""} · {String(report.chunks ?? 0)} chunks ·{" "}
      {report.bounds_ok ? "inside the safe band" : "outside the safe band"}
    </p>
  );
}
