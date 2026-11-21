"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Check,
  CheckCircle2,
  ChevronDown,
  Clock,
  Eye,
  Scissors,
  Search,
  SlidersHorizontal,
  Sparkles,
  X,
} from "lucide-react";
import type { Candidate } from "@clipforge/shared";
import { formatDuration, formatTimecode, scoreTone } from "@clipforge/shared";
import { cn } from "@/lib/utils";
import {
  activeFilterCount,
  candidateFacets,
  candidateType,
  EMPTY_FILTERS,
  filterCandidates,
  sortCandidates,
  type CandidateFilters,
  type CandidateSort,
} from "./candidate-filter";

const TONE_CLASS = {
  high: "border-signal-good/40 bg-signal-good/10 text-signal-good",
  mid: "border-ember-500/40 bg-ember-500/10 text-ember-400",
  low: "border-white/12 bg-white/[0.04] text-mist-500",
} as const;

const SORT_LABEL: Record<CandidateSort, string> = {
  quality: "Quality",
  chronological: "Chronological",
  duration: "Duration",
};

export function CandidateRail({
  candidates,
  selectedId,
  onSelect,
  onDecision,
  onBatchCreate,
  onPreviewSelected,
  onFindMore,
  batchBusy = false,
  convertedIds,
  onCountsChange,
}: {
  candidates: Candidate[];
  selectedId: string | null;
  onSelect: (candidate: Candidate) => void;
  onDecision: (candidate: Candidate, decision: Candidate["decision"]) => void;
  onBatchCreate?: (candidates: Candidate[]) => Promise<void>;
  onPreviewSelected?: (candidates: Candidate[]) => void;
  onFindMore?: () => void;
  batchBusy?: boolean;
  convertedIds?: Set<string>;
  onCountsChange?: (counts: { displayed: number; selected: number }) => void;
}) {
  const [sort, setSort] = useState<CandidateSort>("quality");
  const [filters, setFilters] = useState<CandidateFilters>(EMPTY_FILTERS);
  const [filterOpen, setFilterOpen] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const facets = useMemo(() => candidateFacets(candidates), [candidates]);

  const ordered = useMemo(() => {
    const filtered = filterCandidates(candidates, filters);
    return sortCandidates(filtered, sort);
  }, [candidates, filters, sort]);

  const filteredCount = ordered.length;
  const accepted = candidates.filter((candidate) => candidate.decision === "accepted").length;
  const rejected = candidates.filter((candidate) => candidate.decision === "rejected").length;
  const filtersActive = activeFilterCount(filters);

  useEffect(() => {
    setSelected((current) => {
      const live = new Set(candidates.map((candidate) => candidate.id));
      const next = new Set([...current].filter((id) => live.has(id)));
      return next.size === current.size ? current : next;
    });
  }, [candidates]);

  useEffect(() => {
    onCountsChange?.({ displayed: filteredCount, selected: selected.size });
  }, [filteredCount, selected.size, onCountsChange]);

  const selectedCandidates = useMemo(
    () => candidates.filter((candidate) => selected.has(candidate.id)),
    [candidates, selected],
  );

  function toggleSelected(candidateId: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(candidateId)) next.delete(candidateId);
      else next.add(candidateId);
      return next;
    });
    setConfirming(false);
  }

  function selectShown() {
    setSelected(new Set(ordered.map((candidate) => candidate.id)));
    setConfirming(false);
  }

  function resetFilters() {
    setFilters(EMPTY_FILTERS);
    setConfirming(false);
  }

  async function createClips() {
    if (!onBatchCreate || selectedCandidates.length === 0) return;
    await onBatchCreate(selectedCandidates);
    setConfirming(false);
  }

  return (
    <div className="flex min-h-0 flex-col gap-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="label">Moments</p>
          <p className="numeric mt-0.5 text-[11.5px] text-mist-500">
            {filteredCount === candidates.length
              ? `${candidates.length} discovered`
              : `${filteredCount} of ${candidates.length} shown`}
            {accepted > 0 ? ` · ${accepted} kept` : ""}
            {rejected > 0 ? ` · ${rejected} cut` : ""}
          </p>
        </div>
        {onFindMore ? (
          <button type="button" className="btn btn-sm h-7 px-2 text-[11px]" onClick={onFindMore}>
            <Sparkles className="size-3.5" strokeWidth={2} />
            Find more
          </button>
        ) : null}
      </div>

      <label className="relative block">
        <Search
          className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-mist-600"
          strokeWidth={2}
        />
        <input
          className="field h-8 w-full pl-8 text-[12px]"
          placeholder="Search title, transcript, topic…"
          value={filters.query}
          onChange={(event) => setFilters((current) => ({ ...current, query: event.target.value }))}
          aria-label="Search moments"
        />
      </label>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <button
          type="button"
          className={cn(
            "btn btn-ghost btn-sm h-7 gap-1 px-2 text-[11px]",
            filtersActive > 0 && "text-ember-300",
          )}
          onClick={() => setFilterOpen((value) => !value)}
          aria-expanded={filterOpen}
        >
          <SlidersHorizontal className="size-3.5" strokeWidth={2} />
          Filters{filtersActive > 0 ? ` (${filtersActive})` : ""}
          <ChevronDown
            className={cn("size-3 transition-transform", filterOpen && "rotate-180")}
            strokeWidth={2}
          />
        </button>
        <div className="segmented text-[11px]">
          {(Object.keys(SORT_LABEL) as CandidateSort[]).map((value) => (
            <button
              key={value}
              type="button"
              data-active={sort === value}
              onClick={() => setSort(value)}
              title={`Sort by ${SORT_LABEL[value].toLowerCase()}`}
            >
              {SORT_LABEL[value]}
            </button>
          ))}
        </div>
      </div>

      {filterOpen ? (
        <div className="space-y-2 rounded-lg border border-white/8 bg-black/20 p-2.5">
          <div className="grid grid-cols-2 gap-2">
            <label className="space-y-1">
              <span className="label">Type</span>
              <select
                className="field h-8 w-full text-[12px]"
                value={filters.momentType}
                onChange={(event) =>
                  setFilters((current) => ({ ...current, momentType: event.target.value }))
                }
              >
                <option value="">All types</option>
                {facets.types.map((type) => (
                  <option key={type} value={type}>
                    {type.replace(/_/g, " ")}
                  </option>
                ))}
              </select>
            </label>
            <label className="space-y-1">
              <span className="label">Decision</span>
              <select
                className="field h-8 w-full text-[12px]"
                value={filters.decision}
                onChange={(event) =>
                  setFilters((current) => ({
                    ...current,
                    decision: event.target.value as CandidateFilters["decision"],
                  }))
                }
              >
                <option value="all">All</option>
                <option value="proposed">Undecided</option>
                <option value="accepted">Kept</option>
                <option value="rejected">Cut</option>
              </select>
            </label>
            {facets.speakers.length > 0 ? (
              <label className="space-y-1">
                <span className="label">Speaker</span>
                <select
                  className="field h-8 w-full text-[12px]"
                  value={filters.speaker}
                  onChange={(event) =>
                    setFilters((current) => ({ ...current, speaker: event.target.value }))
                  }
                >
                  <option value="">Any speaker</option>
                  {facets.speakers.map((speaker) => (
                    <option key={speaker} value={speaker}>
                      {speaker}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            <div className="space-y-1">
              <span className="label">Duration (s)</span>
              <div className="flex items-center gap-1">
                <input
                  className="field h-8 w-full text-[12px]"
                  type="number"
                  min={1}
                  placeholder="min"
                  value={filters.minDurationMs === null ? "" : filters.minDurationMs / 1000}
                  onChange={(event) =>
                    setFilters((current) => ({
                      ...current,
                      minDurationMs: event.target.value ? Number(event.target.value) * 1000 : null,
                    }))
                  }
                  aria-label="Minimum duration in seconds"
                />
                <span className="text-[11px] text-mist-600">–</span>
                <input
                  className="field h-8 w-full text-[12px]"
                  type="number"
                  min={1}
                  placeholder="max"
                  value={filters.maxDurationMs === null ? "" : filters.maxDurationMs / 1000}
                  onChange={(event) =>
                    setFilters((current) => ({
                      ...current,
                      maxDurationMs: event.target.value ? Number(event.target.value) * 1000 : null,
                    }))
                  }
                  aria-label="Maximum duration in seconds"
                />
              </div>
            </div>
          </div>
          <div className="space-y-1">
            <span className="label">Source time (s)</span>
            <div className="flex items-center gap-1">
              <input
                className="field h-8 w-full text-[12px]"
                type="number"
                min={0}
                placeholder="from"
                value={filters.fromMs === null ? "" : filters.fromMs / 1000}
                onChange={(event) =>
                  setFilters((current) => ({
                    ...current,
                    fromMs: event.target.value ? Number(event.target.value) * 1000 : null,
                  }))
                }
                aria-label="Source time from in seconds"
              />
              <span className="text-[11px] text-mist-600">–</span>
              <input
                className="field h-8 w-full text-[12px]"
                type="number"
                min={0}
                placeholder="to"
                value={filters.toMs === null ? "" : filters.toMs / 1000}
                onChange={(event) =>
                  setFilters((current) => ({
                    ...current,
                    toMs: event.target.value ? Number(event.target.value) * 1000 : null,
                  }))
                }
                aria-label="Source time to in seconds"
              />
            </div>
          </div>
          <div className="flex items-center justify-between gap-2 pt-0.5">
            <button type="button" className="btn btn-ghost btn-sm h-7 px-2 text-[11px]" onClick={resetFilters}>
              Reset filters
            </button>
            <button type="button" className="btn btn-ghost btn-sm h-7 px-2 text-[11px]" onClick={selectShown}>
              Select all shown
            </button>
          </div>
        </div>
      ) : null}

      {selected.size > 0 ? (
        <div className="rounded-lg border border-ember-500/30 bg-ember-500/[0.06] px-2.5 py-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="numeric text-[11.5px] font-medium text-ember-200">
              {selected.size} selected
            </span>
            <button
              type="button"
              className="btn btn-ghost btn-sm h-6 gap-1 px-1.5 text-[11px]"
              onClick={() => {
                const first = selectedCandidates[0];
                if (first) onSelect(first);
                onPreviewSelected?.(selectedCandidates);
              }}
            >
              <Eye className="size-3" strokeWidth={2} />
              Preview
            </button>
            {confirming ? (
              <>
                <button
                  type="button"
                  className="btn btn-sm h-6 gap-1 border-signal-good/40 px-1.5 text-[11px] text-signal-good"
                  onClick={() => void createClips()}
                  disabled={batchBusy}
                >
                  <Check className="size-3" strokeWidth={2.4} />
                  Queue {selected.size} clip{selected.size === 1 ? "" : "s"}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm h-6 px-1.5 text-[11px]"
                  onClick={() => setConfirming(false)}
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                className="btn btn-sm h-6 gap-1 px-1.5 text-[11px]"
                onClick={() => setConfirming(true)}
                disabled={!onBatchCreate}
              >
                <Scissors className="size-3" strokeWidth={2} />
                Create clips
              </button>
            )}
            <button
              type="button"
              className="btn btn-ghost btn-sm ml-auto h-6 px-1.5 text-[11px] text-mist-500"
              onClick={() => {
                setSelected(new Set());
                setConfirming(false);
              }}
            >
              Clear
            </button>
          </div>
          {confirming ? (
            <p className="mt-1 text-[10.5px] leading-snug text-mist-400">
              This queues {selected.size} real render{selected.size === 1 ? "" : "s"} with your saved
              defaults. Nothing is uploaded or published.
            </p>
          ) : null}
        </div>
      ) : null}

      {ordered.length === 0 ? (
        <div className="rounded-lg border border-dashed border-white/10 px-3 py-6 text-center">
          <p className="text-[12.5px] text-mist-300">No moments match these filters.</p>
          <button type="button" className="btn btn-ghost btn-sm mt-2" onClick={resetFilters}>
            Clear filters
          </button>
        </div>
      ) : (
        <ol className="-mr-1 max-h-[calc(100vh-18rem)] space-y-2 overflow-y-auto pr-1">
          {ordered.map((candidate) => {
            const tone = scoreTone(candidate.overall);
            const active = candidate.id === selectedId;
            const checked = selected.has(candidate.id);
            const boundaryWarning = Boolean(candidate.boundary?.suspicious);
            const converted = convertedIds?.has(candidate.id) ?? false;
            return (
              <li key={candidate.id}>
                <div
                  className={cn(
                    "group rounded-lg border transition-colors",
                    active
                      ? "border-ember-500/50 bg-ember-500/[0.06]"
                      : "border-white/8 bg-black/20 hover:border-white/16",
                    candidate.decision === "rejected" && "opacity-55",
                    checked && !active && "border-ember-500/25",
                  )}
                >
                  <div className="flex items-start gap-2 px-3 pt-2.5">
                    <input
                      type="checkbox"
                      className="mt-1 size-3.5 shrink-0 accent-ember-500"
                      checked={checked}
                      onChange={() => toggleSelected(candidate.id)}
                      aria-label={`Select ${candidate.title}`}
                    />
                    <button
                      type="button"
                      onClick={() => onSelect(candidate)}
                      className="min-w-0 flex-1 text-left"
                    >
                      <div className="flex items-start gap-2">
                        <span
                          className={cn(
                            "numeric mt-0.5 grid size-6 shrink-0 place-items-center rounded border text-[11px] font-semibold",
                            TONE_CLASS[tone],
                          )}
                          title="Display rank"
                        >
                          {candidate.rank}
                        </span>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-[13px] font-medium text-mist-100">{candidate.title}</p>
                          <p className="mt-0.5 line-clamp-2 text-[11.5px] leading-snug text-mist-500">
                            {candidate.hook || candidate.excerpt}
                          </p>
                        </div>
                      </div>
                      {candidate.reason ? (
                        <p className="mt-1 line-clamp-1 text-[10.5px] leading-snug text-mist-600" title={candidate.reason}>
                          Why: {candidate.reason}
                        </p>
                      ) : null}
                      <div className="mt-1.5 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[10.5px] text-mist-600">
                        <span className="numeric flex items-center gap-1">
                          <Clock className="size-3" strokeWidth={2} />
                          {formatTimecode(candidate.start_ms)}–{formatTimecode(candidate.end_ms)}
                        </span>
                        <span className="numeric">{formatDuration(candidate.duration_ms)}</span>
                        <span className="capitalize">{candidateType(candidate).replace(/_/g, " ")}</span>
                        {candidate.speaker ? <span>{candidate.speaker}</span> : null}
                        {candidate.edited ? (
                          <span className="flex items-center gap-1 text-signal-info">
                            <Scissors className="size-3" strokeWidth={2} />
                            trimmed
                          </span>
                        ) : null}
                        {converted ? (
                          <span className="flex items-center gap-1 text-signal-good">
                            <CheckCircle2 className="size-3" strokeWidth={2} />
                            clip made
                          </span>
                        ) : null}
                        {boundaryWarning ? (
                          <span className="text-signal-warn/90">ending may be off</span>
                        ) : null}
                        {candidate.batch_kind === "additional" ? (
                          <span className="text-mist-600">new batch</span>
                        ) : null}
                      </div>
                    </button>
                    <span
                      className="numeric shrink-0 text-right text-[11px] text-mist-300"
                      title="Editorial quality score (0–100) from the deterministic ranker"
                    >
                      {Math.round(candidate.overall)}
                      <span className="block text-[8.5px] uppercase tracking-wide text-mist-600">quality</span>
                    </span>
                  </div>

                  <div className="mt-1.5 flex items-center justify-end gap-1 border-t border-white/6 px-2 py-1">
                    <button
                      type="button"
                      onClick={() => onDecision(candidate, candidate.decision === "accepted" ? "proposed" : "accepted")}
                      className={cn(
                        "btn btn-ghost btn-sm h-6 gap-1 px-1.5 text-[11px]",
                        candidate.decision === "accepted" && "text-signal-good",
                      )}
                      aria-pressed={candidate.decision === "accepted"}
                    >
                      <Check className="size-3" strokeWidth={2.4} />
                      Keep
                    </button>
                    <button
                      type="button"
                      onClick={() => onDecision(candidate, candidate.decision === "rejected" ? "proposed" : "rejected")}
                      className={cn(
                        "btn btn-ghost btn-sm h-6 gap-1 px-1.5 text-[11px]",
                        candidate.decision === "rejected" && "text-signal-bad",
                      )}
                      aria-pressed={candidate.decision === "rejected"}
                    >
                      <X className="size-3" strokeWidth={2.4} />
                      Cut
                    </button>
                  </div>
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
