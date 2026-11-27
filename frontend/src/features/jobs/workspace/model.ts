/** Pure helpers for the Match workspace (filters, counts, display paths). */
import type {
  AppSettings,
  Candidate,
  ItemStatus,
  JobDetail,
  JobItemFilter,
  JobItemOut,
  JobItemPage,
  ProviderRef,
  RenameMode,
} from '@/api/types';
import { pageIndex } from '@/api/wsReducer';
import type { ItemAction } from '@/lib/labels';
import { basename, relativeTo } from '@/lib/pathDiff';

export type ItemFilter = 'all' | 'review' | 'matched' | 'skipped' | 'done' | 'error';

export const FILTER_STATUSES: Record<Exclude<ItemFilter, 'all'>, ItemStatus[]> = {
  review: ['review'],
  matched: ['matched', 'approved'],
  skipped: ['skipped'],
  done: ['done'],
  error: ['error'],
};

export const FILTER_LABELS: Record<ItemFilter, string> = {
  all: 'All',
  review: 'Needs review',
  matched: 'Matched',
  skipped: 'Skipped',
  done: 'Done',
  error: 'Errors',
};

export function matchesFilter(item: JobItemOut, filter: ItemFilter): boolean {
  return filter === 'all' || FILTER_STATUSES[filter].includes(item.status);
}

/** Per page object: its rows that pass the tab filter (pages are immutable, see wsReducer). */
const pageRows = new WeakMap<JobItemPage, { filter: ItemFilter; rows: readonly JobItemOut[] }>();

/**
 * The loaded rows of a paged item list that still pass the tab filter (a row whose status changed
 * since its page was fetched drops out right away). Pages that did not change reuse their
 * filtered rows, so a WS push to one item re-filters one page, not the whole list.
 */
export function visibleRows(pages: readonly JobItemPage[] | undefined, filter: ItemFilter): JobItemOut[] {
  const out: JobItemOut[] = [];
  for (const p of pages ?? []) {
    let hit = pageRows.get(p);
    if (!hit || hit.filter !== filter) {
      hit = { filter, rows: filter === 'all' ? p.items : p.items.filter((it) => matchesFilter(it, filter)) };
      pageRows.set(p, hit);
    }
    for (const r of hit.rows) out.push(r);
  }
  return out;
}

/** A loaded item by id, wherever it is (also rows hidden by the tab filter). */
export function findLoadedItem(pages: readonly JobItemPage[] | undefined, id: number | null): JobItemOut | null {
  if (id === null) return null;
  for (const p of pages ?? []) {
    const idx = pageIndex(p).get(id);
    if (idx !== undefined) return p.items[idx];
  }
  return null;
}

export function filterCounts(items: readonly JobItemOut[]): Record<ItemFilter, number> {
  const counts: Record<ItemFilter, number> = { all: items.length, review: 0, matched: 0, skipped: 0, done: 0, error: 0 };
  for (const it of items) {
    for (const f of Object.keys(FILTER_STATUSES) as Exclude<ItemFilter, 'all'>[]) {
      if (FILTER_STATUSES[f].includes(it.status)) counts[f] += 1;
    }
  }
  return counts;
}

/** Tab counts from the job's server-side per-status counts (covers items not loaded yet). */
export function filterCountsFromJob(job: Pick<JobDetail, 'counts' | 'total'>): Record<ItemFilter, number> {
  const counts: Record<ItemFilter, number> = { all: job.total, review: 0, matched: 0, skipped: 0, done: 0, error: 0 };
  for (const f of Object.keys(FILTER_STATUSES) as Exclude<ItemFilter, 'all'>[]) {
    counts[f] = FILTER_STATUSES[f].reduce((n, s) => n + (job.counts[s] ?? 0), 0);
  }
  return counts;
}

/** Server-side item filter for a workspace tab + search text. */
export function itemFilterOf(filter: ItemFilter, q: string): JobItemFilter {
  return { statuses: filter === 'all' ? null : FILTER_STATUSES[filter], q: q.trim() || null };
}

export function matchesText(item: JobItemOut, q: string): boolean {
  if (!q) return true;
  const needle = q.toLowerCase();
  return (
    item.filename.toLowerCase().includes(needle) ||
    (item.proposed_path ?? '').toLowerCase().includes(needle) ||
    (item.parsed.title ?? '').toLowerCase().includes(needle)
  );
}

/** Items that "Execute" will act on: matched + approved (SPEC ExecuteRequest default). */
export const isExecutable = (item: JobItemOut) => item.status === 'matched' || item.status === 'approved';

export function selectedCandidate(item: JobItemOut): Candidate | null {
  if (item.selected_candidate_id) {
    const c = item.candidates.find((x) => x.id === item.selected_candidate_id);
    if (c) return c;
  }
  return item.candidates[0] ?? null;
}

/** The roots display paths are shown relative to: the job's destination, then the library roots. */
export function displayRoots(destRoot: string | null | undefined, settings?: AppSettings): string[] {
  return [destRoot, ...Object.values(settings?.library_roots ?? {})].filter((r): r is string => !!r);
}

/** Destination shown in the table: relative to the library root when we can tell it. */
export function displayPath(item: JobItemOut, job: Pick<JobDetail, 'dest_root'>, settings?: AppSettings): string | null {
  return displayPathUnder(item, displayRoots(job.dest_root, settings));
}

/** `displayPath` with the roots worked out once (see `displayRoots`); rows call this themselves. */
export function displayPathUnder(item: JobItemOut, roots: readonly string[]): string | null {
  const underRoot = (p: string) => {
    for (const r of roots) {
      const rel = relativeTo(p, r);
      if (rel !== p) return rel;
    }
    return p;
  };
  // Done items show where the file actually landed, still relative to the library root.
  if (item.final_path) return underRoot(item.final_path);
  if (!item.proposed_path) return item.proposed_relative;
  if (!item.path_overridden && item.proposed_relative) return item.proposed_relative;
  return underRoot(item.proposed_path);
}

/**
 * What a move job does (or did) with the item's folder: renames it in place, or moves the file into a
 * new one. Null outside move jobs, and for items with nothing to move.
 */
export function itemAction(item: JobItemOut, mode: RenameMode): ItemAction | null {
  if (item.folder_rename) return 'folder_renamed';
  if (mode !== 'move' || !(item.final_path ?? item.proposed_path)) return null;
  if (item.status === 'skipped' || item.status === 'error' || item.warnings.includes('already_in_place')) return null;
  return 'moved';
}

/** Tooltip for the action badge: "Alien.1979 → Alien (1979)". */
export function itemActionTitle(item: JobItemOut, action: ItemAction): string {
  const rename = item.folder_rename;
  if (action === 'folder_renamed' && rename) {
    return `Folder ${basename(rename.source)} → ${basename(rename.destination)}, renamed in place`;
  }
  return 'Moved into the destination folder';
}

export type CrcState = 'ok' | 'mismatch' | 'unchecked' | null;

export function crcState(item: JobItemOut): CrcState {
  if (!item.parsed.crc32) return null;
  if (item.crc_ok === false || item.warnings.some((w) => w.startsWith('crc_mismatch'))) return 'mismatch';
  if (item.crc_ok === true) return 'ok';
  return 'unchecked';
}

/** Provider reference for a candidate (used to force it via rematch hint.ref). */
export function candidateRef(c: Candidate): ProviderRef {
  const ref = c.movie?.ref ?? c.series?.ref;
  if (ref) return ref;
  // id format: "provider:kind:provider_id"
  const [provider, , ...rest] = c.id.split(':');
  return { provider: provider || c.provider, id: rest.join(':') || c.id };
}

export function posterOf(c: Candidate): string | null {
  return c.movie?.poster_url ?? c.series?.poster_url ?? null;
}
