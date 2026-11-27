import { useEffect, useMemo, useRef, useState } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { ArrowUp, ChevronRight, File, FileVideo, Folder, HardDrive, RefreshCw, Search } from 'lucide-react';
import { useFsList } from '@/api/hooks';
import { ApiError } from '@/api/client';
import type { FsEntry } from '@/api/types';
import { cn } from '@/lib/cn';
import { formatBytes, formatRelative } from '@/lib/format';
import { useDebounced } from '@/lib/hooks';
import { splitPath } from '@/lib/pathDiff';
import { Button, Checkbox, EmptyState, ErrorState, IconButton, Input, SkeletonRows } from '@/components/ui';
import { filterEntries, sortEntries } from './entries';

/** Row height in px (`h-9`); rows are virtualized, so it must match the markup. */
const ROW_HEIGHT = 36;

export interface FileBrowserProps {
  selected: ReadonlySet<string>;
  onToggle: (path: string, on: boolean) => void;
  /** "Select N videos" in one update (falls back to `onToggle` per file). */
  onToggleMany?: (paths: string[], on: boolean) => void;
  /** Directory-only picking (watch folders, destination roots). */
  dirsOnly?: boolean;
  /** Called with the current directory (used by "choose this folder"). */
  onPickDir?: (path: string) => void;
  className?: string;
  maxHeight?: string;
}

/** Server-side file browser over GET /api/fs/list, confined to allowed_roots. */
export function FileBrowser({
  selected,
  onToggle,
  onToggleMany,
  dirsOnly,
  onPickDir,
  className,
  maxHeight = '420px',
}: FileBrowserProps) {
  const [path, setPath] = useState<string | undefined>(undefined);
  const [filter, setFilter] = useState('');
  // Filtering a 5000-entry folder waits for a pause in typing; clearing applies at once.
  const debounced = useDebounced(filter, 150);
  const needle = filter.trim() ? debounced.trim() : '';
  const q = useFsList(path);
  const listing = q.data;
  const scrollRef = useRef<HTMLDivElement>(null);

  // Sorted once per listing; each filter change is one pass over pre-lowered names.
  const sorted = useMemo(() => sortEntries(listing?.entries ?? [], dirsOnly), [listing, dirsOnly]);
  const entries = useMemo(() => filterEntries(sorted, needle), [sorted, needle]);

  const videos = useMemo(() => entries.filter((e) => !e.is_dir && e.is_video), [entries]);
  const allVideosSelected = useMemo(
    () => videos.length > 0 && videos.every((v) => selected.has(v.path)),
    [videos, selected],
  );
  const toggleVideos = () => {
    const on = !allVideosSelected;
    const paths = videos.map((v) => v.path);
    if (onToggleMany) onToggleMany(paths, on);
    else paths.forEach((p) => onToggle(p, on));
  };

  const virtualizer = useVirtualizer({
    count: entries.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 10,
  });
  // A new folder (or filter) starts at the top.
  const listingPath = listing?.path;
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 0 });
  }, [listingPath, needle]);

  const crumbs = useMemo(() => {
    if (!listing) return [];
    const root = listing.roots.find((r) => listing.path === r || listing.path.startsWith(r.replace(/[\\/]+$/, '') + '/') || listing.path.startsWith(r.replace(/[\\/]+$/, '') + '\\'));
    if (!root) return [{ label: listing.path, path: listing.path }];
    const rest = listing.path.slice(root.replace(/[\\/]+$/, '').length).split(/[\\/]/).filter(Boolean);
    const sep = root.includes('\\') && !root.includes('/') ? '\\' : '/';
    const base = root.replace(/[\\/]+$/, '');
    const out = [{ label: root, path: root }];
    rest.forEach((seg, i) => out.push({ label: seg, path: base + sep + rest.slice(0, i + 1).join(sep) }));
    return out;
  }, [listing]);

  const go = (p: string | undefined) => {
    setFilter('');
    setPath(p);
  };

  const forbidden = q.error instanceof ApiError && q.error.isForbidden;

  return (
    <div className={cn('flex min-w-0 flex-col', className)}>
      {listing && listing.roots.length > 1 && (
        <div className="flex gap-1.5 overflow-x-auto border-b border-ink-800 px-3 py-2 scrollbar-none">
          {listing.roots.map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => go(r)}
              className={cn(
                'inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md border px-2 font-mono text-2xs',
                crumbs[0]?.path === r ? 'border-brand-sky/40 bg-brand-sky/10 text-accent' : 'border-ink-800 text-muted hover:text-ice-100',
              )}
            >
              <HardDrive className="h-3 w-3" />
              {r}
            </button>
          ))}
        </div>
      )}
      <div className="flex items-center gap-1 border-b border-ink-800 px-2 py-1.5">
        <IconButton
          label="Up one folder"
          icon={<ArrowUp className="h-4 w-4" />}
          size="sm"
          disabled={!listing?.parent}
          onClick={() => go(listing?.parent ?? undefined)}
        />
        <nav aria-label="Folder path" className="flex min-w-0 flex-1 items-center overflow-x-auto scrollbar-none">
          {crumbs.map((c, i) => (
            <span key={c.path} className="flex shrink-0 items-center">
              {i > 0 && <ChevronRight className="mx-0.5 h-3 w-3 text-muted/60" aria-hidden />}
              <button
                type="button"
                onClick={() => go(c.path)}
                aria-current={i === crumbs.length - 1 ? 'page' : undefined}
                className={cn(
                  'rounded px-1 py-0.5 font-mono text-xs hover:bg-ink-850',
                  i === crumbs.length - 1 ? 'text-ice-100' : 'text-muted',
                )}
              >
                {c.label}
              </button>
            </span>
          ))}
        </nav>
        <IconButton label="Refresh" icon={<RefreshCw className="h-3.5 w-3.5" />} size="sm" onClick={() => void q.refetch()} />
      </div>
      <div className="flex items-center gap-2 px-3 py-2">
        <Input
          size="sm"
          className="flex-1"
          leftIcon={<Search />}
          placeholder="Filter this folder"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          aria-label="Filter entries"
        />
        {!dirsOnly && videos.length > 0 && (
          <Button size="sm" variant="ghost" onClick={toggleVideos}>
            {allVideosSelected ? 'Clear videos' : `Select ${videos.length} videos`}
          </Button>
        )}
        {onPickDir && listing && (
          <Button size="sm" variant="secondary" onClick={() => onPickDir(listing.path)}>
            Use this folder
          </Button>
        )}
      </div>
      <div ref={scrollRef} className="min-h-[160px] overflow-y-auto border-t border-ink-800" style={{ maxHeight }}>
        {q.isPending ? (
          <SkeletonRows rows={6} className="p-3" />
        ) : q.isError ? (
          forbidden ? (
            <EmptyState
              compact
              title="Outside the allowed roots"
              description="Schema can only browse folders listed under Settings → General → Allowed roots."
              action={
                <Button size="sm" onClick={() => go(undefined)}>
                  Back to roots
                </Button>
              }
            />
          ) : (
            <ErrorState compact error={q.error} onRetry={() => void q.refetch()} />
          )
        ) : entries.length === 0 ? (
          <EmptyState
            compact
            title={needle ? 'No matches' : listing?.roots.length === 0 ? 'No allowed roots yet' : 'Empty folder'}
            description={
              listing?.roots.length === 0 ? 'Add allowed roots in Settings → General to browse the server.' : undefined
            }
          />
        ) : (
          <ul role="list" className="relative" style={{ height: virtualizer.getTotalSize() }}>
            {virtualizer.getVirtualItems().map((v) => {
              const e = entries[v.index];
              return (
                <EntryRow
                  key={e.path}
                  entry={e}
                  top={v.start}
                  checked={selected.has(e.path)}
                  onToggle={onToggle}
                  onOpen={() => go(e.path)}
                  selectable={!dirsOnly}
                />
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

function EntryRow({
  entry,
  top,
  checked,
  onToggle,
  onOpen,
  selectable,
}: {
  entry: FsEntry;
  /** Offset inside the virtualized list (px). */
  top: number;
  checked: boolean;
  onToggle: (path: string, on: boolean) => void;
  onOpen: () => void;
  selectable: boolean;
}) {
  const Icon = entry.is_dir ? Folder : entry.is_video ? FileVideo : File;
  const p = splitPath(entry.name);
  const dim = !entry.is_dir && !entry.is_video;
  return (
    <li
      className={cn(
        'group absolute left-0 top-0 flex h-9 w-full items-center gap-2.5 border-b border-ink-800/60 px-3',
        checked && 'bg-brand-sky/[0.06]',
      )}
      style={{ transform: `translateY(${top}px)` }}
    >
      {selectable && (
        <Checkbox
          checked={checked}
          onChange={(on) => onToggle(entry.path, on)}
          aria-label={`Select ${entry.name}`}
          disabled={dim}
        />
      )}
      <Icon
        className={cn('h-4 w-4 shrink-0', entry.is_dir ? 'text-brand-azure' : entry.is_video ? 'text-accent' : 'text-muted/60')}
        aria-hidden
      />
      {entry.is_dir ? (
        <button
          type="button"
          onClick={onOpen}
          className="min-w-0 flex-1 truncate text-left font-mono text-xs text-ice-100 hover:underline"
          title={entry.path}
        >
          {entry.name}
        </button>
      ) : (
        <span className={cn('min-w-0 flex-1 truncate font-mono text-xs', dim ? 'text-muted/70' : 'text-ice-100')} title={entry.path}>
          {p.stem}
          {p.ext && <span className="text-muted">.{p.ext}</span>}
        </span>
      )}
      <span className="hidden w-20 shrink-0 text-right text-2xs text-muted sm:block">
        {entry.is_dir ? '' : formatBytes(entry.size)}
      </span>
      <span className="hidden w-24 shrink-0 text-right text-2xs text-muted md:block">{formatRelative(entry.modified)}</span>
    </li>
  );
}
