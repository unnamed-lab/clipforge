import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

type Tone = "neutral" | "good" | "warn" | "bad" | "info" | "ember";

const TONES: Record<Tone, string> = {
  neutral: "border-white/10 bg-white/[0.04] text-mist-300",
  good: "border-signal-good/30 bg-signal-good/10 text-signal-good",
  warn: "border-signal-warn/30 bg-signal-warn/10 text-signal-warn",
  bad: "border-signal-bad/30 bg-signal-bad/10 text-signal-bad",
  info: "border-signal-info/30 bg-signal-info/10 text-signal-info",
  ember: "border-ember-500/30 bg-ember-500/10 text-ember-300",
};

export function Badge({
  children,
  tone = "neutral",
  className,
}: {
  children: ReactNode;
  tone?: Tone;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex h-6 items-center gap-1.5 rounded-full border px-2.5 text-[11px] font-medium tracking-wide",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function ProgressBar({
  value,
  tone = "ember",
  className,
}: {
  value: number;
  tone?: "ember" | "good" | "info";
  className?: string;
}) {
  const safe = Math.min(100, Math.max(0, value));
  const fill =
    tone === "good" ? "bg-signal-good" : tone === "info" ? "bg-signal-info" : "bg-ember-500";
  return (
    <div
      className={cn("h-1.5 w-full overflow-hidden rounded-full bg-white/10", className)}
      role="progressbar"
      aria-valuenow={Math.round(safe)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className={cn("h-full rounded-full transition-[width] duration-500 ease-out", fill)}
        style={{ width: `${safe}%` }}
      />
    </div>
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        "inline-block size-3.5 shrink-0 animate-spin rounded-full border-[1.5px] border-current border-t-transparent",
        className,
      )}
      aria-hidden="true"
    />
  );
}

export function Alert({
  tone = "neutral",
  title,
  children,
  action,
}: {
  tone?: Tone;
  title?: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className={cn("flex items-start gap-3 rounded-lg border px-3.5 py-3 text-[13px]", TONES[tone])}>
      <div className="min-w-0 flex-1">
        {title ? <p className="font-semibold">{title}</p> : null}
        <div className={cn("text-[13px] leading-relaxed", title && "mt-0.5 opacity-90")}>{children}</div>
      </div>
      {action}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: {
  icon?: ReactNode;
  title: string;
  description: string;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-white/12 px-6 py-12 text-center",
        className,
      )}
    >
      {icon ? <div className="text-mist-600">{icon}</div> : null}
      <div className="space-y-1">
        <p className="text-sm font-semibold text-mist-100">{title}</p>
        <p className="mx-auto max-w-sm text-[13px] leading-relaxed text-mist-500">{description}</p>
      </div>
      {action}
    </div>
  );
}

export function Stat({
  label,
  value,
  hint,
  className,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("space-y-1", className)}>
      <p className="label">{label}</p>
      <p className="numeric text-[15px] font-medium text-mist-100">{value}</p>
      {hint ? <p className="text-[11.5px] text-mist-600">{hint}</p> : null}
    </div>
  );
}

export function SectionHeading({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="space-y-1.5">
        {eyebrow ? <p className="label">{eyebrow}</p> : null}
        <h2 className="text-lg font-semibold text-mist-50">{title}</h2>
        {description ? (
          <p className="max-w-2xl text-[13px] leading-relaxed text-mist-500">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </div>
  );
}

/** A single 0–100 score rendered as a thin meter with its numeric value. */
export function ScoreMeter({
  label,
  value,
  weight,
  reason,
}: {
  label: string;
  value: number;
  weight?: number;
  reason?: string;
}) {
  const pct = Math.min(100, Math.max(0, value));
  const color = pct >= 70 ? "bg-signal-good" : pct >= 45 ? "bg-ember-500" : "bg-mist-600";
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-[12.5px] capitalize text-mist-300">{label.replace(/_/g, " ")}</span>
        <span className="numeric text-[11.5px] text-mist-500">
          {weight !== undefined ? `${Math.round(weight * 100)}% · ` : ""}
          {Math.round(pct)}
        </span>
      </div>
      <div className="h-1 w-full overflow-hidden rounded-full bg-white/8">
        <div className={cn("h-full rounded-full", color)} style={{ width: `${pct}%` }} />
      </div>
      {reason ? <p className="text-[11.5px] leading-snug text-mist-600">{reason}</p> : null}
    </div>
  );
}
