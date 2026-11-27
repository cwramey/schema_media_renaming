import {
  forwardRef,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from 'react';
import { createPortal } from 'react-dom';
import { cn } from '@/lib/cn';
import { isInsideBlock, segmentTokens, tokenize, type IssueRange, type TokenType } from '@/lib/expr/tokenizer';

export interface ExpressionEditorHandle {
  /** Insert text at the caret. With `binding`, wraps in `{…}` unless the caret is already inside a block. */
  insert: (text: string, opts?: { binding?: boolean; caretOffset?: number }) => void;
  select: (start: number, end: number) => void;
  focus: () => void;
}

export interface ExpressionEditorProps {
  value: string;
  onChange: (value: string) => void;
  issues: IssueRange[];
  id?: string;
  className?: string;
  onCaretChange?: (offset: number) => void;
  'aria-describedby'?: string;
  'aria-label'?: string;
}

/** Token colors for the highlighter (dark/light handled by theme tokens). */
const TOKEN_CLASS: Record<TokenType, string> = {
  literal: 'text-ice-100',
  slash: 'text-brand-indigo font-bold dark:text-[#8E83F0]',
  escape: 'text-muted',
  'block-open': 'text-brand-azure font-bold',
  'block-close': 'text-brand-azure font-bold',
  closure: 'text-brand-violet dark:text-[#B07EE0]',
  ws: '',
  string: 'text-success',
  number: 'text-warning',
  binding: 'text-accent font-semibold',
  property: 'text-accent',
  method: 'text-[#7A33B3] dark:text-[#C49BEA]',
  keyword: 'text-info italic',
  operator: 'text-muted',
  punct: 'text-muted',
  unknown: 'text-danger',
};

const SHARED =
  'font-mono text-[13px] leading-6 whitespace-pre-wrap break-all px-3 py-2.5 [tab-size:2] [font-variant-ligatures:none] [scrollbar-gutter:stable]';

/**
 * Monospace template editor: a transparent <textarea> over a syntax-highlighted mirror.
 * Issues (from /formats/validate or the local tokenizer) render as red (error) or amber
 * (warning) squiggles; hovering a squiggle shows its message.
 */
export const ExpressionEditor = forwardRef<ExpressionEditorHandle, ExpressionEditorProps>(function ExpressionEditor(
  { value, onChange, issues, id, className, onCaretChange, ...aria },
  ref,
) {
  const ta = useRef<HTMLTextAreaElement>(null);
  const mirror = useRef<HTMLPreElement>(null);
  const [hover, setHover] = useState<{ x: number; y: number; messages: string[]; severity: string } | null>(null);

  const tokens = useMemo(() => tokenize(value), [value]);
  const segments = useMemo(() => segmentTokens(tokens, issues, value.length), [tokens, issues, value.length]);

  const syncScroll = () => {
    if (ta.current && mirror.current) {
      mirror.current.scrollTop = ta.current.scrollTop;
      mirror.current.scrollLeft = ta.current.scrollLeft;
    }
  };

  // Auto-grow up to a limit.
  useLayoutEffect(() => {
    const el = ta.current;
    if (!el) return;
    el.style.height = '0px';
    el.style.height = `${Math.min(Math.max(el.scrollHeight, 72), 280)}px`;
    syncScroll();
  }, [value]);

  useImperativeHandle(ref, () => ({
    insert(text, opts) {
      const el = ta.current;
      if (!el) return;
      const start = el.selectionStart ?? value.length;
      const end = el.selectionEnd ?? start;
      const inBlock = isInsideBlock(value, start);
      const wrapped = opts?.binding && !inBlock ? `{${text}}` : text;
      const next = value.slice(0, start) + wrapped + value.slice(end);
      onChange(next);
      const lead = wrapped === text ? 0 : 1; // the "{" we added
      const caret = start + (opts?.caretOffset !== undefined ? lead + opts.caretOffset : wrapped.length);
      requestAnimationFrame(() => {
        el.focus();
        el.setSelectionRange(caret, caret);
        onCaretChange?.(caret);
      });
    },
    select(start, end) {
      const el = ta.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(start, end);
      onCaretChange?.(start);
    },
    focus() {
      ta.current?.focus();
    },
  }));

  const onMouseMove = (e: React.MouseEvent<HTMLTextAreaElement>) => {
    const el = ta.current;
    if (!el || issues.length === 0) {
      if (hover) setHover(null);
      return;
    }
    // Peek at the highlighted span under the pointer: briefly let the mirror (normally
    // pointer-events: none) take hits instead of the textarea.
    const m = mirror.current;
    el.style.pointerEvents = 'none';
    if (m) m.style.pointerEvents = 'auto';
    const under = document.elementFromPoint(e.clientX, e.clientY) as HTMLElement | null;
    el.style.pointerEvents = '';
    if (m) m.style.pointerEvents = '';
    const msg = under?.dataset.messages;
    if (msg) {
      setHover({ x: e.clientX, y: e.clientY, messages: msg.split('\n'), severity: under.dataset.severity ?? 'error' });
    } else if (hover) {
      setHover(null);
    }
  };

  const reportCaret = () => {
    if (ta.current) onCaretChange?.(ta.current.selectionStart ?? 0);
  };

  const tooltipStyle: CSSProperties | undefined = hover
    ? { left: Math.min(hover.x + 12, window.innerWidth - 320), top: hover.y + 18 }
    : undefined;

  return (
    <div
      className={cn(
        'relative rounded-lg border border-ink-800 bg-ink-950 transition-colors focus-within:border-ring focus-within:ring-2 focus-within:ring-ring/30',
        className,
      )}
    >
      <pre ref={mirror} aria-hidden className={cn(SHARED, 'pointer-events-none absolute inset-0 m-0 overflow-hidden')}>
        {segments.map((s) => (
          <span
            key={s.start}
            className={cn(
              TOKEN_CLASS[s.type],
              s.severity === 'error' && 'squiggle-error',
              s.severity === 'warning' && 'squiggle-warning',
            )}
            data-messages={s.messages.length ? s.messages.join('\n') : undefined}
            data-severity={s.severity}
          >
            {s.text}
          </span>
        ))}
        {/* keep the trailing newline visible */}
        {'\n'}
      </pre>
      <textarea
        ref={ta}
        id={id}
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          onCaretChange?.(e.target.selectionStart ?? 0);
        }}
        onScroll={syncScroll}
        onSelect={reportCaret}
        onKeyUp={reportCaret}
        onClick={reportCaret}
        onMouseMove={onMouseMove}
        onMouseLeave={() => setHover(null)}
        spellCheck={false}
        autoCapitalize="off"
        autoComplete="off"
        autoCorrect="off"
        className={cn(
          SHARED,
          'relative block w-full resize-none bg-transparent text-transparent caret-ice-100 outline-none',
          'selection:bg-brand-sky/30 selection:text-transparent',
        )}
        {...aria}
      />
      {hover &&
        createPortal(
          <div
            role="tooltip"
            style={tooltipStyle}
            className={cn(
              'pointer-events-none fixed z-[100] max-w-xs rounded-md border bg-ink-850 px-2 py-1 text-xs shadow-raised',
              hover.severity === 'error' ? 'border-danger/40 text-danger' : 'border-warning/40 text-warning',
            )}
          >
            {hover.messages.map((m) => (
              <p key={m}>{m}</p>
            ))}
          </div>,
          document.body,
        )}
    </div>
  );
});
