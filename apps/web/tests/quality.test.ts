import { describe, expect, it } from "vitest";

import {
  aspectDimensions,
  estimatedEnhancementSeconds,
  preserveDimensions,
  resolutionClass,
  resolveTarget,
} from "../src/lib/quality";

describe("quality helpers", () => {
  it("maps a 720p source to 1080p landscape without changing the aspect", () => {
    const target = resolveTarget({
      mode: "1080",
      aspect: "16:9",
      classResolution: 1080,
      source: { width: 1280, height: 720 },
      customWidth: 1280,
      customHeight: 720,
    });

    expect(target).toMatchObject({
      resolution: 1080,
      resolution_mode: "class",
      width: 1920,
      height: 1080,
      valid: true,
    });
  });

  it("maps a portrait 720p source to 1080x1920", () => {
    const target = resolveTarget({
      mode: "1080",
      aspect: "9:16",
      classResolution: 1080,
      source: { width: 720, height: 1280 },
      customWidth: 720,
      customHeight: 1280,
    });

    expect([target.width, target.height]).toEqual([1080, 1920]);
  });

  it("preserves the source short side instead of stretching", () => {
    expect(preserveDimensions(1280, 720, "16:9")).toEqual({ width: 1280, height: 720 });
    expect(preserveDimensions(1280, 720, "9:16")).toEqual({ width: 720, height: 1280 });
    expect(preserveDimensions(1280, 720, "1:1")).toEqual({ width: 720, height: 720 });
  });

  it("rejects a custom size whose aspect does not match the frame", () => {
    const target = resolveTarget({
      mode: "custom",
      aspect: "9:16",
      classResolution: 1080,
      source: { width: 1920, height: 1080 },
      customWidth: 1920,
      customHeight: 1080,
    });

    expect(target.valid).toBe(false);
    expect(target.error).toContain("9:16");
  });

  it("rounds custom dimensions to even values", () => {
    const target = resolveTarget({
      mode: "custom",
      aspect: "16:9",
      classResolution: 1080,
      source: { width: 1280, height: 720 },
      customWidth: 1921,
      customHeight: 1081,
    });

    expect(target.width % 2).toBe(0);
    expect(target.height % 2).toBe(0);
  });

  it("classifies source short sides without exceeding the source", () => {
    expect(resolutionClass(720)).toBe(720);
    expect(resolutionClass(900)).toBe(720);
    expect(resolutionClass(1080)).toBe(1080);
    expect(resolutionClass(1600)).toBe(1440);
    expect(resolutionClass(2160)).toBe(2160);
  });

  it("estimates AI time from measured throughput, not a timer", () => {
    const seconds = estimatedEnhancementSeconds({
      profile: "ai",
      benchmarkSecondsPerMegapixel: 1.19,
      sourceWidth: 1280,
      sourceHeight: 720,
      durationMs: 10_000,
      fps: 30,
    });

    // 300 frames x 0.9216 MP x 1.19 s/MP x 1.25 overhead
    expect(seconds).toBe(411);
  });

  it("estimates Fast AI with less pipeline overhead than full AI", () => {
    const fast = estimatedEnhancementSeconds({
      profile: "ai_fast",
      benchmarkSecondsPerMegapixel: 1.19,
      sourceWidth: 1280,
      sourceHeight: 720,
      durationMs: 10_000,
      fps: 30,
    });
    const full = estimatedEnhancementSeconds({
      profile: "ai",
      benchmarkSecondsPerMegapixel: 1.19,
      sourceWidth: 1280,
      sourceHeight: 720,
      durationMs: 10_000,
      fps: 30,
    });

    if (fast === null || full === null) throw new Error("expected measured estimates");
    expect(fast).toBeLessThan(full);
  });

  it("returns no AI estimate without a measurement", () => {
    expect(
      estimatedEnhancementSeconds({
        profile: "ai",
        benchmarkSecondsPerMegapixel: null,
        sourceWidth: 1280,
        sourceHeight: 720,
        durationMs: 10_000,
        fps: 30,
      }),
    ).toBeNull();
  });

  it("does not estimate an AI pass for Standard", () => {
    expect(
      estimatedEnhancementSeconds({
        profile: "standard",
        benchmarkSecondsPerMegapixel: 1.19,
        sourceWidth: 1280,
        sourceHeight: 720,
        durationMs: 10_000,
        fps: 30,
      }),
    ).toBe(0);
  });

  it("keeps known aspect dimensions even", () => {
    for (const aspect of ["9:16", "16:9", "1:1"]) {
      const dims = aspectDimensions(aspect, 1080);
      expect(dims.width % 2).toBe(0);
      expect(dims.height % 2).toBe(0);
    }
  });
});
