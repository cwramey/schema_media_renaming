/**
 * Applies server-pushed WsMessages (SPEC §5, `/api/ws`) to the TanStack Query cache.
 *
 * The transforms are pure functions over the cached shapes so they can be unit
 * tested; `applyWsMessage` wires them to a QueryClient. Server-computed views
 * the client cannot rebuild exactly (the Home board and the History feed) are
 * refreshed through a debounced invalidation instead.
 */
import { notifyManager, type InfiniteData, type QueryClient, type QueryKey } from '@tanstack/react-query';
import { qk, type EventFilter, type JobListFilter, type OperationFilter } from './keys';
import type {
  EventOut,
  JobDetail,
  JobItemFilter,
  JobItemOut,
  JobItemPage,
  JobOut,
  JobStatus,
  OperationOut,
  ProviderStatus,
  ProvidersOut,
  WatchFolderOut,
  WsMessage,
} from './types';

export const ACTIVE_JOB_STATUSES: readonly JobStatus[] = ['queued', 'matching', 'review', 'ready', 'executing'];
export const isActiveJob = (status: JobStatus) => ACTIVE_JOB_STATUSES.includes(status);

/* ------------------------------------------------------------------ generic list helpers */

/** Replace the element with the same key, or insert it (front by default). Returns a new array. */
export function upsertBy<T, K>(
  list: readonly T[],
  item: T,
  keyOf: (t: T) => K,
  opts: { prepend?: boolean; limit?: number } = {},
): T[] {
  const key = keyOf(item);
  const idx = list.findIndex((x) => keyOf(x) === key);
  let next: T[];
  if (idx >= 0) {
    next = list.slice();
    next[idx] = item;
  } else {
    next = opts.prepend === false ? [...list, item] : [item, ...list];
  }
  if (opts.limit !== undefined && next.length > opts.limit) next = next.slice(0, opts.limit);
  return next;
}

export function removeBy<T, K>(list: readonly T[], key: K, keyOf: (t: T) => K): T[] {
  const next = list.filter((x) => keyOf(x) !== key);
  return next.length === list.length ? (list as T[]) : next;
}

const byId = (x: { id: number }) => x.id;
const byName = (x: { name: string }) => x.name;

/* ------------------------------------------------------------------ jobs */

export function applyJobToList(list: readonly JobOut[], job: JobOut, filter: JobListFilter = {}): JobOut[] {
  const present = list.some((j) => j.id === job.id);
  const matches = !filter.status || filter.status === job.status;
  if (!matches) return present ? removeBy(list, job.id, byId) : (list as JobOut[]);
  return upsertBy(list, job, byId, { prepend: true });
}

/** Merge job-level fields into a cached detail, keeping its items. */
export function applyJobToDetail(detail: JobDetail, job: JobOut): JobDetail {
  return { ...detail, ...job, items: detail.items };
}

/**
 * Merge an incoming item over a cached copy. A slim item (WS push, list page) never clobbers the
 * full candidate data of a fully loaded one; everything else (status, paths, warnings ...) wins.
 */
export function mergeItem(cur: JobItemOut | undefined, next: JobItemOut): JobItemOut {
  if (!cur || !next.slim || cur.slim) return next;
  return { ...next, candidates: cur.candidates, candidate_count: cur.candidate_count, slim: false };
}

/** True when a slim push says a cached full item is out of date (refetch it). */
export function fullItemIsStale(cur: JobItemOut | undefined, next: JobItemOut): boolean {
  return !!cur && !cur.slim && !!next.slim && cur.updated_at !== next.updated_at;
}

/**
 * Replace an item inside a cached job detail. New items are appended only when the detail holds
 * every item of the job (a header-only or partial detail stays as it is).
 */
export function applyItemToDetail(detail: JobDetail, item: JobItemOut): JobDetail {
  if (detail.id !== item.job_id) return detail;
  const idx = detail.items.findIndex((i) => i.id === item.id);
  if (idx >= 0) {
    const items = detail.items.slice();
    items[idx] = mergeItem(items[idx], item);
    return { ...detail, items };
  }
  const complete = detail.items_total === undefined || detail.items.length >= detail.items_total;
  if (!complete) return detail;
  return {
    ...detail,
    items: [...detail.items, item],
    ...(detail.items_total !== undefined ? { items_total: detail.items_total + 1 } : {}),
  };
}

/** Infinite-query cache of `GET /jobs/{id}/items` pages. */
export type ItemPages = InfiniteData<JobItemPage, number | null>;

/**
 * Item id -> position, per page object. Cached pages are never mutated (a changed page is a new
 * object), so each index is built once and a WS push finds its row without scanning every item.
 */
const pageIndexes = new WeakMap<JobItemPage, Map<number, number>>();

export function pageIndex(page: JobItemPage): Map<number, number> {
  let index = pageIndexes.get(page);
  if (!index) {
    index = new Map();
    for (let i = 0; i < page.items.length; i++) index.set(page.items[i].id, i);
    pageIndexes.set(page, index);
  }
  return index;
}

/**
 * Apply one item to a cached page list: replace it where it is (merged, see `mergeItem`), or
 * insert it in id order when it falls inside the loaded range and passes the list's status filter.
 * Items that leave the filter stay cached; the workspace hides them client-side.
 */
export function applyItemToPages(data: ItemPages, item: JobItemOut, filter: JobItemFilter = {}): ItemPages {
  for (let pi = 0; pi < data.pages.length; pi++) {
    const p = data.pages[pi];
    const idx = pageIndex(p).get(item.id);
    if (idx === undefined) continue;
    const items = p.items.slice();
    items[idx] = mergeItem(items[idx], item);
    const pages = data.pages.slice();
    pages[pi] = { ...p, items };
    return { ...data, pages };
  }

  const last = data.pages[data.pages.length - 1];
  if (!last) return data;
  if (filter.q) return data; // text matching is server-side; picked up on the next fetch
  if (filter.statuses?.length && !filter.statuses.includes(item.status)) return data;
  const lastPage = [...data.pages].reverse().find((p) => p.items.length > 0);
  const lastLoadedId = lastPage ? lastPage.items[lastPage.items.length - 1].id : -Infinity;
  if (last.next_after !== null && item.id > lastLoadedId) return data; // arrives with a later page

  let target = data.pages.findIndex((p) => p.items.length > 0 && p.items[p.items.length - 1].id > item.id);
  if (target < 0) target = data.pages.length - 1;
  return {
    ...data,
    pages: data.pages.map((p, i) => {
      const total = p.total + 1;
      if (i !== target) return { ...p, total };
      const at = p.items.findIndex((x) => x.id > item.id);
      const items = at < 0 ? [...p.items, item] : [...p.items.slice(0, at), item, ...p.items.slice(at)];
      return { ...p, items, total };
    }),
  };
}

/* ------------------------------------------------------------------ operations / events */

export function applyOperationToList(
  list: readonly OperationOut[],
  op: OperationOut,
  filter: OperationFilter = {},
): OperationOut[] {
  if (filter.job_id !== undefined && filter.job_id !== op.job_id) return list as OperationOut[];
  return upsertBy(list, op, byId, { prepend: true });
}

export function eventMatches(ev: EventOut, filter: EventFilter = {}): boolean {
  if (filter.level && filter.level !== ev.level) return false;
  if (filter.category && filter.category !== ev.category) return false;
  if (filter.job_id !== undefined && filter.job_id !== ev.job_id) return false;
  return true;
}

export function applyEventToList(list: readonly EventOut[], ev: EventOut, filter: EventFilter = {}): EventOut[] {
  if (!eventMatches(ev, filter)) return list as EventOut[];
  const limit = Math.max(filter.limit ?? 50, list.length);
  return upsertBy(list, ev, byId, { prepend: true, limit });
}

/* ------------------------------------------------------------------ watchers / providers */

export function applyProviderToProviders(p: ProvidersOut, status: ProviderStatus): ProvidersOut {
  return { ...p, statuses: upsertBy(p.statuses, status, byName, { prepend: false }) };
}

/* ------------------------------------------------------------------ cache wiring */

export interface ApplyOptions {
  /** Called for aggregates that must be refetched; default is a 1 s debounced invalidate. */
  invalidate?: (key: QueryKey) => void;
  /** Called for `resync`; default is `scheduleResync` (debounced). */
  resync?: () => void;
}

export interface ScheduleOptions {
  /** Delay before the run (ms). */
  ms?: number;
  /** Push the run back on every call (trailing debounce) instead of keeping the first deadline. */
  restart?: boolean;
  /** How long after the first call the run may be pushed back, by `restart` or by `busy` (ms). */
  maxWait?: number;
  /**
   * True while a fetch the run would repeat is still in flight. The run then waits for it (polling
   * every `BUSY_POLL_MS`, up to `maxWait`) instead of aborting it or piggybacking on its older data.
   */
  busy?: () => boolean;
}

const BUSY_POLL_MS = 250;
const scheduled = new WeakMap<QueryClient, Map<string, { timer: ReturnType<typeof setTimeout>; first: number }>>();

/** Run `run` once after `opts.ms`; further calls with the same `id` while it is pending coalesce. */
export function schedule(qc: QueryClient, id: string, run: () => void, opts: ScheduleOptions = {}): void {
  const { ms = 1000, restart = false, maxWait = 10_000, busy } = opts;
  let pending = scheduled.get(qc);
  if (!pending) {
    pending = new Map();
    scheduled.set(qc, pending);
  }
  const map = pending;
  const cur = map.get(id);
  if (cur && !restart) return;
  if (cur) clearTimeout(cur.timer);
  const first = cur?.first ?? Date.now();
  const arm = (delay: number) => map.set(id, { timer: setTimeout(fire, Math.max(0, delay)), first });
  function fire() {
    if (busy?.() && Date.now() - first < maxWait) {
      arm(BUSY_POLL_MS);
      return;
    }
    map.delete(id);
    run();
  }
  arm(restart ? Math.min(ms, first + maxWait - Date.now()) : ms);
}

/**
 * Invalidate `key` after `ms`, coalescing bursts into one refetch. `cancelRefetch: false` plus the
 * `busy` wait mean a slow request (Home or History on the NAS) is never aborted client-side while
 * the server keeps working on it, and the refetch still starts after the last change.
 */
export function debouncedInvalidate(qc: QueryClient, key: QueryKey, ms = 1000) {
  schedule(qc, `invalidate:${JSON.stringify(key)}`, () => void qc.invalidateQueries({ queryKey: key }, { cancelRefetch: false }), {
    ms,
    busy: () => qc.isFetching({ queryKey: key }) > 0,
  });
}

/** Infinite lists keep at most this many pages through a resync (a full refetch is one request per page). */
export const RESYNC_KEEP_PAGES = 4;
export const RESYNC_DEBOUNCE_MS = 1500;

const isInfinite = (d: unknown): d is InfiniteData<unknown, unknown> =>
  !!d && Array.isArray((d as InfiniteData<unknown>).pages) && Array.isArray((d as InfiniteData<unknown>).pageParams);

/**
 * Cut cached infinite lists (job items, History) down to their first `keep` pages, so the refetch
 * that follows is `keep` requests instead of one per page scrolled through. Lists that are
 * fetching are left alone (the fetch would write its pages back).
 */
export function trimInfiniteQueries(qc: QueryClient, keep = RESYNC_KEEP_PAGES): void {
  for (const query of qc.getQueryCache().getAll()) {
    const data = query.state.data;
    if (!isInfinite(data) || data.pages.length <= keep || query.state.fetchStatus === 'fetching') continue;
    qc.setQueryData(query.queryKey, { pages: data.pages.slice(0, keep), pageParams: data.pageParams.slice(0, keep) });
  }
}

/** Refetch what is on screen after missed messages; see `scheduleResync`. */
export function resyncNow(qc: QueryClient): void {
  trimInfiniteQueries(qc);
  void qc.invalidateQueries({ refetchType: 'active' }, { cancelRefetch: false });
}

/**
 * The server sends `resync` each time this socket's queue overflows (~once per 1000 messages), so a
 * big job brings several in a row: refetch once they stop (trailing debounce, at most `maxWait`
 * late), after in-flight fetches finish, and without aborting each other.
 */
export function scheduleResync(qc: QueryClient, ms = RESYNC_DEBOUNCE_MS): void {
  schedule(qc, 'resync', () => resyncNow(qc), { ms, restart: true, maxWait: 10_000, busy: () => qc.isFetching() > 0 });
}

function filterOf<F>(key: QueryKey, index: number): F {
  const f = key[index];
  return (f && typeof f === 'object' ? f : {}) as F;
}

export function applyWsMessage(qc: QueryClient, msg: WsMessage, opts: ApplyOptions = {}): void {
  const invalidate = opts.invalidate ?? ((key: QueryKey) => debouncedInvalidate(qc, key));

  switch (msg.type) {
    case 'hello':
      return;

    case 'job.updated': {
      const job = msg.data;
      for (const [key, list] of qc.getQueriesData<JobOut[]>({ queryKey: qk.jobs.lists })) {
        if (!Array.isArray(list)) continue;
        qc.setQueryData<JobOut[]>(key, applyJobToList(list, job, filterOf<JobListFilter>(key, 2)));
      }
      qc.setQueryData<JobDetail>(qk.jobs.detail(job.id), (d) => (d ? applyJobToDetail(d, job) : d));
      invalidate(qk.home);
      return;
    }

    case 'job.deleted': {
      const { id } = msg.data;
      for (const [key, list] of qc.getQueriesData<JobOut[]>({ queryKey: qk.jobs.lists })) {
        if (!Array.isArray(list)) continue;
        qc.setQueryData<JobOut[]>(key, removeBy(list, id, byId));
      }
      qc.removeQueries({ queryKey: qk.jobs.detail(id), exact: true });
      qc.removeQueries({ queryKey: qk.jobs.itemLists(id) });
      invalidate(qk.home);
      return;
    }

    case 'item.updated': {
      const item = msg.data;
      qc.setQueryData<JobDetail>(qk.jobs.detail(item.job_id), (d) => (d ? applyItemToDetail(d, item) : d));
      for (const [key, data] of qc.getQueriesData<ItemPages>({ queryKey: qk.jobs.itemLists(item.job_id) })) {
        if (!data || !Array.isArray(data.pages)) continue;
        qc.setQueryData<ItemPages>(key, applyItemToPages(data, item, filterOf<JobItemFilter>(key, 3)));
      }
      const full = qc.getQueryData<JobItemOut>(qk.items.detail(item.id));
      if (full) {
        qc.setQueryData<JobItemOut>(qk.items.detail(item.id), mergeItem(full, item));
        // Candidates may have changed (rematch): refetch the full item if anyone shows it.
        if (fullItemIsStale(full, item)) invalidate(qk.items.detail(item.id));
      }
      invalidate(qk.home);
      // Skipped and failed items are History entries too.
      if (item.status === 'skipped' || item.status === 'error') invalidate(qk.history.all);
      return;
    }

    case 'job.items_changed': {
      // No item data: refetch the job's item pages, its header (tab counts) and open inspectors.
      const { job_id: jobId, item_ids: ids } = msg.data;
      invalidate(qk.jobs.itemLists(jobId));
      invalidate(qk.jobs.detail(jobId));
      if (ids && ids.length > 0) {
        for (const id of ids) {
          if (qc.getQueryData(qk.items.detail(id)) !== undefined) invalidate(qk.items.detail(id));
        }
      } else {
        for (const [key, full] of qc.getQueriesData<JobItemOut>({ queryKey: qk.items.all })) {
          if (full && full.job_id === jobId) invalidate(key);
        }
      }
      invalidate(qk.home);
      // Statuses are unknown here; skipped and failed items are History entries.
      invalidate(qk.history.all);
      return;
    }

    case 'operation.created':
    case 'operation.updated': {
      const op = msg.data;
      for (const [key, list] of qc.getQueriesData<OperationOut[]>({ queryKey: qk.operations.all })) {
        if (!Array.isArray(list)) continue;
        qc.setQueryData<OperationOut[]>(key, applyOperationToList(list, op, filterOf<OperationFilter>(key, 1)));
      }
      invalidate(qk.home);
      invalidate(qk.history.all);
      return;
    }

    case 'event': {
      const ev = msg.data;
      for (const [key, list] of qc.getQueriesData<EventOut[]>({ queryKey: qk.events.all })) {
        if (!Array.isArray(list)) continue;
        qc.setQueryData<EventOut[]>(key, applyEventToList(list, ev, filterOf<EventFilter>(key, 1)));
      }
      // Events can raise or clear "Needs you" event cards (debounced, so bursts cost one fetch).
      invalidate(qk.home);
      return;
    }

    case 'watcher.updated': {
      const w = msg.data;
      qc.setQueryData<WatchFolderOut[]>(qk.watchFolders, (list) =>
        list ? upsertBy(list, w, byId, { prepend: false }) : list,
      );
      invalidate(qk.home);
      return;
    }

    case 'provider.updated': {
      const p = msg.data;
      qc.setQueryData<ProvidersOut>(qk.providers, (cur) => (cur ? applyProviderToProviders(cur, p) : cur));
      invalidate(qk.home);
      return;
    }

    case 'resync':
      (opts.resync ?? (() => scheduleResync(qc)))();
      return;

    default: {
      // Unknown message types from a newer server are ignored.
      const _exhaustive: never = msg;
      void _exhaustive;
    }
  }
}

/**
 * Drop `item.updated` / `job.updated` messages that a later message in the same batch replaces
 * (each carries the whole row). Everything else keeps its order.
 */
export function coalesceWsMessages(msgs: readonly WsMessage[]): WsMessage[] {
  if (msgs.length < 2) return msgs.slice();
  const seen = new Set<string>();
  const out: WsMessage[] = [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    const key = m.type === 'item.updated' || m.type === 'job.updated' ? `${m.type}:${m.data.id}` : null;
    if (key !== null) {
      if (seen.has(key)) continue;
      seen.add(key);
    }
    out.push(m);
  }
  return out.reverse();
}

/**
 * Apply a batch of messages (one animation frame's worth, see `ws.ts`) with observer notifications
 * batched, so a burst costs one render instead of one per message.
 */
export function applyWsMessages(qc: QueryClient, msgs: readonly WsMessage[], opts: ApplyOptions = {}): void {
  notifyManager.batch(() => {
    for (const msg of coalesceWsMessages(msgs)) applyWsMessage(qc, msg, opts);
  });
}

/** Defensive parse of a raw socket frame. Returns null for anything that isn't a WsMessage. */
export function parseWsFrame(raw: unknown): WsMessage | null {
  if (typeof raw !== 'string') return null;
  try {
    const v = JSON.parse(raw) as unknown;
    if (v && typeof v === 'object' && 'type' in v && 'data' in v && typeof (v as { type: unknown }).type === 'string') {
      return v as WsMessage;
    }
  } catch {
    /* ignore malformed frames */
  }
  return null;
}
