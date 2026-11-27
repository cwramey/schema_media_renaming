import {
  cloneElement,
  isValidElement,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import { cn } from '@/lib/cn';

export interface TooltipProps {
  content: ReactNode;
  children: ReactElement;
  side?: 'top' | 'bottom';
  delay?: number;
  className?: string;
  /** Render nothing extra when false/empty content. */
  disabled?: boolean;
}

/**
 * Lightweight tooltip: shows on hover and keyboard focus, positioned in a portal,
 * linked to the trigger with aria-describedby.
 */
export function Tooltip({ content, children, side = 'top', delay = 350, className, disabled }: TooltipProps) {
  const id = useId();
  const wrapRef = useRef<HTMLSpanElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number; side: 'top' | 'bottom' } | null>(null);

  const show = useCallback(
    (immediate = false) => {
      if (disabled || !content) return;
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setOpen(true), immediate ? 0 : delay);
    },
    [delay, disabled, content],
  );
  const hide = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    setOpen(false);
  }, []);

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && hide();
    window.addEventListener('keydown', onKey);
    window.addEventListener('scroll', hide, true);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', hide, true);
    };
  }, [open, hide]);

  useLayoutEffect(() => {
    if (!open || !wrapRef.current || !tipRef.current) return;
    const r = wrapRef.current.getBoundingClientRect();
    const t = tipRef.current.getBoundingClientRect();
    let s = side;
    if (s === 'top' && r.top - t.height - 8 < 0) s = 'bottom';
    if (s === 'bottom' && r.bottom + t.height + 8 > window.innerHeight) s = 'top';
    const top = s === 'top' ? r.top - t.height - 6 : r.bottom + 6;
    const left = Math.min(Math.max(8, r.left + r.width / 2 - t.width / 2), window.innerWidth - t.width - 8);
    setPos({ left, top, side: s });
  }, [open, side, content]);

  const trigger = isValidElement<{ 'aria-describedby'?: string }>(children)
    ? cloneElement(children, { 'aria-describedby': open ? id : children.props['aria-describedby'] })
    : children;

  return (
    <>
      <span
        ref={wrapRef}
        className="inline-flex max-w-full"
        onMouseEnter={() => show()}
        onMouseLeave={hide}
        onFocus={() => show(true)}
        onBlur={hide}
        onPointerDown={hide}
      >
        {trigger}
      </span>
      {open &&
        createPortal(
          <div
            ref={tipRef}
            id={id}
            role="tooltip"
            style={{ left: pos?.left ?? -9999, top: pos?.top ?? -9999 }}
            className={cn(
              'pointer-events-none fixed z-[100] max-w-xs rounded-md border border-ink-700 bg-ink-850 px-2 py-1',
              'text-xs leading-snug text-ice-100 shadow-raised animate-fade-in',
              className,
            )}
          >
            {content}
          </div>,
          document.body,
        )}
    </>
  );
}
