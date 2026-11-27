import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Search } from 'lucide-react';
import { useHistory } from '@/api/hooks';
import type { HistoryQuery } from '@/api/keys';
import type { HistoryEntry, HistoryEntryType, HistoryFilter } from '@/api/types';
import { cn } from '@/lib/cn';
import { formatBytes, formatClock } from '@/lib/format';
import { useDebounced, useDocumentTitle } from '@/lib/hooks';
import { itemLink } from '@/lib/routes';
import { Button, ButtonLink, EmptyState, ErrorState, PathText, Skeleton, controlBase } from '@/components/ui';
import { groupByDay } from './days';
import { UndoOperationButton } from './UndoOperationButton';

const FILTERS: { value: HistoryFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'moved', label: 'Moved' },
  { value: 'skipped', label: 'Skipped' },
  { value: 'problems', label: 'Problems' },
];

type KindChoice = NonNullable<HistoryQuery['kind']> | 'all';
const KINDS: { value: KindChoice; label: string }[] = [
  { value: 'all', label: 'All types' },
  { value: 'movie', label: 'Movies' },
  { value: 'series', label: 'TV' },
  { value: 'anime', label: 'Anime' },
];

const isFilter = (v: string | null): v is HistoryFilter => FILTERS.some((f) => f.value === v);
const isKind = (v: string | null): v is NonNullable<HistoryQuery['kind']> => v === 'movie' || v === 'series' || v === 'anime';

const BADGE: Record<HistoryEntryType, { label: string; className: string }> = {
  moved: { label: 'Moved', className: 'bg-success/10 text-success' },
  skipped: { label: 'Skipped', className: 'bg-warning/10 text-warning' },
  problem: { label: 'Problem', className: 'bg-danger/10 text-danger' },
  undone: { label: 'Undone', className: 'bg-ink-800 text-muted' },
};

/** History: every file Schema touched, newest first, grouped by day. Filters live in the URL. */
export function HistoryPage() {
  useDocumentTitle('History');
  const [params, setParams] = useSearchParams();
  const searchId = useId();
  const kindId = useId();

  const rawFilter = params.get('filter');
  const filter: HistoryFilter = isFilter(rawFilter) ? rawFilter : 'all';
  const rawKind = params.get('kind');
  const kind = isKind(rawKind) ? rawKind : undefined;
  const urlQ = params.get('q') ?? '';

  // The search box writes `?q=` (debounced); `?q=` from elsewhere (the top bar) fills the box.
  const [text, setText] = useState(urlQ);
  const debounced = useDebounced(text, 250);
  const lastWritten = useRef(urlQ);
  useEffect(() => {
    if (urlQ === lastWritten.current) return;
    lastWritten.current = urlQ;
    setText(urlQ);
  }, [urlQ]);
  useEffect(() => {
    const next = debounced.trim();
    if (next === urlQ.trim()) return;
    lastWritten.current = next;
    setParams(
      (cur) => {
        const p = new URLSearchParams(cur);
        if (next) p.set('q', next);
        else p.delete('q');
        return p;
      },
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounced]);

  const setParam = (key: string, value: string | null) =>
    setParams(
      (cur) => {
        const p = new URLSearchParams(cur);
        if (value) p.set(key, value);
        else p.delete(key);
        return p;
      },
      { replace: true },
    );

  const query: HistoryQuery = { filter, q: urlQ.trim() || undefined, kind };
  const history = useHistory(query);
  const entries = useMemo(() => history.data?.pages.flatMap((p) => p.entries) ?? [], [history.data]);
  const days = useMemo(() => groupByDay(entries), [entries]);
  const filtered = filter !== 'all' || !!query.q || !!kind;

  return (
    <div className="mx-auto flex w-full max-w-[1040px] flex-col gap-6 px-4 pb-16 pt-7 md:px-6 md:pt-9">
      <div>
        <h1 className="text-[30px] font-bold leading-tight tracking-[-0.02em]">History</h1>
        <p className="mt-1 text-[15px] text-muted">
          Every file Schema has touched, newest first. Undo puts a file back exactly where it was.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <div role="group" aria-label="Show" className="flex flex-wrap gap-0.5 rounded-[10px] border border-ink-800 bg-ink-900 p-[3px]">
          {FILTERS.map((f) => {
            const on = f.value === filter;
            return (
              <button
                key={f.value}
                type="button"
                aria-pressed={on}
                onClick={() => setParam('filter', f.value === 'all' ? null : f.value)}
                className={cn(
                  'h-10 rounded-[7px] px-3.5 text-sm transition-colors',
                  on ? 'bg-ink-800 font-semibold text-ice-100' : 'font-medium text-muted hover:text-ice-100',
                )}
              >
                {f.label}
              </button>
            );
          })}
        </div>
        <div className="relative min-w-0 flex-[1_1_260px]">
          <label htmlFor={searchId} className="sr-only">
            Search history
          </label>
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" aria-hidden />
          <input
            id={searchId}
            type="search"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Search by title or file name"
            className={cn(controlBase, 'h-10 pl-9 pr-3 text-sm')}
          />
        </div>
        <div className="w-full sm:w-40">
          <label htmlFor={kindId} className="sr-only">
            Media type
          </label>
          <select
            id={kindId}
            value={kind ?? 'all'}
            onChange={(e) => setParam('kind', e.target.value === 'all' ? null : e.target.value)}
            className={cn(controlBase, 'h-10 px-2.5 text-sm')}
          >
            {KINDS.map((k) => (
              <option key={k.value} value={k.value}>
                {k.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {history.isPending ? (
        <HistorySkeleton />
      ) : history.isError && entries.length === 0 ? (
        <ErrorState error={history.error} onRetry={() => void history.refetch()} title="Could not load history" />
      ) : entries.length === 0 ? (
        <EmptyState
          title={filtered ? 'Nothing matches these filters' : 'Nothing here yet'}
          description={filtered ? 'Try another filter or search.' : 'Files Schema moves, skips or has trouble with show up here.'}
        />
      ) : (
        <>
          {days.map((day) => (
            <section key={day.key} aria-labelledby={`day-${day.key}`} className="flex flex-col gap-2">
              <h2 id={`day-${day.key}`} className="mt-2 text-[13px] font-semibold uppercase tracking-[0.06em] text-muted">
                {day.label}
              </h2>
              <ul className="rounded-xl border border-ink-800 bg-ink-900">
                {day.entries.map((e) => (
                  <HistoryRow key={e.key} entry={e} />
                ))}
              </ul>
            </section>
          ))}
          {history.hasNextPage && (
            <Button
              variant="outline"
              size="lg"
              className="self-center px-[18px] text-sm"
              loading={history.isFetchingNextPage}
              onClick={() => void history.fetchNextPage()}
            >
              Show older
            </Button>
          )}
        </>
      )}
    </div>
  );
}

function HistoryRow({ entry: e }: { entry: HistoryEntry }) {
  const badge = BADGE[e.type];
  const meta = [e.bytes > 0 ? formatBytes(e.bytes) : null, formatClock(e.ts)].filter(Boolean).join(' · ');
  return (
    <li className="flex flex-wrap items-center gap-x-3.5 gap-y-2 border-b border-ink-800/70 px-4 py-3.5 last:border-b-0 md:px-5">
      <span className={cn('min-w-[76px] flex-none rounded-full px-2.5 py-0.5 text-center text-xs font-semibold', badge.className)}>
        {badge.label}
      </span>
      <div className="min-w-0 flex-[1_1_420px]">
        <div className="font-semibold [overflow-wrap:anywhere]">{e.title}</div>
        <PathText path={e.source} wrap className="block text-muted" />
        {e.destination && (
          <div className="flex gap-1.5">
            <span className="font-mono text-xs leading-5 text-accent" aria-label="moved to">
              →
            </span>
            <PathText path={e.destination} wrap className={cn('min-w-0', e.type === 'undone' && 'line-through opacity-70')} />
          </div>
        )}
        {e.detail && <div className="text-xs text-muted">{e.detail}</div>}
      </div>
      <span className="min-w-[120px] text-[13px] text-muted md:text-right">{meta}</span>
      {e.can_undo && e.operation_id !== null ? (
        <UndoOperationButton operationId={e.operation_id} title={e.title} destination={e.destination} source={e.source} />
      ) : e.can_fix && e.job_id !== null ? (
        <ButtonLink to={itemLink(e.job_id, e.item_id)} variant="outline" size="lg" className="px-3.5 text-sm" aria-label={`Fix ${e.title}`}>
          Fix
        </ButtonLink>
      ) : null}
    </li>
  );
}

function HistorySkeleton() {
  return (
    <div className="flex flex-col gap-6" aria-busy="true" aria-label="Loading history">
      {[1, 3].map((rows, i) => (
        <div key={i} className="flex flex-col gap-2">
          <Skeleton className="mt-2 h-3.5 w-24" />
          <div className="rounded-xl border border-ink-800 bg-ink-900">
            {Array.from({ length: rows }, (_, j) => (
              <div key={j} className="flex items-center gap-3.5 border-b border-ink-800/70 px-5 py-4 last:border-b-0">
                <Skeleton className="h-5 w-[76px] rounded-full" />
                <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                  <Skeleton className="h-4 w-56 max-w-full" />
                  <Skeleton className="h-3 w-full" />
                  <Skeleton className="h-3 w-4/5" />
                </div>
                <Skeleton className="hidden h-10 w-16 md:block" />
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
