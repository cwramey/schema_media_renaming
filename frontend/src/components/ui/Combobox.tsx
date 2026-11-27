import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';
import { Check, ChevronsUpDown } from 'lucide-react';
import { cn } from '@/lib/cn';
import { controlBase } from './Input';

export interface ComboOption {
  value: string;
  label: string;
  description?: string;
  icon?: ReactNode;
}

export interface ComboboxProps {
  value: string;
  onChange: (value: string) => void;
  options: readonly ComboOption[];
  placeholder?: string;
  /** Accept typed text that isn't one of the options (e.g. a path). */
  allowCustom?: boolean;
  mono?: boolean;
  id?: string;
  className?: string;
  disabled?: boolean;
  emptyText?: string;
  'aria-label'?: string;
  'aria-describedby'?: string;
}

/** WAI-ARIA combobox (editable input + listbox popup) with type-to-filter and full keyboard control. */
export function Combobox({
  value,
  onChange,
  options,
  placeholder,
  allowCustom = false,
  mono,
  id,
  className,
  disabled,
  emptyText = 'No matches',
  ...aria
}: ComboboxProps) {
  const autoId = useId();
  const inputId = id ?? autoId;
  const listId = `${inputId}-list`;
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState<string | null>(null);
  const [active, setActive] = useState(0);

  const selected = options.find((o) => o.value === value);
  const display = query ?? (selected ? selected.label : value);

  const filtered = useMemo(() => {
    const q = (query ?? '').trim().toLowerCase();
    if (!q) return options;
    return options.filter((o) => o.label.toLowerCase().includes(q) || o.value.toLowerCase().includes(q));
  }, [options, query]);

  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`);
    el?.scrollIntoView({ block: 'nearest' });
  }, [active, open]);

  const commit = (opt: ComboOption | null) => {
    if (opt) onChange(opt.value);
    else if (allowCustom && query !== null) onChange(query);
    setQuery(null);
    setOpen(false);
  };

  return (
    <div className={cn('relative', className)}>
      <input
        ref={inputRef}
        id={inputId}
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={open && filtered[active] ? `${listId}-${active}` : undefined}
        autoComplete="off"
        spellCheck={false}
        disabled={disabled}
        placeholder={placeholder}
        value={display}
        onChange={(e) => {
          setQuery(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => {
          // Let option clicks land first.
          setTimeout(() => {
            if (document.activeElement !== inputRef.current) commit(null);
          }, 120);
        }}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown') {
            e.preventDefault();
            setOpen(true);
            setActive((a) => Math.min(a + 1, Math.max(0, filtered.length - 1)));
          } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setActive((a) => Math.max(a - 1, 0));
          } else if (e.key === 'Enter') {
            if (open && filtered[active]) {
              e.preventDefault();
              commit(filtered[active]);
            } else if (allowCustom) {
              e.preventDefault();
              commit(null);
            }
          } else if (e.key === 'Escape') {
            if (open) {
              e.preventDefault();
              e.stopPropagation();
            }
            setQuery(null);
            setOpen(false);
          }
        }}
        className={cn(controlBase, 'h-8 pl-2.5 pr-8 text-[13px]', mono && 'font-mono text-xs')}
        {...aria}
      />
      <ChevronsUpDown className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
      {open && (
        <ul
          ref={listRef}
          id={listId}
          role="listbox"
          className="absolute z-50 mt-1 max-h-64 w-full overflow-auto rounded-lg border border-ink-700 bg-ink-850 p-1 shadow-raised animate-fade-in"
        >
          {filtered.length === 0 ? (
            <li className="px-2 py-1.5 text-xs text-muted">{allowCustom && query ? `Use “${query}”` : emptyText}</li>
          ) : (
            filtered.map((o, i) => (
              <li
                key={o.value}
                id={`${listId}-${i}`}
                data-index={i}
                role="option"
                aria-selected={o.value === value}
                onMouseDown={(e) => e.preventDefault()}
                onMouseEnter={() => setActive(i)}
                onClick={() => commit(o)}
                className={cn(
                  'flex cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 text-[13px]',
                  i === active && 'bg-ink-800',
                )}
              >
                <span className="mt-0.5 w-3.5 shrink-0">
                  {o.value === value && <Check className="h-3.5 w-3.5 text-accent" aria-hidden />}
                </span>
                {o.icon}
                <span className="min-w-0 flex-1">
                  <span className={cn('block truncate', mono && 'font-mono text-xs')}>{o.label}</span>
                  {o.description && <span className="block truncate text-xs text-muted">{o.description}</span>}
                </span>
              </li>
            ))
          )}
        </ul>
      )}
    </div>
  );
}
