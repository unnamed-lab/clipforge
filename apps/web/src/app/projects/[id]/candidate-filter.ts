import type { Candidate } from "@clipforge/shared";

export type CandidateSort = "quality" | "chronological" | "duration";

export type CandidateFilters = {
  query: string;
  momentType: string;
  decision: "all" | Candidate["decision"];
  speaker: string;
  minDurationMs: number | null;
  maxDurationMs: number | null;
  fromMs: number | null;
  toMs: number | null;
};

export const EMPTY_FILTERS: CandidateFilters = {
  query: "",
  momentType: "",
  decision: "all",
  speaker: "",
  minDurationMs: null,
  maxDurationMs: null,
  fromMs: null,
  toMs: null,
};

export function candidateType(candidate: Candidate): string {
  return candidate.moment_type || candidate.category || "other";
}

function haystack(candidate: Candidate): string {
  return [
    candidate.title,
    candidate.hook,
    candidate.excerpt,
    candidate.reason,
    candidate.summary,
    candidate.category,
    candidate.moment_type,
    candidate.speaker,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

export function filterCandidates(candidates: Candidate[], filters: CandidateFilters): Candidate[] {
  const query = filters.query.trim().toLowerCase();
  return candidates.filter((candidate) => {
    if (query && !haystack(candidate).includes(query)) return false;
    if (filters.momentType && candidateType(candidate) !== filters.momentType) return false;
    if (filters.decision !== "all" && candidate.decision !== filters.decision) return false;
    if (filters.speaker && candidate.speaker !== filters.speaker) return false;
    if (filters.minDurationMs !== null && candidate.duration_ms < filters.minDurationMs) return false;
    if (filters.maxDurationMs !== null && candidate.duration_ms > filters.maxDurationMs) return false;
    if (filters.fromMs !== null && candidate.end_ms <= filters.fromMs) return false;
    if (filters.toMs !== null && candidate.start_ms >= filters.toMs) return false;
    return true;
  });
}

export function sortCandidates(candidates: Candidate[], sort: CandidateSort): Candidate[] {
  const copy = [...candidates];
  if (sort === "chronological") {
    copy.sort((a, b) => a.start_ms - b.start_ms || a.rank - b.rank);
  } else if (sort === "duration") {
    copy.sort((a, b) => b.duration_ms - a.duration_ms || a.rank - b.rank);
  } else {
    copy.sort((a, b) => b.overall - a.overall || a.rank - b.rank);
  }
  return copy;
}

export function candidateFacets(candidates: Candidate[]): {
  types: string[];
  speakers: string[];
} {
  return {
    types: [...new Set(candidates.map(candidateType))].sort(),
    speakers: [...new Set(candidates.map((candidate) => candidate.speaker).filter(Boolean) as string[])].sort(),
  };
}

export function activeFilterCount(filters: CandidateFilters): number {
  let count = 0;
  if (filters.query.trim()) count += 1;
  if (filters.momentType) count += 1;
  if (filters.decision !== "all") count += 1;
  if (filters.speaker) count += 1;
  if (filters.minDurationMs !== null) count += 1;
  if (filters.maxDurationMs !== null) count += 1;
  if (filters.fromMs !== null) count += 1;
  if (filters.toMs !== null) count += 1;
  return count;
}
