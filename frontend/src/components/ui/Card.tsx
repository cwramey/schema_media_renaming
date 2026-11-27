import type { HTMLAttributes, ReactNode } from 'react';
import { AlertTriangle, RotateCw } from 'lucide-react';
import { cn } from '@/lib/cn';
import { Mark } from '@/components/brand/Mark';
import { Button } from './Button';

export interface CardProps extends Omit<HTMLAttributes<HTMLElement>, 'title'> {
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  icon?: ReactNode;
  /** Remove body padding (tables, lists). */
  flush?: boolean;
  bodyClassName?: string;
}

export function Card({ title, description, actions, icon, flush, className, bodyClassName, children, ...rest }: CardProps) {
  return (
    <section className={cn('surface flex min-w-0 flex-col', className)} {...rest}>
      {(title || actions) && (
        <header className="flex items-start gap-3 border-b border-ink-800 px-4 py-3">
          {icon && <span className="mt-0.5 text-muted [&>svg]:h-4 [&>svg]:w-4">{icon}</span>}
          <div className="min-w-0 flex-1">
            {title && <h2 className="text-[13px] font-semibold text-ice-100">{title}</h2>}
            {description && <p className="mt-0.5 text-xs text-muted">{description}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-1">{actions}</div>}
        </header>
      )}
      <div className={cn('min-h-0 flex-1', !flush && 'p-4', bodyClassName)}>{children}</div>
    </section>
  );
}

export interface EmptyStateProps {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
  compact?: boolean;
  icon?: ReactNode;
}

/** Empty state with the brand mark (SPEC §9.5: empty states use the mark). */
export function EmptyState({ title, description, action, className, compact, icon }: EmptyStateProps) {
  return (
    <div className={cn('flex flex-col items-center justify-center text-center', compact ? 'gap-2 py-6' : 'gap-3 py-12', className)}>
      {icon ?? (
        <div className="relative">
          <div className="absolute inset-0 rounded-full bg-brand-gradient opacity-20 blur-2xl" aria-hidden />
          <Mark size={compact ? 36 : 56} className="relative opacity-80" />
        </div>
      )}
      <div className="max-w-sm">
        <p className={cn('font-semibold text-ice-100', compact ? 'text-[13px]' : 'text-sm')}>{title}</p>
        {description && <p className="mt-1 text-xs text-muted">{description}</p>}
      </div>
      {action}
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
  title = 'Could not load this',
  compact,
}: {
  error: unknown;
  onRetry?: () => void;
  title?: string;
  compact?: boolean;
}) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <EmptyState
      compact={compact}
      icon={
        <span className="flex h-10 w-10 items-center justify-center rounded-full bg-danger/10 text-danger">
          <AlertTriangle className="h-5 w-5" />
        </span>
      }
      title={title}
      description={message}
      action={
        onRetry && (
          <Button size="sm" leftIcon={<RotateCw className="h-3.5 w-3.5" />} onClick={onRetry}>
            Retry
          </Button>
        )
      }
    />
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('skeleton h-4', className)} aria-hidden />;
}

export function SkeletonRows({ rows = 6, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn('flex flex-col gap-2', className)} aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className={cn('h-8', i % 3 === 2 && 'w-4/5')} />
      ))}
    </div>
  );
}

export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={cn(
        'inline-flex h-5 min-w-[20px] items-center justify-center rounded border border-ink-700 border-b-2 bg-ink-850 px-1',
        'font-mono text-2xs font-medium text-muted',
        className,
      )}
    >
      {children}
    </kbd>
  );
}

export function Stat({
  label,
  value,
  hint,
  icon,
  tone,
  className,
}: {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  icon?: ReactNode;
  tone?: 'brand' | 'warning' | 'default';
  className?: string;
}) {
  return (
    <div className={cn('surface relative overflow-hidden p-4', className)}>
      {tone === 'brand' && <div className="absolute inset-x-0 top-0 h-0.5 bg-brand-gradient" aria-hidden />}
      <div className="flex items-center gap-2 text-xs font-medium text-muted">
        {icon && <span className="[&>svg]:h-3.5 [&>svg]:w-3.5">{icon}</span>}
        {label}
      </div>
      <div className={cn('tabular mt-2 text-2xl font-semibold tracking-tight', tone === 'warning' && 'text-warning')}>
        {value}
      </div>
      {hint && <div className="mt-1 text-xs text-muted">{hint}</div>}
    </div>
  );
}
