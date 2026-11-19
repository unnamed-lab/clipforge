/** Resolve the edit decision list a preview must play.
 *
 * The renderer already owns the canonical EDL resolution (``_segments_for`` in
 * the worker). This module mirrors those rules on the client so the preview
 * plays the same order the export will encode, including every honest fallback.
 * Source timestamps stay in ``source_*`` fields; output times are derived here.
 */

import type { Candidate, TimelineReport } from "@clipforge/shared";

export type EdlBasis =
  | "timeline"
  | "manual_trim"
  | "review_not_approved"
  | "timeline_unavailable"
  | "invalid_timeline_span"
  | "chronological";

export type EdlEntry = {
  index: number;
  role: string;
  source_start_ms: number;
  source_end_ms: number;
  output_start_ms: number;
  output_end_ms: number;
  transition: string;
  key_point: string;
};

export type ResolvedEdl = {
  entries: EdlEntry[];
  basis: EdlBasis;
  note: string;
  total_output_ms: number;
  reordered: boolean;
  review_required: boolean;
  strategy: string;
  warnings: string[];
};

export type EdlOptions = {
  includeHook?: boolean;
  approveEditorial?: boolean;
  pendingTrim?: { start_ms: number; end_ms: number } | null;
  sourceDurationMs?: number;
};

type DraftEntry = Omit<EdlEntry, "index" | "output_start_ms" | "output_end_ms">;

const MIN_HOOK_MS = 500;

export function resolvePreviewEdl(candidate: Candidate, options: EdlOptions = {}): ResolvedEdl {
  const includeHook = options.includeHook ?? true;
  const approveEditorial = options.approveEditorial ?? false;
  const sourceDurationMs = options.sourceDurationMs;

  const pending = options.pendingTrim;
  const dirtyTrim = Boolean(
    pending && (pending.start_ms !== candidate.start_ms || pending.end_ms !== candidate.end_ms),
  );
  const trimStart = dirtyTrim ? pending!.start_ms : candidate.start_ms;
  const trimEnd = dirtyTrim ? pending!.end_ms : candidate.end_ms;

  const decision = candidate.editorial?.editorial;
  const timeline = decision?.timeline ?? candidate.editorial?.timeline ?? null;
  const reordered = Boolean(decision?.reordered ?? candidate.editorial?.reordered);
  const reviewRequired = Boolean(decision?.review_required ?? candidate.editorial?.review_required);
  const strategy = decision?.strategy ?? candidate.editorial?.strategy ?? "chronological";
  const warnings = [...(timeline?.warnings ?? [])];

  let basis: EdlBasis = "timeline";
  let drafts: DraftEntry[] = [];

  if (dirtyTrim || candidate.edited) {
    basis = "manual_trim";
  } else if (!timeline || timeline.valid === false || !timeline.entries.length) {
    basis = "timeline_unavailable";
  } else if (reordered && reviewRequired && !approveEditorial) {
    basis = "review_not_approved";
  }

  if (basis === "timeline" && timeline) {
    drafts = timelineEntries(timeline, includeHook);
    if (!drafts.length) {
      basis = "timeline_unavailable";
    } else if (!spansAreValid(drafts, sourceDurationMs)) {
      basis = "invalid_timeline_span";
      drafts = [];
    }
  }

  if (basis !== "timeline") {
    drafts = [{ role: "main", source_start_ms: trimStart, source_end_ms: trimEnd, transition: "cut", key_point: "" }];
    const hook = beforeHook(candidate, trimStart, includeHook);
    if (hook) drafts.unshift(hook);
  }

  const entries = withOutputTimes(drafts);
  const last = entries[entries.length - 1];
  return {
    entries,
    basis,
    note: basisNote(basis, strategy),
    total_output_ms: last ? last.output_end_ms : 0,
    reordered,
    review_required: reviewRequired,
    strategy,
    warnings,
  };
}

/** Convert an output-time playhead into the source time the video should seek to. */
export function outputToSource(entries: EdlEntry[], outputMs: number): number | null {
  const hit = entryAtOutput(entries, outputMs);
  return hit ? hit.source_ms : null;
}

/** Convert a source time into its first matching output position (loop-aware). */
export function sourceToOutput(entries: EdlEntry[], sourceMs: number): number | null {
  for (const entry of entries) {
    if (sourceMs >= entry.source_start_ms && sourceMs < entry.source_end_ms) {
      return entry.output_start_ms + (sourceMs - entry.source_start_ms);
    }
  }
  return null;
}

export function entryAtOutput(
  entries: EdlEntry[],
  outputMs: number,
): { entry: EdlEntry; index: number; source_ms: number } | null {
  const last = entries[entries.length - 1];
  if (!last) return null;
  const clamped = Math.max(0, Math.min(outputMs, last.output_end_ms));
  const found = entries.findIndex(
    (entry) => clamped >= entry.output_start_ms && clamped < entry.output_end_ms,
  );
  const index = found >= 0 ? found : entries.length - 1;
  const entry = entries[index];
  if (!entry) return null;
  const offset = Math.max(0, Math.min(entry.output_end_ms - entry.output_start_ms, clamped - entry.output_start_ms));
  return { entry, index, source_ms: entry.source_start_ms + offset };
}

function timelineEntries(timeline: TimelineReport, includeHook: boolean): DraftEntry[] {
  const drafts: DraftEntry[] = [];
  for (const entry of timeline.entries) {
    if (entry.role === "hook" && !includeHook) continue;
    drafts.push({
      role: entry.role,
      source_start_ms: entry.source_start_ms,
      source_end_ms: entry.source_end_ms,
      transition: entry.transition,
      key_point: entry.key_point,
    });
  }
  return drafts;
}

function spansAreValid(entries: DraftEntry[], sourceDurationMs?: number): boolean {
  return entries.every((entry) => {
    if (!Number.isFinite(entry.source_start_ms) || !Number.isFinite(entry.source_end_ms)) return false;
    if (entry.source_start_ms < 0 || entry.source_end_ms <= entry.source_start_ms) return false;
    if (sourceDurationMs !== undefined && entry.source_end_ms > sourceDurationMs + 250) return false;
    return true;
  });
}

function withOutputTimes(drafts: DraftEntry[]): EdlEntry[] {
  let cursor = 0;
  return drafts.map((draft, index) => {
    const duration = Math.max(0, draft.source_end_ms - draft.source_start_ms);
    const entry: EdlEntry = {
      ...draft,
      index,
      output_start_ms: cursor,
      output_end_ms: cursor + duration,
    };
    cursor += duration;
    return entry;
  });
}

/** A line that sits strictly before the clip can open it without reordering. */
function beforeHook(candidate: Candidate, clipStartMs: number, includeHook: boolean): DraftEntry | null {
  if (!includeHook) return null;
  const hook = candidate.hook_detail;
  if (!hook || !hook.approved) return null;
  if (hook.placement !== "before" || hook.direction !== "before") return null;
  if (hook.source === "none") return null;
  const start = hook.start_ms;
  const end = hook.end_ms;
  if (!Number.isInteger(start) || !Number.isInteger(end)) return null;
  if (start < 0 || end - start < MIN_HOOK_MS) return null;
  if (end > clipStartMs) return null;
  return {
    role: "hook",
    source_start_ms: start,
    source_end_ms: end,
    transition: "cut",
    key_point: hook.text,
  };
}

function basisNote(basis: EdlBasis, strategy: string): string {
  switch (basis) {
    case "timeline":
      return strategy === "loop"
        ? "Preview plays the validated loop timeline, including its deliberate callback."
        : "Preview plays the validated editorial timeline in the same order the export uses.";
    case "manual_trim":
      return "Preview plays the current trim area in source order. Saving a manual trim keeps source order on export.";
    case "review_not_approved":
      return "This edit reorders the source. Approve it in the editorial plan to preview that order; until then, source order is shown.";
    case "timeline_unavailable":
      return "No validated editorial timeline is stored, so preview uses source order.";
    case "invalid_timeline_span":
      return "The stored timeline contains a span outside the source, so preview falls back to source order.";
    default:
      return "Preview plays the clip in source order.";
  }
}
