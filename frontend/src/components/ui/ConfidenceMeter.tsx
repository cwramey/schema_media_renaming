import { cn } from '@/lib/cn';

/** SPEC §9.1: < 0.60 danger, < 0.85 warning, ≥ 0.85 success. */
function confidenceTone(v: number): 'danger' | 'warning' | 'success' {
  if (v < 0.6) return 'danger';
  if (v < 0.85) return 'warning';
  return 'success';
}

const BAR = { danger: 'bg-danger', warning: 'bg-warning', success: 'bg-success' };
const TEXT = { danger: 'text-danger', warning: 'text-warning', success: 'text-success' };

export interface ConfidenceMeterProps {
  value: number;
  /** Hide the numeric label (bar only). */
  compact?: boolean;
  className?: string;
  label?: string;
}

export function ConfidenceMeter({ value, compact, className, label = 'Match confidence' }: ConfidenceMeterProps) {
  const v = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0));
  const tone = confidenceTone(v);
  const pct = Math.round(v * 100);
  return (
    <span
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct}
      aria-valuetext={`${pct}%`}
      title={`${label}: ${pct}%`}
      className={cn('inline-flex shrink-0 items-center gap-1.5', className)}
    >
      <span className="relative h-1.5 w-10 overflow-hidden rounded-full bg-ink-800">
        <span className={cn('absolute inset-y-0 left-0 rounded-full', BAR[tone])} style={{ width: `${pct}%` }} />
      </span>
      {!compact && <span className={cn('tabular w-8 text-right text-2xs font-semibold', TEXT[tone])}>{pct}%</span>}
    </span>
  );
}

/** Brand-gradient progress bar; indeterminate when value is undefined. */
export function ProgressBar({
  value,
  className,
  label = 'Progress',
}: {
  value?: number;
  className?: string;
  label?: string;
}) {
  const pct = value === undefined ? undefined : Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct}
      className={cn('relative h-1.5 w-full overflow-hidden rounded-full bg-ink-800', className)}
    >
      {pct === undefined ? (
        <div className="absolute inset-y-0 w-2/5 rounded-full bg-brand-gradient animate-indeterminate" />
      ) : (
        <div
          className="absolute inset-y-0 left-0 rounded-full bg-brand-gradient transition-[width] duration-500"
          style={{ width: `${pct}%` }}
        />
      )}
    </div>
  );
}
