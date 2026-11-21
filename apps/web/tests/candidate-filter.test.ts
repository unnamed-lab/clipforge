import { describe, expect, it } from "vitest";

import type { Candidate } from "@clipforge/shared";

import {
  EMPTY_FILTERS,
  activeFilterCount,
  candidateFacets,
  candidateType,
  filterCandidates,
  sortCandidates,
} from "../src/app/projects/[id]/candidate-filter";

function candidate(overrides: Partial<Candidate> = {}): Candidate {
  const base = {
    id: "candidate_1",
    project_id: "project_1",
    analysis_run_id: "run_1",
    rank: 1,
    start_ms: 0,
    end_ms: 30_000,
    duration_ms: 30_000,
    title: "The hiring mistake",
    hook: "Most founders hire too early.",
    reason: "A complete story with a clear lesson.",
    category: "moment",
    excerpt: "We spent six months looking for a head of sales.",
    scores: {
      overall: 80,
      components: {},
      weights: {},
      method: "baseline",
      method_version: "baseline-1",
      estimated: false,
    },
    overall: 80,
    warnings: [],
    method: "baseline",
    method_version: "1",
    decision: "proposed" as const,
    user_start_ms: null,
    user_end_ms: null,
    edited: false,
    moment_type: "best_advice",
    speaker: "SPEAKER_00",
    batch_kind: "initial",
    hook_candidates: [],
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
  return { ...base, hook_candidates: overrides.hook_candidates ?? [] };
}

const candidates = [
  candidate(),
  candidate({
    id: "candidate_2",
    rank: 2,
    start_ms: 45_000,
    end_ms: 75_000,
    duration_ms: 30_000,
    title: "A funny reaction",
    hook: "Wait, that actually worked?",
    excerpt: "Everyone laughed when the demo broke.",
    overall: 92,
    moment_type: "funny",
    speaker: null,
  }),
  candidate({
    id: "candidate_3",
    rank: 3,
    start_ms: 90_000,
    end_ms: 105_000,
    duration_ms: 15_000,
    title: "The short answer",
    excerpt: "Talk to customers before you hire.",
    overall: 70,
    moment_type: "",
    category: "insight",
    speaker: "SPEAKER_01",
    decision: "accepted",
  }),
];

describe("candidate local filters", () => {
  it("matches the query across title, hook, excerpt and topic without changing the source order", () => {
    // when
    const result = filterCandidates(candidates, { ...EMPTY_FILTERS, query: "DEMO" });

    // then
    expect(result.map((item) => item.id)).toEqual(["candidate_2"]);
  });

  it("filters by moment type and falls back to the category when no type exists", () => {
    // when
    const typed = filterCandidates(candidates, { ...EMPTY_FILTERS, momentType: "funny" });
    const fallback = filterCandidates(candidates, { ...EMPTY_FILTERS, momentType: "insight" });

    // then
    expect(typed.map((item) => item.id)).toEqual(["candidate_2"]);
    expect(fallback.map((item) => item.id)).toEqual(["candidate_3"]);
    expect(candidateType(candidates[2]!)).toBe("insight");
  });

  it("filters by decision and speaker", () => {
    // when
    const accepted = filterCandidates(candidates, { ...EMPTY_FILTERS, decision: "accepted" });
    const speaker = filterCandidates(candidates, { ...EMPTY_FILTERS, speaker: "SPEAKER_00" });

    // then
    expect(accepted.map((item) => item.id)).toEqual(["candidate_3"]);
    expect(speaker.map((item) => item.id)).toEqual(["candidate_1"]);
  });

  it("filters by inclusive duration bounds", () => {
    // when
    const result = filterCandidates(candidates, {
      ...EMPTY_FILTERS,
      minDurationMs: 15_000,
      maxDurationMs: 15_000,
    });

    // then
    expect(result.map((item) => item.id)).toEqual(["candidate_3"]);
  });

  it("keeps candidates whose source interval overlaps the requested range", () => {
    // when
    const result = filterCandidates(candidates, {
      ...EMPTY_FILTERS,
      fromMs: 20_000,
      toMs: 50_000,
    });

    // then
    expect(result.map((item) => item.id)).toEqual(["candidate_1", "candidate_2"]);
  });
});

describe("candidate local sorting", () => {
  it("sorts by quality with rank as the tie-breaker", () => {
    // when
    const result = sortCandidates(candidates, "quality");

    // then
    expect(result.map((item) => item.id)).toEqual(["candidate_2", "candidate_1", "candidate_3"]);
  });

  it("sorts chronologically by source start", () => {
    // when
    const result = sortCandidates(candidates, "chronological");

    // then
    expect(result.map((item) => item.start_ms)).toEqual([0, 45_000, 90_000]);
  });

  it("sorts by duration without mutating the input", () => {
    // when
    const result = sortCandidates(candidates, "duration");

    // then
    expect(result.map((item) => item.id)).toEqual(["candidate_1", "candidate_2", "candidate_3"]);
    expect(candidates.map((item) => item.id)).toEqual(["candidate_1", "candidate_2", "candidate_3"]);
  });
});

describe("candidate facets and active filter count", () => {
  it("lists unique sorted types and non-empty speakers", () => {
    // when
    const facets = candidateFacets(candidates);

    // then
    expect(facets.types).toEqual(["best_advice", "funny", "insight"]);
    expect(facets.speakers).toEqual(["SPEAKER_00", "SPEAKER_01"]);
  });

  it("counts every active filter and ignores blank values", () => {
    // given
    const filters = {
      ...EMPTY_FILTERS,
      query: "  ",
      momentType: "funny",
      speaker: "SPEAKER_00",
      minDurationMs: 10_000,
      fromMs: 0,
    };

    // then
    expect(activeFilterCount(filters)).toBe(4);
    expect(activeFilterCount(EMPTY_FILTERS)).toBe(0);
  });
});
