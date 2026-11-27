/**
 * Settings-page building blocks: section card, label/help + control row, and the autosaving
 * controls (segmented buttons, radio cards, chips, path pairs, connection rows, text fields).
 * Generic UI primitives come from `@/components/ui`; only Settings-specific ones live here.
 */
import {
  useEffect,
  useRef,
  useState,
  type FocusEvent,
  type KeyboardEvent,
  type ReactNode,
} from 'react';
import { AlertTriangle, ArrowRight, Check, FolderSearch, Loader2, Plus, X } from 'lucide-react';
import { Button, Dot, ErrorState, Input, Select, Skeleton, Switch, type SelectOption } from '@/components/ui';
import { cn } from '@/lib/cn';
import type { Tone } from '@/lib/labels';
import { FileBrowser } from '@/features/import/FileBrowser';
import { useSaveState } from './api';
import { addChips, normalizePattern, rowIds, type ChipCheck } from './mapping';

/* ================================================================== section + row */

export function SettingsSection({
  id,
  num,
  title,
  description,
  children,
}: {
  id: string;
  num: number;
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section
      id={id}
      aria-labelledby={`${id}-h`}
      className="surface min-w-0 scroll-mt-4 overflow-hidden rounded-2xl"
    >
      <header className="border-b border-ink-800 px-4 pb-4 pt-5 md:px-6">
        <h2 id={`${id}-h`} tabIndex={-1} className="text-[17px] font-semibold text-ice-100 outline-none md:text-lg">
          {num} · {title}
        </h2>
        <p className="mt-0.5 text-[13px] text-muted md:text-sm">{description}</p>
      </header>
      <div className="divide-y divide-ink-800">{children}</div>
    </section>
  );
}

export type RowWidth = 'narrow' | 'wide' | 'auto';

/**
 * One option: label + help on the left, control on the right; wraps to stacked on phones.
 * `group` rows (segmented, radio, chips …) label their control with aria-labelledby instead of
 * <label for>, which only works for form elements.
 */
export function Row({
  id,
  label,
  help,
  children,
  width = 'narrow',
  group = false,
  aside,
  className,
}: {
  id: string;
  label: ReactNode;
  help?: ReactNode;
  children: ReactNode;
  width?: RowWidth;
  group?: boolean;
  /** Extra content under the help text (status, warnings). */
  aside?: ReactNode;
  className?: string;
}) {
  const { labelId, helpId } = rowIds(id);
  return (
    <div className={cn('flex flex-wrap items-start gap-x-7 gap-y-3 px-4 py-4 md:px-6', className)}>
      <div className="min-w-0 flex-[1_1_280px]">
        {group ? (
          <span id={labelId} className="text-sm font-semibold text-ice-100">
            {label}
          </span>
        ) : (
          <label id={labelId} htmlFor={id} className="text-sm font-semibold text-ice-100">
            {label}
          </label>
        )}
        {help && (
          <p id={helpId} className="mt-0.5 text-[13px] leading-5 text-muted">
            {help}
          </p>
        )}
        {aside}
      </div>
      <div
        className={cn(
          'min-w-0',
          width === 'narrow' && 'flex-[1_1_320px]',
          width === 'wide' && 'basis-full',
          width === 'auto' && 'flex-none',
        )}
      >
        {children}
      </div>
    </div>
  );
}

/** A row whose control is a single on/off switch. */
export function ToggleRow({
  id,
  label,
  help,
  checked,
  onChange,
  disabled,
}: {
  id: string;
  label: ReactNode;
  help: ReactNode;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <Row id={id} width="auto" label={label} help={help}>
      <Switch id={id} aria-describedby={`${id}-help`} checked={checked} onChange={onChange} disabled={disabled} />
    </Row>
  );
}

/** Inline loading placeholder shaped like a few rows. */
export function RowSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div aria-busy="true" aria-label="Loading" className="divide-y divide-ink-800">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex flex-wrap gap-x-7 gap-y-3 px-4 py-4 md:px-6">
          <div className="flex min-w-0 flex-[1_1_280px] flex-col gap-2">
            <Skeleton className="h-4 w-40" />
            <Skeleton className="h-3 w-4/5" />
          </div>
          <Skeleton className="h-9 min-w-0 flex-[1_1_320px]" />
        </div>
      ))}
    </div>
  );
}

export function SectionError({ error, onRetry, what }: { error: unknown; onRetry: () => void; what: string }) {
  return (
    <div className="px-4 md:px-6">
      <ErrorState compact error={error} onRetry={onRetry} title={`Could not load ${what}`} />
    </div>
  );
}

/** Sub-heading inside a section (one per watch folder / media server). */
export function SubHeader({ children, actions }: { children: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 bg-ink-950/40 px-4 py-2.5 md:px-6">
      <div className="min-w-0 flex-1 text-[13px] font-semibold text-ice-100">{children}</div>
      {actions && <div className="flex flex-wrap items-center gap-1.5">{actions}</div>}
    </div>
  );
}

/* ================================================================== segmented */

export interface SegOption<V extends string> {
  value: V;
  label: string;
}

export function Segmented<V extends string>({
  id,
  options,
  value,
  onChange,
  labelledBy,
  describedBy,
  disabled,
  size = 'md',
}: {
  id?: string;
  options: readonly SegOption<V>[];
  value: V;
  onChange: (v: V) => void;
  labelledBy?: string;
  describedBy?: string;
  disabled?: boolean;
  size?: 'sm' | 'md';
}) {
  return (
    <div
      id={id}
      role="group"
      aria-labelledby={labelledBy}
      aria-describedby={describedBy}
      className="inline-flex max-w-full flex-wrap gap-0.5 rounded-xl border border-ink-700 bg-ink-950 p-[3px]"
    >
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            aria-pressed={on}
            disabled={disabled}
            onClick={() => !on && onChange(o.value)}
            className={cn(
              'rounded-lg px-3 font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50',
              size === 'md' ? 'min-h-9 text-[13px]' : 'min-h-7 text-xs',
              on ? 'bg-accent font-semibold text-ink-950' : 'text-muted hover:bg-ink-850 hover:text-ice-100',
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

/* ================================================================== radio cards */

export function RadioCards<V extends string>({
  id,
  name,
  options,
  value,
  onChange,
  labelledBy,
  describedBy,
}: {
  id: string;
  name: string;
  options: readonly { value: V; label: string; description: string }[];
  value: V;
  onChange: (v: V) => void;
  labelledBy?: string;
  describedBy?: string;
}) {
  return (
    <div
      id={id}
      role="radiogroup"
      aria-labelledby={labelledBy}
      aria-describedby={describedBy}
      className="grid grid-cols-1 gap-2 sm:grid-cols-2"
    >
      {options.map((o) => {
        const on = o.value === value;
        return (
          <label
            key={o.value}
            className={cn(
              'flex cursor-pointer items-start gap-2.5 rounded-xl border p-3 transition-colors',
              'focus-within:ring-2 focus-within:ring-ring/50',
              on ? 'border-accent bg-accent/10' : 'border-ink-700 hover:border-slate-400/60',
            )}
          >
            <input
              type="radio"
              name={name}
              value={o.value}
              checked={on}
              onChange={() => onChange(o.value)}
              className="mt-0.5 h-4 w-4 shrink-0 accent-[rgb(var(--accent))] focus:outline-none"
            />
            <span className="min-w-0">
              <span className="block text-sm font-semibold text-ice-100">{o.label}</span>
              <span className="block text-[13px] leading-5 text-muted">{o.description}</span>
            </span>
          </label>
        );
      })}
    </div>
  );
}

/* ================================================================== text / path */

/**
 * Text input that saves on blur or Enter (never on every keystroke). Escape restores the saved
 * value. With `browse`, a Browse button opens the server folder picker below the input.
 */
export function TextSetting({
  id,
  value,
  onCommit,
  placeholder,
  mono = true,
  validate,
  browse = false,
  describedBy,
  allowEmpty = true,
  className,
}: {
  id: string;
  value: string;
  onCommit: (v: string) => void;
  placeholder?: string;
  mono?: boolean;
  validate?: (v: string) => string | null;
  browse?: boolean;
  describedBy?: string;
  allowEmpty?: boolean;
  className?: string;
}) {
  const [text, setText] = useState(value);
  const [error, setError] = useState<string | null>(null);
  const [browsing, setBrowsing] = useState(false);
  useEffect(() => setText(value), [value]);

  const commit = (raw: string) => {
    const v = raw.trim();
    if (v === value.trim()) {
      setError(null);
      return;
    }
    if (!v && !allowEmpty) {
      setError('This can’t be empty.');
      return;
    }
    const problem = v ? (validate?.(v) ?? null) : null;
    setError(problem);
    if (!problem) onCommit(v);
  };

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      commit(text);
    } else if (e.key === 'Escape') {
      setText(value);
      setError(null);
    }
  };

  const errId = `${id}-err`;
  return (
    <div className={cn('flex flex-col gap-1.5', className)}>
      <div className="flex gap-2">
        <Input
          id={id}
          mono={mono}
          className="min-w-0 flex-1 [&_input]:h-10"
          value={text}
          placeholder={placeholder}
          invalid={!!error}
          aria-describedby={[describedBy, error ? errId : null].filter(Boolean).join(' ') || undefined}
          onChange={(e) => setText(e.target.value)}
          onBlur={() => commit(text)}
          onKeyDown={onKey}
          spellCheck={false}
          autoComplete="off"
        />
        {browse && (
          <Button
            variant="outline"
            className="h-10"
            leftIcon={<FolderSearch className="h-3.5 w-3.5" />}
            aria-expanded={browsing}
            aria-controls={`${id}-browser`}
            onClick={() => setBrowsing((b) => !b)}
          >
            Browse
          </Button>
        )}
      </div>
      {error && (
        <p id={errId} role="alert" className="text-xs text-danger">
          {error}
        </p>
      )}
      {browse && browsing && (
        <div id={`${id}-browser`} className="overflow-hidden rounded-lg border border-ink-800">
          <FileBrowser
            dirsOnly
            selected={new Set()}
            onToggle={() => undefined}
            maxHeight="260px"
            onPickDir={(p) => {
              setBrowsing(false);
              setText(p);
              commit(p);
            }}
          />
        </div>
      )}
    </div>
  );
}

/* ================================================================== chips */

export function ChipsEditor({
  id,
  value,
  onChange,
  normalize = normalizePattern,
  check,
  split = true,
  placeholder,
  labelledBy,
  describedBy,
  mono = true,
  noun = 'item',
}: {
  id: string;
  value: readonly string[];
  onChange: (next: string[]) => void;
  normalize?: (s: string) => string;
  check?: ChipCheck;
  /** Split typed text on spaces/commas into several chips (off for paths). */
  split?: boolean;
  placeholder?: string;
  labelledBy?: string;
  describedBy?: string;
  mono?: boolean;
  noun?: string;
}) {
  const [adding, setAdding] = useState(false);
  const [text, setText] = useState('');
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const addRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (adding) inputRef.current?.focus();
  }, [adding]);

  const tryAdd = (close: boolean) => {
    if (!text.trim()) {
      setError(null);
      if (close) setAdding(false);
      return;
    }
    const { next, error: problem } = addChips(value, text, normalize, check, split);
    setError(problem);
    const added = next.length !== value.length;
    if (added) onChange(next);
    // Clear what was accepted; keep the text only when nothing could be added.
    if (!problem || added) setText('');
    if (!problem && close) setAdding(false);
  };

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      tryAdd(false);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      setText('');
      setError(null);
      setAdding(false);
      addRef.current?.focus();
    }
  };

  const errId = `${id}-err`;
  return (
    <div className="flex flex-col gap-1.5">
      <ul
        id={id}
        aria-labelledby={labelledBy}
        aria-describedby={describedBy}
        className="flex flex-wrap items-center gap-1.5"
      >
        {value.map((v) => (
          <li
            key={v}
            className={cn(
              'inline-flex min-h-8 items-center gap-1 rounded-lg border border-ink-700 bg-ink-950 pl-2.5 pr-1 text-[13px]',
              mono && 'font-mono text-xs',
            )}
          >
            <span className="break-all">{v}</span>
            <button
              type="button"
              aria-label={`Remove ${v}`}
              onClick={() => onChange(value.filter((x) => x !== v))}
              className="flex h-6 w-6 items-center justify-center rounded-md text-muted hover:bg-ink-850 hover:text-ice-100"
            >
              <X className="h-3.5 w-3.5" aria-hidden />
            </button>
          </li>
        ))}
        <li className="inline-flex">
          {adding ? (
            <input
              ref={inputRef}
              value={text}
              onChange={(e) => {
                setText(e.target.value);
                setError(null);
              }}
              onKeyDown={onKey}
              onBlur={() => tryAdd(true)}
              placeholder={placeholder}
              aria-label={`Add ${noun}`}
              aria-invalid={!!error || undefined}
              aria-describedby={error ? errId : undefined}
              spellCheck={false}
              autoComplete="off"
              className={cn(
                'h-8 w-40 rounded-lg border border-ink-700 bg-ink-950 px-2.5 text-ice-100 placeholder:text-muted/70',
                'focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/30 aria-[invalid=true]:border-danger',
                mono ? 'font-mono text-xs' : 'text-[13px]',
              )}
            />
          ) : (
            <button
              ref={addRef}
              type="button"
              onClick={() => setAdding(true)}
              className="inline-flex min-h-8 items-center gap-1 rounded-lg border border-dashed border-ink-700 px-2.5 text-[13px] font-medium text-muted hover:border-slate-400/60 hover:text-ice-100"
            >
              <Plus className="h-3.5 w-3.5" aria-hidden />
              Add
            </button>
          )}
        </li>
      </ul>
      {error && (
        <p id={errId} role="alert" className="text-xs text-danger">
          {error}
        </p>
      )}
    </div>
  );
}

/* ================================================================== pairs */

export interface Pair {
  a: string;
  b: string;
}

const tidy = (rows: Pair[]) =>
  rows.map((p) => ({ a: p.a.trim(), b: p.b.trim() })).filter((p) => p.a.length > 0 && p.b.length > 0);

/**
 * "left → right" rows. Edits stay local until a row loses focus; only complete rows are saved.
 * Removing a row or changing a select saves straight away.
 */
export function PairsEditor({
  id,
  leftHead,
  rightHead,
  value,
  onCommit,
  leftPlaceholder,
  rightPlaceholder,
  rightOptions,
  defaultRight = '',
  labelledBy,
  describedBy,
}: {
  id: string;
  leftHead: string;
  rightHead: string;
  value: Pair[];
  onCommit: (rows: Pair[]) => void;
  leftPlaceholder?: string;
  rightPlaceholder?: string;
  /** Render the right side as a select instead of a text input. */
  rightOptions?: readonly SelectOption[];
  defaultRight?: string;
  labelledBy?: string;
  describedBy?: string;
}) {
  const [rows, setRows] = useState<Pair[]>(value);
  const committed = useRef(JSON.stringify(value));
  const firstInputs = useRef<(HTMLInputElement | null)[]>([]);
  const focusNew = useRef(false);

  useEffect(() => {
    const s = JSON.stringify(value);
    if (s !== committed.current) {
      committed.current = s;
      setRows(value);
    }
  }, [value]);

  useEffect(() => {
    if (focusNew.current) {
      focusNew.current = false;
      firstInputs.current[rows.length - 1]?.focus();
    }
  }, [rows.length]);

  const commit = (next: Pair[]) => {
    const clean = tidy(next);
    const s = JSON.stringify(clean);
    if (s === committed.current) return;
    committed.current = s;
    onCommit(clean);
  };

  const shown = rows.length ? rows : [{ a: '', b: rightOptions ? defaultRight : '' }];
  const setRow = (i: number, patch: Partial<Pair>) =>
    setRows(shown.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  const onRowBlur = (e: FocusEvent<HTMLDivElement>) => {
    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) commit(shown);
  };

  return (
    <div id={id} role="group" aria-labelledby={labelledBy} aria-describedby={describedBy} className="flex flex-col gap-2">
      <div className="hidden gap-2 text-xs text-muted sm:flex" aria-hidden>
        <span className="flex-1">{leftHead}</span>
        <span className="w-4" />
        <span className="flex-1">{rightHead}</span>
        <span className="w-8" />
      </div>
      {shown.map((p, i) => (
        <div key={i} className="flex flex-wrap items-center gap-2 sm:flex-nowrap" onBlur={onRowBlur}>
          <Input
            ref={(el) => {
              firstInputs.current[i] = el;
            }}
            mono
            className="min-w-0 flex-[1_1_140px] [&_input]:h-10"
            value={p.a}
            placeholder={leftPlaceholder}
            aria-label={`${leftHead} ${i + 1}`}
            onChange={(e) => setRow(i, { a: e.target.value })}
            onKeyDown={(e) => e.key === 'Enter' && commit(shown)}
            spellCheck={false}
            autoComplete="off"
          />
          <ArrowRight className="h-4 w-4 shrink-0 text-accent" aria-hidden />
          {rightOptions ? (
            <Select
              className="min-w-0 flex-[1_1_140px] [&_select]:h-10"
              value={p.b || defaultRight}
              options={rightOptions}
              aria-label={`${rightHead} ${i + 1}`}
              onChange={(v) => {
                const next = shown.map((r, j) => (j === i ? { ...r, b: v } : r));
                setRows(next);
                commit(next);
              }}
            />
          ) : (
            <Input
              mono
              className="min-w-0 flex-[1_1_140px] [&_input]:h-10"
              value={p.b}
              placeholder={rightPlaceholder}
              aria-label={`${rightHead} ${i + 1}`}
              onChange={(e) => setRow(i, { b: e.target.value })}
              onKeyDown={(e) => e.key === 'Enter' && commit(shown)}
              spellCheck={false}
              autoComplete="off"
            />
          )}
          <button
            type="button"
            aria-label={`Remove row ${i + 1}`}
            onClick={() => {
              const next = shown.filter((_, j) => j !== i);
              setRows(next);
              commit(next);
            }}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-ink-850 hover:text-ice-100"
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>
      ))}
      <div>
        <button
          type="button"
          onClick={() => {
            focusNew.current = true;
            setRows([...shown, { a: '', b: rightOptions ? defaultRight : '' }]);
          }}
          className="inline-flex min-h-8 items-center gap-1 rounded-lg border border-dashed border-ink-700 px-2.5 text-[13px] font-medium text-muted hover:border-slate-400/60 hover:text-ice-100"
        >
          <Plus className="h-3.5 w-3.5" aria-hidden />
          Add
        </button>
      </div>
    </div>
  );
}

/* ================================================================== connection row control */

export function ConnStatus({
  id,
  tone,
  status,
  children,
  detail,
}: {
  id?: string;
  tone: Tone;
  status: ReactNode;
  /** Buttons. */
  children?: ReactNode;
  /** Second line (error message, test result). */
  detail?: ReactNode;
}) {
  return (
    <div id={id} className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2.5">
        <Dot tone={tone} className="h-2.5 w-2.5" />
        <span className="min-w-0 flex-[1_1_140px] text-[13px] text-ice-100/90" role="status">
          {status}
        </span>
        {children && <div className="flex flex-wrap gap-1.5">{children}</div>}
      </div>
      {detail}
    </div>
  );
}

/* ================================================================== save indicator */

/**
 * Live save state for the page header: Saving… / Saved / Couldn't save + Retry. Empty while idle,
 * because the app's top bar already says "Changes save automatically" on /settings.
 */
export function SaveIndicator() {
  const { status, retry } = useSaveState();
  return (
    <div role="status" aria-live="polite" className="flex min-h-8 items-center gap-2 text-[13px] text-muted">
      {status === 'saving' ? (
        <>
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
          Saving…
        </>
      ) : status === 'saved' ? (
        <>
          <Check className="h-3.5 w-3.5 text-success" strokeWidth={2.5} aria-hidden />
          <span className="text-ice-100">Saved</span>
        </>
      ) : status === 'error' ? (
        <>
          <AlertTriangle className="h-3.5 w-3.5 text-danger" aria-hidden />
          <span className="text-danger">Couldn’t save</span>
          {retry && (
            <Button size="xs" variant="outline" onClick={retry}>
              Retry
            </Button>
          )}
        </>
      ) : null}
    </div>
  );
}
