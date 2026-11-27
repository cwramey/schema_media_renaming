import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';
import { ITEM_STATUS, JOB_STATUS, WATCH_STATUS, type Tone } from '@/lib/labels';
import type { ItemStatus, JobStatus, WatchStatus } from '@/api/types';

const TONES: Record<Tone, string> = {
  neutral: 'bg-ink-800 text-muted border-ink-700',
  info: 'bg-info/10 text-info border-info/25',
  success: 'bg-success/10 text-success border-success/25',
  warning: 'bg-warning/10 text-warning border-warning/30',
  danger: 'bg-danger/10 text-danger border-danger/30',
  brand: 'bg-brand-sky/10 text-accent border-brand-sky/30',
};

const DOTS: Record<Tone, string> = {
  neutral: 'bg-slate-400',
  info: 'bg-info',
  success: 'bg-success',
  warning: 'bg-warning',
  danger: 'bg-danger',
  brand: 'bg-brand-sky',
};

export interface BadgeProps {
  tone?: Tone;
  children: ReactNode;
  icon?: ReactNode;
  mono?: boolean;
  className?: string;
  title?: string;
}

/** Small rounded chip. Used for parsed-metadata chips, provider badges, counts. */
export function Badge({ tone = 'neutral', children, icon, mono, className, title }: BadgeProps) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex h-5 shrink-0 items-center gap-1 whitespace-nowrap rounded-md border px-1.5 text-2xs font-medium',
        mono && 'font-mono',
        TONES[tone],
        className,
      )}
    >
      {icon}
      {children}
    </span>
  );
}

export interface StatusPillProps {
  tone: Tone;
  label: string;
  pulse?: boolean;
  className?: string;
}

export function StatusPill({ tone, label, pulse, className }: StatusPillProps) {
  return (
    <span
      className={cn(
        'inline-flex h-5 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2 text-2xs font-semibold',
        TONES[tone],
        className,
      )}
    >
      <span className={cn('h-1.5 w-1.5 rounded-full', DOTS[tone], pulse && 'animate-pulse')} aria-hidden />
      {label}
    </span>
  );
}

export const JobStatusPill = ({ status, className }: { status: JobStatus; className?: string }) => (
  <StatusPill
    {...JOB_STATUS[status]}
    pulse={status === 'matching' || status === 'executing'}
    className={className}
  />
);

export const ItemStatusPill = ({ status, className }: { status: ItemStatus; className?: string }) => (
  <StatusPill {...ITEM_STATUS[status]} pulse={status === 'executing'} className={className} />
);

export const WatchStatusPill = ({ status, className }: { status: WatchStatus; className?: string }) => (
  <StatusPill {...WATCH_STATUS[status]} className={className} />
);

export function Dot({ tone, className, label }: { tone: Tone; className?: string; label?: string }) {
  return (
    <span
      role={label ? 'img' : undefined}
      aria-label={label}
      className={cn('inline-block h-2 w-2 shrink-0 rounded-full', DOTS[tone], className)}
    />
  );
}
