import type { EnhancementProfile, Source } from "@clipforge/shared";

export type TargetMode = "source" | "720" | "1080" | "1440" | "2160" | "custom";

export const TARGET_OPTIONS: ReadonlyArray<{ id: TargetMode; label: string; hint: string }> = [
  { id: "source", label: "Preserve source", hint: "Keeps the source's short side" },
  { id: "720", label: "720p", hint: "1280×720 or 720×1280" },
  { id: "1080", label: "1080p", hint: "1920×1080 or 1080×1920" },
  { id: "1440", label: "1440p", hint: "Explicit 2K export" },
  { id: "2160", label: "4K", hint: "Explicit 4K export" },
  { id: "custom", label: "Custom", hint: "Exact even dimensions" },
];

export const QUALITY_PROFILE_FALLBACK: Record<
  EnhancementProfile,
  { label: string; summary: string; engine: string; denoise: number; restoration: number }
> = {
  standard: {
    label: "Standard",
    summary: "High-quality resizing only. No denoising and no AI model.",
    engine: "FFmpeg lanczos",
    denoise: 0,
    restoration: 1,
  },
  clean: {
    label: "Clean",
    summary: "Conservative denoising before a high-quality resize. Preserves natural texture.",
    engine: "FFmpeg hqdn3d + lanczos",
    denoise: 30,
    restoration: 1,
  },
  ai: {
    label: "AI Enhance",
    summary: "A tested super-resolution model at a conservative blend, then a high-quality resize.",
    engine: "ESPCN / Real-ESRGAN",
    denoise: 15,
    restoration: 0.9,
  },
  ai_plus: {
    label: "AI Enhance+",
    summary: "Full model strength with static-frame stabilisation to reduce flicker on HUD and text.",
    engine: "ESPCN / Real-ESRGAN",
    denoise: 30,
    restoration: 1,
  },
};

const ASPECT_RATIOS: Record<string, number> = {
  "9:16": 9 / 16,
  "16:9": 16 / 9,
  "1:1": 1,
};

function even(value: number): number {
  const rounded = Math.max(2, Math.round(value));
  return rounded + (rounded % 2);
}

export function aspectDimensions(aspect: string, resolution: 720 | 1080 | 1440 | 2160): {
  width: number;
  height: number;
} {
  if (aspect === "9:16") {
    return { width: resolution, height: even((resolution * 16) / 9) };
  }
  if (aspect === "16:9") {
    return { width: even((resolution * 16) / 9), height: resolution };
  }
  return { width: resolution, height: resolution };
}

export function preserveDimensions(
  sourceWidth: number,
  sourceHeight: number,
  aspect: string,
): { width: number; height: number } {
  const ratio = ASPECT_RATIOS[aspect] ?? 1;
  const short = Math.max(320, Math.min(2160, Math.min(sourceWidth, sourceHeight)));
  if (ratio >= 1) {
    return { width: even(short * ratio), height: even(short) };
  }
  return { width: even(short), height: even(short / ratio) };
}

export function resolutionClass(shortSide: number): 720 | 1080 | 1440 | 2160 {
  const value = Math.max(320, Math.min(2160, Math.round(shortSide)));
  if (value >= 2160) return 2160;
  if (value >= 1440) return 1440;
  if (value >= 1080) return 1080;
  return 720;
}

export function resolveTarget(input: {
  mode: TargetMode;
  aspect: string;
  classResolution: 720 | 1080 | 1440 | 2160;
  source: Pick<Source, "width" | "height"> | null;
  customWidth: number;
  customHeight: number;
}): {
  resolution: 720 | 1080 | 1440 | 2160;
  resolution_mode: "class" | "source" | "custom";
  custom_width?: number;
  custom_height?: number;
  width: number;
  height: number;
  valid: boolean;
  error?: string;
} {
  const { mode, aspect, classResolution, source, customWidth, customHeight } = input;
  if (mode === "source" && source?.width && source?.height) {
    const dims = preserveDimensions(source.width, source.height, aspect);
    return {
      resolution: resolutionClass(Math.min(source.width, source.height)),
      resolution_mode: "source",
      custom_width: dims.width,
      custom_height: dims.height,
      ...dims,
      valid: true,
    };
  }
  if (mode === "custom") {
    const width = even(customWidth);
    const height = even(customHeight);
    const target = ASPECT_RATIOS[aspect] ?? 1;
    const valid =
      width >= 320 &&
      height >= 320 &&
      width <= 3840 &&
      height <= 4320 &&
      Math.abs(width / height - target) <= target * 0.01;
    return {
      resolution: classResolution,
      resolution_mode: "custom",
      custom_width: width,
      custom_height: height,
      width,
      height,
      valid,
      error: valid ? undefined : `Custom dimensions must match the ${aspect} frame and be even.`,
    };
  }
  const resolution = mode === "source" ? classResolution : (Number(mode) as 720 | 1080 | 1440 | 2160);
  return {
    resolution,
    resolution_mode: "class",
    ...aspectDimensions(aspect, resolution),
    valid: true,
  };
}

export function estimatedEnhancementSeconds(input: {
  profile: EnhancementProfile;
  benchmarkSecondsPerMegapixel: number | null | undefined;
  sourceWidth: number;
  sourceHeight: number;
  durationMs: number;
  fps: number | null | undefined;
  maxFps?: number | null;
}): number | null {
  if (input.profile !== "ai" && input.profile !== "ai_plus") return 0;
  const perMegapixel = input.benchmarkSecondsPerMegapixel;
  if (!perMegapixel || perMegapixel <= 0) return null;
  const fps = Math.max(1, Math.min(Number(input.maxFps || input.fps || 30), 60));
  const frames = Math.max(1, Math.round((input.durationMs / 1000) * fps));
  const megapixels = (input.sourceWidth * input.sourceHeight) / 1_000_000;
  // Measured model time plus the streaming decode/encode and blend overhead.
  return Math.round(frames * megapixels * perMegapixel * 1.25);
}

export function formatSeconds(value: number | null): string {
  if (value === null) return "not measured";
  if (value < 60) return `${Math.round(value)} s`;
  if (value < 3600) return `${Math.round(value / 60)} min`;
  const hours = Math.floor(value / 3600);
  const minutes = Math.round((value % 3600) / 60);
  return `${hours} h ${minutes} min`;
}
