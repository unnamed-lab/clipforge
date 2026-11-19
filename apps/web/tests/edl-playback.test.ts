import { describe, expect, it } from "vitest";

import type { Candidate } from "@clipforge/shared";
import { candidateSchema } from "@clipforge/shared";

import {
  entryAtOutput,
  outputToSource,
  resolvePreviewEdl,
  sourceToOutput,
} from "../src/lib/edl-playback";

const BASE = {
  id: "can_1",
  project_id: "prj_1",
  analysis_run_id: "run_1",
  rank: 0,
  start_ms: 10_000,
  end_ms: 40_000,
  duration_ms: 30_000,
  title: "Clip",
  hook: "",
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
} as const;

function candidate(overrides: Record<string, unknown> = {}): Candidate {
  return candidateSchema.parse({ ...BASE, ...overrides });
}

function timeline(entries: Array<{ role: string; start: number; end: number }>) {
  const rows = entries.map((entry, index) => ({
    index,
    role: entry.role,
    source_start_ms: entry.start,
    source_end_ms: entry.end,
    output_start_ms: 0,
    output_end_ms: 0,
    transition: "cut",
    key_point: "",
    reason: "",
  }));
  return {
    version: 1,
    entries: rows,
    total_output_ms: 0,
    covered_ms: 0,
    missing_ms: 0,
    duplicated_ms: 0,
    loop_ms: 0,
    gaps: [],
    overlaps: [],
    valid: true,
    warnings: [],
  };
}

const reorderedTimeline = timeline([
  { role: "payoff", start: 35_000, end: 40_000 },
  { role: "main", start: 10_000, end: 20_000 },
]);

const reorderedDecision = {
  strategy: "result_first",
  reordered: true,
  review_required: true,
  timeline: reorderedTimeline,
};

describe("resolvePreviewEdl", () => {
  it("plays prepared source order when the clip has no timeline", () => {
    const edl = resolvePreviewEdl(candidate());

    expect(edl.basis).toBe("timeline_unavailable");
    expect(edl.entries).toHaveLength(1);
    expect(edl.entries[0]).toMatchObject({ source_start_ms: 10_000, source_end_ms: 40_000 });
    expect(edl.total_output_ms).toBe(30_000);
  });

  it("uses saved trim order for an edited clip", () => {
    const edl = resolvePreviewEdl(candidate({ edited: true, start_ms: 12_000, end_ms: 30_000 }));

    expect(edl.basis).toBe("manual_trim");
    expect(edl.entries[0]).toMatchObject({ source_start_ms: 12_000, source_end_ms: 30_000 });
  });

  it("uses unsaved boundaries as a manual trim so a stale EDL is never previewed", () => {
    const edl = resolvePreviewEdl(candidate(), {
      pendingTrim: { start_ms: 11_000, end_ms: 22_000 },
    });

    expect(edl.basis).toBe("manual_trim");
    expect(edl.entries[0]).toMatchObject({ source_start_ms: 11_000, source_end_ms: 22_000 });
  });

  it("plays a validated reordered timeline", () => {
    const edl = resolvePreviewEdl(candidate({ editorial: reorderedDecision }), {
      approveEditorial: true,
    });

    expect(edl.basis).toBe("timeline");
    expect(edl.entries.map((entry) => entry.role)).toEqual(["payoff", "main"]);
    expect(edl.entries.map((entry) => entry.output_start_ms)).toEqual([0, 5_000]);
    expect(edl.reordered).toBe(true);
  });

  it("falls back to source order until a reordered plan is approved", () => {
    const edl = resolvePreviewEdl(candidate({ editorial: reorderedDecision }));

    expect(edl.basis).toBe("review_not_approved");
    expect(edl.entries).toHaveLength(1);
    expect(edl.entries[0]!.source_start_ms).toBe(10_000);
  });

  it("uses the reordered timeline once approved", () => {
    const edl = resolvePreviewEdl(candidate({ editorial: reorderedDecision }), {
      approveEditorial: true,
    });

    expect(edl.basis).toBe("timeline");
    expect(edl.entries.map((entry) => entry.role)).toEqual(["payoff", "main"]);
  });

  it("refuses a timeline span outside the source", () => {
    const editorial = {
      ...reorderedDecision,
      review_required: false,
      timeline: timeline([{ role: "main", start: 41_000, end: 45_000 }]),
    };
    const edl = resolvePreviewEdl(candidate({ editorial }), { sourceDurationMs: 40_000 });

    expect(edl.basis).toBe("invalid_timeline_span");
    expect(edl.entries[0]!.source_start_ms).toBe(10_000);
  });

  it("drops a hook entry when hooks are excluded and keeps output times contiguous", () => {
    const editorial = {
      strategy: "cold_open",
      reordered: true,
      review_required: true,
      timeline: timeline([
        { role: "hook", start: 20_000, end: 23_000 },
        { role: "main", start: 10_000, end: 40_000 },
      ]),
    };
    const edl = resolvePreviewEdl(candidate({ editorial }), {
      includeHook: false,
      approveEditorial: true,
    });

    expect(edl.entries).toHaveLength(1);
    expect(edl.entries[0]).toMatchObject({
      role: "main",
      output_start_ms: 0,
      output_end_ms: 30_000,
    });
  });

  it("keeps a deliberate loop entry", () => {
    const editorial = {
      strategy: "loop",
      reordered: true,
      review_required: true,
      timeline: {
        ...timeline([
          { role: "payoff", start: 30_000, end: 40_000 },
          { role: "main", start: 10_000, end: 25_000 },
          { role: "loop", start: 30_000, end: 34_000 },
        ]),
        loop_ms: 4_000,
      },
    };
    const edl = resolvePreviewEdl(candidate({ editorial }), { approveEditorial: true });

    expect(edl.entries.map((entry) => entry.role)).toEqual(["payoff", "main", "loop"]);
    expect(edl.entries[2]!.source_start_ms).toBe(30_000);
    expect(edl.total_output_ms).toBe(29_000);
  });

  it("prepends an approved earlier hook to the chronological fallback", () => {
    const edl = resolvePreviewEdl(
      candidate({
        hook_detail: {
          text: "Earlier setup line",
          start_ms: 5_000,
          end_ms: 8_000,
          source: "external",
          placement: "before",
          direction: "before",
          approved: true,
        },
      }),
    );

    expect(edl.basis).toBe("timeline_unavailable");
    expect(edl.entries.map((entry) => entry.role)).toEqual(["hook", "main"]);
    expect(edl.entries[1]!.output_start_ms).toBe(3_000);
  });

  it("does not prepend a later line to the chronological fallback", () => {
    const edl = resolvePreviewEdl(
      candidate({
        hook_detail: {
          text: "Later payoff",
          start_ms: 34_000,
          end_ms: 37_000,
          source: "external",
          placement: "none",
          direction: "after",
          approved: true,
        },
      }),
    );

    expect(edl.entries.map((entry) => entry.role)).toEqual(["main"]);
  });

  it("does not prepend a hook when the trim moved before it", () => {
    const edl = resolvePreviewEdl(
      candidate({
        hook_detail: {
          text: "Earlier setup line",
          start_ms: 5_000,
          end_ms: 9_500,
          source: "external",
          placement: "before",
          direction: "before",
          approved: true,
        },
      }),
      { pendingTrim: { start_ms: 9_000, end_ms: 20_000 } },
    );

    expect(edl.entries.map((entry) => entry.role)).toEqual(["main"]);
  });
});

describe("edl time mapping", () => {
  const edl = resolvePreviewEdl(
    candidate({
      editorial: {
        strategy: "result_first",
        reordered: true,
        review_required: true,
        timeline: reorderedTimeline,
      },
    }),
    { approveEditorial: true },
  );

  it("maps output time through each join into source time", () => {
    expect(outputToSource(edl.entries, 0)).toBe(35_000);
    expect(outputToSource(edl.entries, 4_999)).toBe(39_999);
    expect(outputToSource(edl.entries, 5_000)).toBe(10_000);
    expect(outputToSource(edl.entries, 19_000)).toBe(20_000);
  });

  it("clamps output time past the end to the last source frame", () => {
    expect(outputToSource(edl.entries, 99_000)).toBe(20_000);
  });

  it("maps source time back to its first output position", () => {
    expect(sourceToOutput(edl.entries, 35_000)).toBe(0);
    expect(sourceToOutput(edl.entries, 10_000)).toBe(5_000);
    expect(sourceToOutput(edl.entries, 25_000)).toBeNull();
  });

  it("reports the active entry and its source playhead", () => {
    const hit = entryAtOutput(edl.entries, 7_500);
    expect(hit?.index).toBe(1);
    expect(hit?.entry.role).toBe("main");
    expect(hit?.source_ms).toBe(12_500);
  });
});
