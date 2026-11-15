"use client";

import { useMemo, useState } from "react";
import { Check, Clock, Scissors, X } from "lucide-react";
import type { Candidate } from "@clipforge/shared";
import { formatDuration, formatTimecode, scoreTone } from "@clipforge/shared";
import { cn } from "@/lib/utils";

const TONE_CLASS = {
  high: "border-signal-good/40 bg-signal-good/10 text-signal-good",
  mid: "border-ember-500/40 bg-ember-500/10 text-ember-400",
  low: "border-white/12 bg-white/[0.04] text-mist-500",
} as const;

export function CandidateRail({
  candidates,
  selectedId,
  onSelect,
  onDecision,
}: {
  candidates: Candidate[];
  selectedId: string | null;
  onSelect: (candidate: Candidate) => void;
  onDecision: (candidate: Candidate, decision: Candidate["decision"]) => void;
}) {
  const [order, setOrder] = useState<"ranked" | "timeline">("ranked");

  const ordered = useMemo(() => {
    const copy = [...candidates];
    if (order === "timeline") copy.sort((a, b) => a.start_ms - b.start_ms);
    else copy.sort((a, b) => a.rank - b.rank);
    return copy;
  }, [candidates, order]);

  const accepted = candidates.filter((candidate) => candidate.decision === "accepted").length;
  const rejected = candidates.filter((candidate) => candidate.decision === "rejected").length;

  return (
    <div className="flex min-h-0 flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <p className="label">Moments</p>
          <p className="numeric mt-0.5 text-[11.5px] text-mist-500">
            {candidates.length} proposed
            {accepted > 0 ? ` · ${accepted} kept` : ""}
            {rejected > 0 ? ` · ${rejected} cut` : ""}
          </p>
        </div>
        <div className="segmented">
          <button type="button" data-active={order === "ranked"} onClick={() => setOrder("ranked")}>
            Ranked
          </button>
          <button type="button" data-active={order === "timeline"} onClick={() => setOrder("timeline")}>
            Timeline
          </button>
        </div>
      </div>

      <ol className="-mr-1 max-h-[calc(100vh-15rem)] space-y-2 overflow-y-auto pr-1">
        {ordered.map((candidate) => {
          const tone = scoreTone(candidate.overall);
          const active = candidate.id === selectedId;
          return (
            <li key={candidate.id}>
              <div
                className={cn(
                  "group rounded-lg border transition-colors",
                  active
                    ? "border-ember-500/50 bg-ember-500/[0.06]"
                    : "border-white/8 bg-black/20 hover:border-white/16",
                  candidate.decision === "rejected" && "opacity-55",
                )}
              >
                <button type="button" onClick={() => onSelect(candidate)} className="w-full px-3 pt-2.5 pb-2 text-left">
                  <div className="flex items-start gap-2.5">
                    <span className={cn("numeric mt-0.5 grid size-6 shrink-0 place-items-center rounded border text-[11px] font-semibold", TONE_CLASS[tone])}>
                      {candidate.rank}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-[13px] font-medium text-mist-100">{candidate.title}</p>
                      <p className="mt-0.5 line-clamp-2 text-[11.5px] leading-snug text-mist-500">
                        {candidate.hook || candidate.excerpt}
                      </p>
                    </div>
                    <span className="numeric shrink-0 text-[12px] font-semibold text-mist-200">
                      {Math.round(candidate.overall)}
                    </span>
                  </div>

                  <div className="mt-2 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[10.5px] text-mist-600">
                    <span className="numeric flex items-center gap-1">
                      <Clock className="size-3" strokeWidth={2} />
                      {formatTimecode(candidate.start_ms)}–{formatTimecode(candidate.end_ms)}
                    </span>
                    <span className="numeric">{formatDuration(candidate.duration_ms)}</span>
                    <span className="capitalize">{candidate.category.replace(/_/g, " ")}</span>
                    {candidate.edited ? (
                      <span className="flex items-center gap-1 text-signal-info">
                        <Scissors className="size-3" strokeWidth={2} />
                        trimmed
                      </span>
                    ) : null}
                    {candidate.warnings.length > 0 ? (
                      <span className="text-signal-warn/80">{candidate.warnings.length} note{candidate.warnings.length === 1 ? "" : "s"}</span>
                    ) : null}
                  </div>
                </button>

                <div className="flex items-center justify-end gap-1 border-t border-white/6 px-2 py-1">
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
    </div>
  );
}
