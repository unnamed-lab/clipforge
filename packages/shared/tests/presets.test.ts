import { describe, expect, it } from "vitest";

import {
  CAPTION_ALIGNMENTS,
  CAPTION_FONTS,
  CAPTION_POSITIONS,
  COMPOSITION_MODES,
  DEFAULT_CAPTION_ALIGNMENT,
  DEFAULT_CAPTION_POSITION,
  DEFAULT_CAPTION_PRESET,
} from "../src/presets";
import { candidateSchema, renderRequestSchema } from "../src/schemas";

describe("composition modes", () => {
  it("exposes auto and every manual mode the renderer understands", () => {
    expect(COMPOSITION_MODES.map((mode) => mode.id)).toEqual([
      "auto",
      "crop",
      "fit",
      "pip",
      "split",
      "manual",
    ]);
  });
});

describe("caption defaults", () => {
  it("defaults to Clean podcast at the bottom, centred", () => {
    expect(DEFAULT_CAPTION_PRESET).toBe("clean");
    expect(DEFAULT_CAPTION_POSITION).toBe("bottom");
    expect(DEFAULT_CAPTION_ALIGNMENT).toBe("center");
    expect(CAPTION_POSITIONS.map((position) => position.id)).toEqual(["bottom", "center", "top"]);
    expect(CAPTION_ALIGNMENTS.map((alignment) => alignment.id)).toEqual(["left", "center", "right"]);
  });
});

describe("caption fonts", () => {
  it("only advertises bundled, redistributable faces", () => {
    expect(CAPTION_FONTS.map((font) => font.id)).toEqual([
      "Montserrat",
      "Inter",
      "Archivo Black",
      "Bebas Neue",
      "Lato",
      "Anton",
    ]);
  });
});

describe("render requests", () => {
  it("defaults composition to auto and carries crop focus", () => {
    const parsed = renderRequestSchema.parse({
      aspect: "9:16",
      resolution: 1080,
      framing: "crop",
      caption_preset: "clean",
      captions_enabled: true,
      crop_x_pct: 22,
    });
    expect(parsed.composition).toBe("auto");
    expect(parsed.crop_x_pct).toBe(22);
  });

  it("rejects a composition mode the worker cannot honour", () => {
    expect(() =>
      renderRequestSchema.parse({
        aspect: "9:16",
        resolution: 1080,
        framing: "crop",
        composition: "magic",
        caption_preset: "clean",
        captions_enabled: true,
      }),
    ).toThrow();
  });
});

describe("candidate boundaries", () => {
  it("keeps a legacy candidate readable without ending evidence", () => {
    const parsed = candidateSchema.parse({
      id: "can_1",
      project_id: "prj_1",
      analysis_run_id: "run_1",
      rank: 0,
      start_ms: 0,
      end_ms: 40_000,
      duration_ms: 40_000,
      title: "Clip",
      hook: "Hook",
      reason: "Reason",
      category: "insight",
      excerpt: "Excerpt",
      scores: { overall: 70, components: {} },
      overall: 70,
      warnings: [],
      method: "baseline",
      method_version: "baseline-1",
      decision: "proposed",
      user_start_ms: null,
      user_end_ms: null,
      edited: false,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    });
    expect(parsed.boundary).toBeUndefined();
  });

  it("parses evidence and alternatives without narrowing passthrough data", () => {
    const parsed = candidateSchema.parse({
      id: "can_1",
      project_id: "prj_1",
      analysis_run_id: "run_1",
      rank: 0,
      start_ms: 0,
      end_ms: 40_000,
      duration_ms: 40_000,
      title: "Clip",
      hook: "Hook",
      reason: "Reason",
      category: "insight",
      excerpt: "Excerpt",
      scores: { overall: 70, components: {} },
      overall: 70,
      warnings: [],
      method: "baseline",
      method_version: "baseline-1",
      decision: "proposed",
      user_start_ms: null,
      user_end_ms: null,
      edited: false,
      boundary: {
        suspicious: true,
        reviewed: "pending",
        evidence: ["Speech continues 0.5s after the end."],
        alternatives: [
          { end_ms: 39_000, label: "End on the cut", reason: "Cut", confidence: 0.72, source: "scene" },
        ],
        nearest_cut_before_ms: 39_000,
      },
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    });
    expect(parsed.boundary?.suspicious).toBe(true);
    expect(parsed.boundary?.alternatives[0].end_ms).toBe(39_000);
    expect(parsed.boundary?.nearest_cut_before_ms).toBe(39_000);
  });
});
