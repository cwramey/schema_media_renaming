import { useId, useRef, type KeyboardEvent, type ReactNode } from 'react';
import { cn } from '@/lib/cn';

export interface TabItem<V extends string = string> {
  value: V;
  label: ReactNode;
  count?: number;
  icon?: ReactNode;
  disabled?: boolean;
}

export interface TabsProps<V extends string = string> {
  value: V;
  onChange: (value: V) => void;
  items: readonly TabItem<V>[];
  variant?: 'underline' | 'pill';
  className?: string;
  'aria-label'?: string;
  /** id prefix used to link tabs with panels (`${idBase}-panel-${value}`). */
  idBase?: string;
}

/** Tablist with roving tabindex and arrow-key / Home / End navigation. */
export function Tabs<V extends string = string>({
  value,
  onChange,
  items,
  variant = 'underline',
  className,
  idBase,
  ...aria
}: TabsProps<V>) {
  const auto = useId();
  const base = idBase ?? auto;
  const refs = useRef<(HTMLButtonElement | null)[]>([]);

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const enabled = items.map((t, i) => (t.disabled ? -1 : i)).filter((i) => i >= 0);
    const cur = items.findIndex((t) => t.value === value);
    const pos = enabled.indexOf(cur);
    let next: number | undefined;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = enabled[(pos + 1) % enabled.length];
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = enabled[(pos - 1 + enabled.length) % enabled.length];
    else if (e.key === 'Home') next = enabled[0];
    else if (e.key === 'End') next = enabled[enabled.length - 1];
    if (next === undefined) return;
    e.preventDefault();
    onChange(items[next].value);
    refs.current[next]?.focus();
  };

  return (
    <div
      role="tablist"
      onKeyDown={onKey}
      className={cn(
        'flex items-center overflow-x-auto scrollbar-none',
        variant === 'underline' ? 'gap-4 border-b border-ink-800' : 'gap-1 rounded-lg bg-ink-950 p-0.5',
        className,
      )}
      {...aria}
    >
      {items.map((t, i) => {
        const selected = t.value === value;
        return (
          <button
            key={t.value}
            ref={(el) => {
              refs.current[i] = el;
            }}
            id={`${base}-tab-${t.value}`}
            role="tab"
            type="button"
            aria-selected={selected}
            aria-controls={`${base}-panel-${t.value}`}
            tabIndex={selected ? 0 : -1}
            disabled={t.disabled}
            onClick={() => onChange(t.value)}
            className={cn(
              'inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap font-medium transition-colors disabled:opacity-40',
              variant === 'underline'
                ? cn(
                    '-mb-px h-9 border-b-2 px-0.5 text-[13px]',
                    selected ? 'border-brand-sky text-ice-100' : 'border-transparent text-muted hover:text-ice-100',
                  )
                : cn(
                    'h-7 rounded-md px-2.5 text-xs',
                    selected ? 'bg-ink-800 text-ice-100 shadow-sm' : 'text-muted hover:text-ice-100',
                  ),
            )}
          >
            {t.icon}
            {t.label}
            {t.count !== undefined && (
              <span
                className={cn(
                  'tabular rounded-full px-1.5 text-2xs',
                  selected ? 'bg-brand-sky/15 text-accent' : 'bg-ink-800 text-muted',
                )}
              >
                {t.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({
  idBase,
  value,
  children,
  className,
}: {
  idBase: string;
  value: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div role="tabpanel" id={`${idBase}-panel-${value}`} aria-labelledby={`${idBase}-tab-${value}`} className={className}>
      {children}
    </div>
  );
}
