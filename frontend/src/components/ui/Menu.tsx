import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { MoreHorizontal } from 'lucide-react';
import { cn } from '@/lib/cn';
import { IconButton } from './Button';

export interface MenuItem {
  label: string;
  icon?: ReactNode;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
}

/** Overflow menu (WAI-ARIA menu button): arrow keys move, Enter/Space selects, Escape closes. */
export function Menu({
  items,
  label = 'More actions',
  trigger,
  align = 'right',
}: {
  items: MenuItem[];
  label?: string;
  trigger?: ReactNode;
  align?: 'left' | 'right';
}) {
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const wrap = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    requestAnimationFrame(() => itemRefs.current.find((b) => b && !b.disabled)?.focus());
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  const move = (dir: 1 | -1) => {
    const enabled = itemRefs.current.filter((b): b is HTMLButtonElement => !!b && !b.disabled);
    const idx = enabled.indexOf(document.activeElement as HTMLButtonElement);
    enabled[(idx + dir + enabled.length) % enabled.length]?.focus();
  };

  return (
    <div ref={wrap} className="relative inline-flex">
      <span
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown') {
            e.preventDefault();
            setOpen(true);
          }
        }}
      >
        {trigger ?? (
          <IconButton
            label={label}
            icon={<MoreHorizontal className="h-4 w-4" />}
            size="sm"
            aria-haspopup="menu"
            aria-expanded={open}
            aria-controls={open ? menuId : undefined}
          />
        )}
      </span>
      {open && (
        <div
          id={menuId}
          role="menu"
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') {
              e.preventDefault();
              move(1);
            } else if (e.key === 'ArrowUp') {
              e.preventDefault();
              move(-1);
            } else if (e.key === 'Escape') {
              e.preventDefault();
              e.stopPropagation();
              setOpen(false);
              (wrap.current?.querySelector('button') as HTMLButtonElement | null)?.focus();
            } else if (e.key === 'Tab') {
              setOpen(false);
            }
          }}
          className={cn(
            'absolute top-full z-50 mt-1 min-w-[180px] rounded-lg border border-ink-700 bg-ink-850 p-1 shadow-raised animate-fade-in',
            align === 'right' ? 'right-0' : 'left-0',
          )}
        >
          {items.map((it, i) => (
            <button
              key={it.label}
              ref={(el) => {
                itemRefs.current[i] = el;
              }}
              type="button"
              role="menuitem"
              tabIndex={-1}
              disabled={it.disabled}
              onClick={() => {
                setOpen(false);
                it.onSelect();
              }}
              className={cn(
                'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] transition-colors',
                'hover:bg-ink-800 focus:bg-ink-800 focus:outline-none disabled:opacity-40',
                it.danger ? 'text-danger' : 'text-ice-100',
              )}
            >
              {it.icon && <span className="text-muted [&>svg]:h-3.5 [&>svg]:w-3.5">{it.icon}</span>}
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
