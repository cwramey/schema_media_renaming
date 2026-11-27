import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useVirtualizer } from '@tanstack/react-virtual';
import {
  ArrowLeft,
  Ban,
  Check,
  CircleSlash,
  Keyboard,
  PanelRightClose,
  PanelRightOpen,
  Play,
  RefreshCcw,
  Search,
  ShieldQuestion,
  Trash2,
  X,
} from 'lucide-react';
import {
  useBulkItems,
  useCancelJob,
  useDeleteJob,
  useExecuteJob,
  useItem,
  useJob,
  useJobItems,
  useRematchJob,
  useSettings,
  useUpdateItem,
} from '@/api/hooks';
import { ApiError } from '@/api/client';
import type { BulkAction, ExecuteRequest, JobItemOut, RenameMode } from '@/api/types';
import { RENAME_MODES } from '@/api/types';
import { isActiveJob } from '@/api/wsReducer';
import { cn } from '@/lib/cn';
import { useDebounced, useDocumentTitle, useHotkeys, useIsPhone, useLocalStorage, useMediaQuery } from '@/lib/hooks';
import { CONFLICT_INFO, MODE_INFO, SOURCE_LABEL } from '@/lib/labels';
import { formatRelative } from '@/lib/format';
import {
  Button,
  ButtonLink,
  Checkbox,
  ConfirmDialog,
  Dialog,
  Drawer,
  EmptyState,
  ErrorState,
  IconButton,
  Input,
  JobStatusPill,
  Kbd,
  Menu,
  ProgressBar,
  Select,
  Skeleton,
  SkeletonRows,
  Tabs,
  Tooltip,
} from '@/components/ui';
import { countsSummary, jobTitle } from '@/features/shared/helpers';
import { Inspector } from './workspace/Inspector';
import { GRID_COLS, ItemCard, ItemRow } from './workspace/ItemRow';
import {
  displayRoots,
  filterCountsFromJob,
  FILTER_LABELS,
  findLoadedItem,
  isExecutable,
  itemFilterOf,
  visibleRows,
  type ItemFilter,
} from './workspace/model';

const FILTERS: ItemFilter[] = ['all', 'review', 'matched', 'skipped', 'done', 'error'];

export default function WorkspacePage() {
  const { id: idParam } = useParams();
  const id = Number(idParam);
  const navigate = useNavigate();
  // `?item=<id>` (Home "Choose match", History "Fix"): open the job on that item.
  const [searchParams] = useSearchParams();
  const focusItemId = parseItemParam(searchParams.get('item'));
  const job = useJob(Number.isFinite(id) ? id : undefined);
  const settings = useSettings();
  const phone = useIsPhone();
  const wide = useMediaQuery('(min-width: 1280px)');

  const update = useUpdateItem();
  const bulk = useBulkItems();
  const execute = useExecuteJob();
  const cancel = useCancelJob();
  const rematchJob = useRematchJob();
  const del = useDeleteJob();

  const [filter, setFilter] = useState<ItemFilter>('all');
  const [text, setText] = useState('');
  const q = useDebounced(text, 120);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  /** "Every item matching the tab + search" (server-side), including pages not loaded yet. */
  const [allMatching, setAllMatching] = useState(false);
  const [activeId, setActiveId] = useState<number | null>(focusItemId);
  const [inspectorPref, setInspectorPref] = useLocalStorage('schema.workspace.inspector', true);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [modeOverride, setModeOverride] = useState<RenameMode | null>(null);
  const [confirmExec, setConfirmExec] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [showKeys, setShowKeys] = useState(false);
  const anchor = useRef<number | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const inspectorRef = useRef<HTMLDivElement>(null);

  const detail = job.data;
  useDocumentTitle(detail ? jobTitle(detail) : 'Job');
  // Items are filtered and paged server-side (slim rows); the header counts cover the whole job.
  const itemFilter = useMemo(() => itemFilterOf(filter, q), [filter, q]);
  const list = useJobItems(Number.isFinite(id) ? id : undefined, itemFilter);
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = list;
  const pages = list.data?.pages;
  const matchingTotal = pages?.[0]?.total ?? 0;
  const counts = useMemo(
    () => (detail ? filterCountsFromJob(detail) : { all: 0, review: 0, matched: 0, skipped: 0, done: 0, error: 0 }),
    [detail],
  );
  // Rows whose status changed since they were fetched drop out of the tab right away. Only pages a
  // push changed are filtered again; rows work out their own display path (memoized per item).
  const rows = useMemo(() => visibleRows(pages, filter), [pages, filter]);
  const rowIndex = useMemo(() => new Map(rows.map((r, i) => [r.id, i])), [rows]);
  const destRoot = detail?.dest_root;
  const roots = useMemo(() => displayRoots(destRoot, settings.data), [destRoot, settings.data]);
  const jobMode = detail?.mode ?? 'move';

  const activeIndex = activeId === null ? -1 : (rowIndex.get(activeId) ?? -1);
  const activeRow: JobItemOut | null = activeIndex >= 0 ? rows[activeIndex] : findLoadedItem(pages, activeId);
  // The inspector needs every candidate: fetch the full item, showing the slim row meanwhile.
  const full = useItem(activeId, activeRow);
  const activeItem: JobItemOut | null = full.data ?? activeRow;
  const inlineInspector = wide && inspectorPref;

  // Pick the first row that needs attention when the job loads.
  useEffect(() => {
    if (activeId !== null || rows.length === 0) return;
    const firstReview = rows.find((r) => r.status === 'review');
    setActiveId((firstReview ?? rows[0]).id);
  }, [rows, activeId]);

  // A selection belongs to one tab + search: start over when either changes.
  useEffect(() => {
    setSelected((cur) => (cur.size === 0 ? cur : new Set()));
    setAllMatching(false);
    anchor.current = null;
  }, [filter, q]);

  // Drop selections that are no longer listed (gone, or moved out of this tab).
  useEffect(() => {
    setSelected((cur) => {
      if (cur.size === 0) return cur;
      const next = new Set([...cur].filter((x) => rowIndex.has(x)));
      return next.size === cur.size ? cur : next;
    });
  }, [rowIndex]);

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => (phone ? 112 : 32),
    overscan: 12,
    measureElement: phone ? (el) => el.getBoundingClientRect().height : undefined,
  });

  useEffect(() => {
    virtualizer.measure();
  }, [phone, virtualizer]);

  // Infinite scroll: fetch the next page when the rendered window nears the end of what is loaded.
  const virtualRows = virtualizer.getVirtualItems();
  const lastRendered = virtualRows.length ? virtualRows[virtualRows.length - 1].index : -1;
  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage && lastRendered >= rows.length - 40) void fetchNextPage();
  }, [lastRendered, rows.length, hasNextPage, isFetchingNextPage, fetchNextPage]);

  // Deep link: select the requested item, scroll it into view once it is loaded, and on narrow
  // screens open the inspector sheet (the full item is fetched even if its page is not loaded).
  const focusedRef = useRef<{ id: number | null; scrolled: boolean; opened: boolean }>({ id: null, scrolled: false, opened: false });
  useEffect(() => {
    if (focusItemId === null) return;
    const f = focusedRef.current;
    if (f.id !== focusItemId) {
      focusedRef.current = { id: focusItemId, scrolled: false, opened: false };
      setActiveId(focusItemId);
    }
    const cur = focusedRef.current;
    if (!cur.opened && !inlineInspector) {
      cur.opened = true;
      setSheetOpen(true);
    }
    if (!cur.scrolled) {
      const idx = rowIndex.get(focusItemId);
      if (idx !== undefined) {
        cur.scrolled = true;
        virtualizer.scrollToIndex(idx, { align: 'center' });
      }
    }
  }, [focusItemId, inlineInspector, rowIndex, virtualizer]);

  const focusRow = useCallback(
    (index: number) => {
      const it = rows[index];
      if (!it) return;
      setActiveId(it.id);
      virtualizer.scrollToIndex(index, { align: 'auto' });
    },
    [rows, virtualizer],
  );

  const openInspector = useCallback(
    (focusSearch = false) => {
      if (inlineInspector) {
        requestAnimationFrame(() => {
          if (focusSearch) searchRef.current?.focus();
          else inspectorRef.current?.focus();
        });
      } else {
        setSheetOpen(true);
        if (focusSearch) setTimeout(() => searchRef.current?.focus(), 60);
      }
    },
    [inlineInspector],
  );

  function activate(index: number, e: MouseEvent) {
    const it = rows[index];
    if (!it) return;
    if (e.shiftKey || e.metaKey || e.ctrlKey) {
      toggleSelect(index, !(allMatching || selected.has(it.id)), e.shiftKey);
      return;
    }
    setActiveId(it.id);
    if (!inlineInspector) setSheetOpen(true);
  }

  function toggleSelect(index: number, on: boolean, shift: boolean) {
    const it = rows[index];
    if (!it) return;
    // Leaving "all matching": keep what is loaded selected, then apply this toggle.
    const fromAll = allMatching;
    if (fromAll) setAllMatching(false);
    setSelected((cur) => {
      const next = fromAll ? new Set(rows.map((r) => r.id)) : new Set(cur);
      if (shift && anchor.current !== null) {
        const [a, b] = [Math.min(anchor.current, index), Math.max(anchor.current, index)];
        for (let i = a; i <= b; i++) {
          if (!rows[i]) continue;
          if (on) next.add(rows[i].id);
          else next.delete(rows[i].id);
        }
      } else if (on) next.add(it.id);
      else next.delete(it.id);
      return next;
    });
    anchor.current = index;
  }

  // Row callbacks that never change identity (they read the latest render through a ref), so a
  // push to one item re-renders that row only, not every visible row.
  const latest = useRef({ activate, toggleSelect });
  useLayoutEffect(() => {
    latest.current = { activate, toggleSelect };
  });
  const onActivate = useCallback((index: number, e: MouseEvent) => latest.current.activate(index, e), []);
  const onToggleSelect = useCallback(
    (index: number, on: boolean, shift: boolean) => latest.current.toggleSelect(index, on, shift),
    [],
  );

  const isSelected = (itemId: number) => allMatching || selected.has(itemId);
  const allVisibleSelected = rows.length > 0 && (allMatching || rows.every((r) => selected.has(r.id)));
  const someVisibleSelected = allMatching || rows.some((r) => selected.has(r.id));
  const selectionCount = allMatching ? matchingTotal : selected.size;
  const moreThanLoaded = !!hasNextPage;

  const mode: RenameMode = modeOverride ?? detail?.mode ?? 'move';
  // Selected items Execute would rename (loaded ones; a selection only holds loaded rows).
  const selectedRunnable = useMemo(() => {
    const ids: number[] = [];
    if (selected.size === 0) return ids;
    for (const p of pages ?? []) for (const it of p.items) if (selected.has(it.id) && isExecutable(it)) ids.push(it.id);
    return ids;
  }, [pages, selected]);
  const runnableTotal = counts.matched; // matched + approved, whole job
  // What Execute acts on, and how many files that is (null = known only to the server).
  let execCount: number | null;
  let execBody: Pick<ExecuteRequest, 'item_ids' | 'q'>;
  if (allMatching) {
    const runnableTab = filter === 'all' || filter === 'matched';
    execBody = { item_ids: null, q: itemFilter.q ?? null };
    execCount = !runnableTab ? 0 : !itemFilter.q ? runnableTotal : filter === 'matched' ? matchingTotal : null;
  } else if (selected.size > 0) {
    execBody = { item_ids: selectedRunnable };
    execCount = selectedRunnable.length;
  } else {
    execBody = { item_ids: null };
    execCount = runnableTotal;
  }
  const busy = detail?.status === 'executing' || detail?.status === 'matching' || detail?.status === 'queued';
  const canExecute = !!detail && execCount !== 0 && !busy && detail.status !== 'cancelled';
  const execLabel =
    execCount === null
      ? `${MODE_INFO[mode].verb} matching files`
      : `${MODE_INFO[mode].verb} ${execCount} file${execCount === 1 ? '' : 's'}`;

  const clearSelection = () => {
    setSelected(new Set());
    setAllMatching(false);
  };

  const doExecute = () => {
    if (!detail) return;
    execute.mutate(
      {
        id: detail.id,
        body: { ...execBody, mode: modeOverride && modeOverride !== detail.mode ? modeOverride : null },
      },
      {
        onSuccess: () => {
          setConfirmExec(false);
          clearSelection();
        },
      },
    );
  };

  /** Bulk action on the selection: explicit ids, or the whole tab + search server-side. */
  const bulkAct = (action: BulkAction) => {
    if (!detail) return;
    if (allMatching) {
      bulk.mutate(
        { action, job_id: detail.id, statuses: itemFilter.statuses ?? null, q: itemFilter.q ?? null },
        { onSuccess: clearSelection },
      );
    } else {
      bulk.mutate({ item_ids: [...selected], action });
    }
  };

  const statusAction = (status: 'approved' | 'skipped') => {
    if (selectionCount > 0) {
      bulkAct(status === 'approved' ? 'approve' : 'skip');
      return;
    }
    if (activeItem && !['executing', 'done'].includes(activeItem.status)) {
      update.mutate({ item: activeItem, patch: { status } });
      // Move on to the next row so review flows with one hand.
      if (activeIndex >= 0 && activeIndex < rows.length - 1) focusRow(activeIndex + 1);
    }
  };

  useHotkeys(
    {
      j: () => focusRow(Math.min(rows.length - 1, Math.max(0, activeIndex + 1))),
      k: () => focusRow(Math.max(0, activeIndex - 1)),
      arrowdown: (e) => {
        if (!scrollRef.current?.contains(document.activeElement)) return;
        e.preventDefault();
        focusRow(Math.min(rows.length - 1, activeIndex + 1));
      },
      arrowup: (e) => {
        if (!scrollRef.current?.contains(document.activeElement)) return;
        e.preventDefault();
        focusRow(Math.max(0, activeIndex - 1));
      },
      x: () => activeIndex >= 0 && toggleSelect(activeIndex, !isSelected(rows[activeIndex].id), false),
      a: () => statusAction('approved'),
      s: () => statusAction('skipped'),
      '/': (e) => {
        e.preventDefault();
        if (activeItem) openInspector(true);
      },
      enter: (e) => {
        if (!activeItem) return;
        const t = e.target as HTMLElement;
        if (t.closest('button, a, [role="tab"]') && !scrollRef.current?.contains(t)) return;
        e.preventDefault();
        openInspector(false);
      },
      escape: () => {
        if (sheetOpen) setSheetOpen(false);
        else if (selectionCount > 0) clearSelection();
      },
      'mod+enter': (e) => {
        e.preventDefault();
        if (canExecute) setConfirmExec(true);
      },
      '?': () => setShowKeys(true),
    },
    !!detail,
  );

  /* ------------------------------------------------------------------ render states */

  if (job.isPending) {
    return (
      <div className="flex h-full flex-col">
        <div className="border-b border-ink-800 px-4 py-4 md:px-6">
          <Skeleton className="mb-2 h-5 w-64" />
          <Skeleton className="h-4 w-96 max-w-full" />
        </div>
        <SkeletonRows rows={12} className="p-4 md:p-6" />
      </div>
    );
  }
  if (job.isError || !detail) {
    const notFound = job.error instanceof ApiError && job.error.isNotFound;
    return (
      <div className="flex min-h-[60vh] items-center justify-center p-4">
        {notFound ? (
          <EmptyState
            title="Job not found"
            description="It may have been deleted."
            action={
              <ButtonLink to="/" variant="primary" size="sm">
                Back to Home
              </ButtonLink>
            }
          />
        ) : (
          <ErrorState error={job.error} onRetry={() => void job.refetch()} title="Could not load this job" />
        )}
      </div>
    );
  }

  const conflict = settings.data?.conflict_policy;

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* ---------------------------------------------------------- top bar */}
      <div className="border-b border-ink-800 bg-ink-900/60">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 md:px-6">
          <div className="flex min-w-0 flex-1 items-center gap-2.5">
            <IconButton label="Back to Home" icon={<ArrowLeft className="h-4 w-4" />} size="sm" onClick={() => navigate('/')} />
            <div className="min-w-0">
              <div className="flex min-w-0 items-center gap-2">
                <h1 className="truncate text-[15px] font-semibold">{jobTitle(detail)}</h1>
                <JobStatusPill status={detail.status} />
              </div>
              <p className="truncate text-xs text-muted">
                {SOURCE_LABEL[detail.source]} · {countsSummary(detail)} · created {formatRelative(detail.created_at)}
              </p>
            </div>
          </div>
          <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto">
            <label className="flex items-center gap-1.5 text-xs text-muted">
              <span className="hidden lg:inline">Mode</span>
              <Select<RenameMode>
                size="sm"
                value={mode}
                onChange={(m) => setModeOverride(m)}
                options={RENAME_MODES.map((m) => ({ value: m, label: MODE_INFO[m].label }))}
                aria-label="Rename mode for this run"
                className="w-[132px]"
              />
            </label>
            {conflict && (
              <Tooltip content={CONFLICT_INFO[conflict].description}>
                <Link
                  to="/settings#moving"
                  className="hidden items-center gap-1 rounded-md px-1.5 py-1 text-xs text-muted hover:text-ice-100 lg:inline-flex"
                >
                  <ShieldQuestion className="h-3.5 w-3.5" />
                  On conflict: {CONFLICT_INFO[conflict].label}
                </Link>
              </Tooltip>
            )}
            <Menu
              items={[
                { label: 'Re-match all', icon: <RefreshCcw />, onSelect: () => rematchJob.mutate(detail.id), disabled: busy },
                { label: 'Cancel job', icon: <Ban />, onSelect: () => cancel.mutate(detail.id), disabled: !isActiveJob(detail.status) },
                { label: 'Keyboard shortcuts', icon: <Keyboard />, onSelect: () => setShowKeys(true) },
                { label: 'Delete job', icon: <Trash2 />, danger: true, onSelect: () => setConfirmDelete(true), disabled: detail.status === 'executing' },
              ]}
            />
            <Tooltip content={<span>Execute <Kbd>Ctrl</Kbd>+<Kbd>Enter</Kbd></span>}>
              <Button
                variant="primary"
                className="flex-1 sm:flex-none"
                leftIcon={<Play className="h-4 w-4" />}
                disabled={!canExecute}
                loading={execute.isPending}
                onClick={() => setConfirmExec(true)}
              >
                {execLabel}
              </Button>
            </Tooltip>
          </div>
        </div>
        {busy && (
          <ProgressBar
            className="h-0.5 rounded-none"
            value={detail.progress > 0 ? detail.progress : undefined}
            label={detail.status === 'executing' ? 'Renaming progress' : 'Matching progress'}
          />
        )}
        {detail.error && <div className="border-t border-danger/30 bg-danger/10 px-6 py-2 text-xs text-danger">{detail.error}</div>}
      </div>

      {/* ---------------------------------------------------------- filters + bulk bar */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-ink-800 px-4 md:px-6">
        <Tabs
          value={filter}
          onChange={setFilter}
          items={FILTERS.map((f) => ({ value: f, label: FILTER_LABELS[f], count: counts[f] }))}
          aria-label="Filter files"
          className="min-w-0 flex-1 border-b-0"
        />
        <Input
          size="sm"
          className="w-full pb-2 sm:w-44 sm:pb-0 xl:w-56"
          leftIcon={<Search />}
          placeholder="Filter files"
          value={text}
          onChange={(e) => setText(e.target.value)}
          aria-label="Filter files by name or path"
        />
        {!phone && (
          <IconButton
            label={inspectorPref ? 'Hide inspector' : 'Show inspector'}
            icon={inspectorPref ? <PanelRightClose className="h-4 w-4" /> : <PanelRightOpen className="h-4 w-4" />}
            size="sm"
            className="hidden xl:inline-flex"
            onClick={() => setInspectorPref((v) => !v)}
          />
        )}
      </div>

      {selectionCount > 0 && (
        <div className="flex flex-wrap items-center gap-2 border-b border-brand-sky/20 bg-brand-sky/[0.06] px-4 py-1.5 md:px-6" role="region" aria-label="Bulk actions">
          <span className="text-xs font-medium">
            {allMatching ? `All ${selectionCount} matching selected` : `${selectionCount} selected`}
          </span>
          <Button size="xs" variant="primary" leftIcon={<Check className="h-3 w-3" />} loading={bulk.isPending && bulk.variables?.action === 'approve'} onClick={() => bulkAct('approve')}>
            Approve
          </Button>
          <Button size="xs" leftIcon={<CircleSlash className="h-3 w-3" />} loading={bulk.isPending && bulk.variables?.action === 'skip'} onClick={() => bulkAct('skip')}>
            Skip
          </Button>
          <Button size="xs" leftIcon={<RefreshCcw className="h-3 w-3" />} loading={bulk.isPending && bulk.variables?.action === 'rematch'} onClick={() => bulkAct('rematch')}>
            Re-match
          </Button>
          <Button size="xs" variant="ghost" onClick={() => bulkAct('review')}>
            Back to review
          </Button>
          <Button size="xs" variant="subtle" leftIcon={<X className="h-3 w-3" />} onClick={clearSelection}>
            Clear
          </Button>
          {!allMatching && allVisibleSelected && moreThanLoaded && (
            <button type="button" className="text-xs text-brand-sky hover:underline" onClick={() => setAllMatching(true)}>
              Select all {matchingTotal} matching files
            </button>
          )}
        </div>
      )}

      {/* ---------------------------------------------------------- table + inspector */}
      <div className="flex min-h-0 flex-1">
        <div className="flex min-w-0 flex-1 flex-col">
          {!phone && (
            <div
              role="row"
              className={cn('grid h-8 shrink-0 items-center gap-2 border-b border-ink-800 bg-ink-900 px-3 text-2xs font-semibold uppercase tracking-wider text-muted', GRID_COLS)}
            >
              <span role="columnheader">
                <Checkbox
                  checked={allVisibleSelected}
                  indeterminate={!allVisibleSelected && someVisibleSelected}
                  onChange={(on) => {
                    if (!on && allMatching) {
                      clearSelection();
                      return;
                    }
                    const ids = new Set(rows.map((r) => r.id));
                    setSelected(on ? new Set([...selected, ...ids]) : new Set([...selected].filter((x) => !ids.has(x))));
                  }}
                  aria-label="Select all visible files"
                />
              </span>
              <span role="columnheader">Status</span>
              <span role="columnheader">Original filename</span>
              <span aria-hidden />
              <span role="columnheader">Parsed</span>
              <span aria-hidden />
              <span role="columnheader">Proposed path</span>
              <span role="columnheader" className="text-right">
                Conf.
              </span>
            </div>
          )}
          <div
            ref={scrollRef}
            role="grid"
            aria-label="Files in this job"
            aria-rowcount={Math.max(rows.length, matchingTotal) + 1}
            aria-multiselectable
            aria-activedescendant={activeItem ? `item-row-${activeItem.id}` : undefined}
            tabIndex={0}
            className="relative min-h-0 flex-1 overflow-y-auto outline-none focus-visible:shadow-[inset_0_0_0_2px_rgb(var(--ring))]"
          >
            {list.isPending ? (
              <SkeletonRows rows={12} className="p-4" />
            ) : list.isError ? (
              <ErrorState error={list.error} onRetry={() => void list.refetch()} title="Could not load the files" />
            ) : rows.length === 0 && hasNextPage ? (
              <SkeletonRows rows={6} className="p-4" />
            ) : rows.length === 0 ? (
              <EmptyState
                title={detail.total === 0 ? (busy ? 'Scanning and matching…' : 'No video files in this job') : 'Nothing matches this filter'}
                description={
                  detail.total === 0
                    ? busy
                      ? 'Files appear here as they are parsed and matched.'
                      : 'The selected paths contained no videos (samples and non-video files are ignored).'
                    : undefined
                }
              />
            ) : (
              <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
                {virtualRows.map((v) => {
                  const it = rows[v.index];
                  if (!it) return null;
                  const Row = phone ? ItemCard : ItemRow;
                  return (
                    <div
                      key={it.id}
                      data-index={v.index}
                      ref={phone ? virtualizer.measureElement : undefined}
                      style={{ position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${v.start}px)` }}
                    >
                      <Row
                        item={it}
                        index={v.index}
                        roots={roots}
                        mode={jobMode}
                        active={it.id === activeId}
                        selected={isSelected(it.id)}
                        onActivate={onActivate}
                        onToggleSelect={onToggleSelect}
                      />
                    </div>
                  );
                })}
              </div>
            )}
          </div>
          {moreThanLoaded && rows.length > 0 && (
            <div className="flex shrink-0 items-center gap-3 border-t border-ink-800 px-4 py-1.5 text-2xs text-muted" aria-live="polite">
              <span>
                Showing {rows.length} of {matchingTotal}
              </span>
              <Button size="xs" variant="ghost" loading={isFetchingNextPage} onClick={() => void fetchNextPage()}>
                Load more
              </Button>
            </div>
          )}
          {!phone && (
            <div className="hidden shrink-0 items-center gap-3 border-t border-ink-800 px-4 py-1.5 text-2xs text-muted md:flex">
              <span><Kbd>j</Kbd>/<Kbd>k</Kbd> move</span>
              <span><Kbd>a</Kbd> approve</span>
              <span><Kbd>s</Kbd> skip</span>
              <span><Kbd>x</Kbd> select</span>
              <span><Kbd>/</Kbd> search</span>
              <span><Kbd>Enter</Kbd> inspect</span>
              <span><Kbd>Ctrl</Kbd>+<Kbd>Enter</Kbd> execute</span>
              <button type="button" className="ml-auto hover:text-ice-100" onClick={() => setShowKeys(true)}>
                All shortcuts <Kbd>?</Kbd>
              </button>
            </div>
          )}
        </div>

        {inlineInspector && (
          <aside
            ref={inspectorRef}
            tabIndex={-1}
            aria-label="Inspector"
            className="w-[400px] shrink-0 overflow-y-auto border-l border-ink-800 bg-ink-900 outline-none animate-slide-in-right"
          >
            {activeItem ? (
              <Inspector item={activeItem} searchRef={searchRef} />
            ) : (
              <EmptyState compact title="Select a file" description="Its candidates, overrides and warnings show here." />
            )}
          </aside>
        )}
      </div>

      {/* phone / narrow: inspector sheet */}
      {!inlineInspector && (
        <Drawer open={sheetOpen && !!activeItem} onClose={() => setSheetOpen(false)} title={activeItem?.filename ?? 'Inspector'} width="420px">
          {activeItem && <Inspector item={activeItem} searchRef={searchRef} />}
        </Drawer>
      )}

      <ConfirmDialog
        open={confirmExec}
        onClose={() => setConfirmExec(false)}
        onConfirm={doExecute}
        loading={execute.isPending}
        title={`${execLabel}?`}
        description={MODE_INFO[mode].description}
        confirmLabel={execLabel}
      >
        <ul className="flex flex-col gap-1.5 text-[13px]">
          <li className="flex justify-between gap-4">
            <span className="text-muted">Files</span>
            <span>
              {allMatching
                ? `${execCount ?? 'All'} matched or approved matching the filter`
                : selected.size > 0
                  ? `${execCount} selected`
                  : `${execCount} matched or approved`}
            </span>
          </li>
          <li className="flex justify-between gap-4">
            <span className="text-muted">Mode</span>
            <span>{MODE_INFO[mode].label}</span>
          </li>
          {conflict && (
            <li className="flex justify-between gap-4">
              <span className="text-muted">On conflict</span>
              <span>{CONFLICT_INFO[conflict].label}</span>
            </li>
          )}
          {counts.review > 0 && selectionCount === 0 && (
            <li className="mt-1 rounded-md bg-warning/10 px-2 py-1.5 text-xs text-warning">
              {counts.review} file{counts.review === 1 ? '' : 's'} still need review and will be left alone.
            </li>
          )}
        </ul>
      </ConfirmDialog>

      <ConfirmDialog
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        onConfirm={() => del.mutate(detail.id, { onSuccess: () => navigate('/') })}
        loading={del.isPending}
        tone="danger"
        title="Delete this job?"
        description="Files that were already renamed stay where they are, and their history (with undo) is kept."
        confirmLabel="Delete job"
      />

      <Dialog open={showKeys} onClose={() => setShowKeys(false)} title="Keyboard shortcuts" size="sm">
        <dl className="grid grid-cols-[auto_1fr] items-center gap-x-4 gap-y-2 text-[13px]">
          {(
            [
              [['j'], 'Next file'],
              [['k'], 'Previous file'],
              [['a'], 'Approve (or approve selection)'],
              [['s'], 'Skip (or skip selection)'],
              [['x'], 'Toggle selection'],
              [['Shift', 'click'], 'Select a range'],
              [['/'], 'Search for another match'],
              [['Enter'], 'Open the inspector'],
              [['Esc'], 'Close inspector / clear selection'],
              [['Ctrl', 'Enter'], 'Execute (⌘ on Mac)'],
            ] as [string[], string][]
          ).map(([keys, label]) => (
            <div key={label} className="contents">
              <dt className="flex gap-1">
                {keys.map((k) => (
                  <Kbd key={k}>{k}</Kbd>
                ))}
              </dt>
              <dd className="text-muted">{label}</dd>
            </div>
          ))}
        </dl>
      </Dialog>
    </div>
  );
}

function parseItemParam(raw: string | null): number | null {
  if (!raw || !/^\d+$/.test(raw)) return null;
  const n = Number(raw);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}
