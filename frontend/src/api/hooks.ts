/**
 * One TanStack Query hook per API route (SPEC §5).
 * Queries are `use<Thing>`; mutations are `use<Verb><Thing>`.
 */
import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
  type QueryKey,
} from '@tanstack/react-query';
import { api, request } from './client';
import {
  qk,
  type EventFilter,
  type HistoryQuery,
  type JobListFilter,
  type OperationFilter,
  type SearchParams,
} from './keys';
import { applyItemToDetail, applyItemToPages, mergeItem, upsertBy, type ItemPages } from './wsReducer';
import { useLiveConnected } from './ws';
import type {
  ApiKeyOut,
  AppSettings,
  AuthStatus,
  BulkItemAction,
  Candidate,
  Credentials,
  EventOut,
  ExecuteRequest,
  FormatCatalog,
  FormatIn,
  FormatOut,
  FormatValidateRequest,
  FormatValidateResponse,
  FsListing,
  HealthOut,
  HistoryPage,
  HomeActionRequest,
  HomeOut,
  HomeClearedResult,
  HomeRefreshResult,
  ItemRematch,
  ItemSearch,
  ItemStatus,
  ItemUpdate,
  JobCreate,
  JobDetail,
  JobItemFilter,
  JobItemOut,
  JobItemPage,
  JobOut,
  MediaServerConfig,
  MediaServerTest,
  OperationOut,
  ParsedMedia,
  PasswordChange,
  PreviewItem,
  PreviewRequest,
  ProvidersIn,
  ProvidersOut,
  ProviderStatus,
  StartNowRequest,
  StartNowResult,
  WatchFolderIn,
  WatchFolderOut,
} from './types';

/* ================================================================== health & auth */

export const useHealth = () => useQuery({ queryKey: qk.health, queryFn: () => api.get<HealthOut>('/health') });

export const useAuthStatus = () =>
  useQuery({
    queryKey: qk.authStatus,
    queryFn: () => api.get<AuthStatus>('/auth/status'),
    staleTime: 60_000,
    retry: 1,
    meta: { silent: true },
  });

function useAuthMutation(path: '/auth/setup' | '/auth/login') {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Credentials) => api.post<AuthStatus>(path, body),
    onSuccess: (status) => {
      qc.setQueryData(qk.authStatus, status);
    },
    meta: { silent: true },
  });
}

export const useSetup = () => useAuthMutation('/auth/setup');
export const useLogin = () => useAuthMutation('/auth/login');

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<AuthStatus>('/auth/logout'),
    onSuccess: (status) => {
      qc.clear();
      qc.setQueryData(qk.authStatus, status);
    },
    meta: { errorTitle: 'Sign out failed' },
  });
}

/** "Sign out everywhere": revokes every session cookie, this browser's included. */
export function useLogoutAll() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<AuthStatus>('/auth/logout-all'),
    onSuccess: (status) => {
      qc.clear();
      qc.setQueryData(qk.authStatus, status);
    },
    meta: { errorTitle: 'Could not sign out everywhere' },
  });
}

/** Change the password; other sessions are signed out, this browser gets a fresh cookie. */
export function useChangePassword() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: PasswordChange) => api.post<AuthStatus>('/auth/password', body),
    onSuccess: (status) => qc.setQueryData(qk.authStatus, status),
    // A wrong current password is shown inline by the form.
    meta: { silent: true, successTitle: 'Password changed: other sessions were signed out' },
  });
}

/* ================================================================== home (flow board) */

/** Polling cadence of the Home board without the live socket. */
export const HOME_POLL_MS = 5_000;
/**
 * Polling cadence while the socket is open: WS pushes already invalidate Home (see wsReducer), so
 * this only catches what no message covers (a download growing in the watch folder).
 */
export const HOME_POLL_LIVE_MS = 60_000;

/** GET /home: polled while the tab is visible (paused in the background), slowly while the socket is open. */
export function useHome() {
  const live = useLiveConnected();
  return useQuery({
    queryKey: qk.home,
    queryFn: ({ signal }) => api.get<HomeOut>('/home', undefined, signal),
    refetchInterval: live ? HOME_POLL_LIVE_MS : HOME_POLL_MS,
    refetchIntervalInBackground: false,
    staleTime: 2_000,
    // The board shows a quiet inline notice instead of a toast every poll.
    meta: { silent: true },
  });
}

/** One-click action on a "Needs you" card; the card leaves the board optimistically. */
export function useHomeAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: HomeActionRequest) => api.post<{ ok: boolean }>('/home/actions', body),
    onMutate: async (body) => {
      await qc.cancelQueries({ queryKey: qk.home });
      const prev = qc.getQueryData<HomeOut>(qk.home);
      if (prev) {
        const needs_you = prev.needs_you.filter((c) => c.key !== body.key);
        // A group card stands for many files.
        const removed = prev.needs_you.filter((c) => c.key === body.key).reduce((n, c) => n + (c.count ?? 1), 0);
        qc.setQueryData<HomeOut>(qk.home, {
          ...prev,
          needs_you,
          needs_you_count: Math.max(0, prev.needs_you_count - removed),
        });
      }
      return { prev, card: prev?.needs_you.find((c) => c.key === body.key) };
    },
    onError: (_e, _b, ctx) => {
      if (ctx?.prev) qc.setQueryData(qk.home, ctx.prev);
    },
    onSettled: (_d, _e, _b, ctx) => {
      void qc.invalidateQueries({ queryKey: qk.home });
      void qc.invalidateQueries({ queryKey: qk.history.all });
      const jobId = ctx?.card?.job_id;
      if (typeof jobId === 'number') {
        void qc.invalidateQueries({ queryKey: qk.jobs.detail(jobId) });
        void qc.invalidateQueries({ queryKey: qk.jobs.itemLists(jobId) });
      }
      const itemId = ctx?.card?.item_id;
      if (typeof itemId === 'number') void qc.invalidateQueries({ queryKey: qk.items.detail(itemId) });
    },
    meta: { errorTitle: 'Could not do that' },
  });
}

/**
 * The Refresh button: re-check the disk (cards for files that are gone clear), start whatever has
 * finished downloading in the watch folders, then reload the board.
 */
export function useRefreshHome() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<HomeRefreshResult>('/home/refresh'),
    onSettled: () => qc.invalidateQueries({ queryKey: qk.home }),
    meta: { errorTitle: 'Could not refresh' },
  });
}

/** "Dismiss all" on the Needs review column; the list empties optimistically. */
export function useDismissAllHome() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<HomeClearedResult>('/home/dismiss-all'),
    onMutate: async () => {
      await qc.cancelQueries({ queryKey: qk.home });
      const prev = qc.getQueryData<HomeOut>(qk.home);
      if (prev) qc.setQueryData<HomeOut>(qk.home, { ...prev, needs_you: [], needs_you_count: 0 });
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(qk.home, ctx.prev);
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: qk.home });
      void qc.invalidateQueries({ queryKey: qk.history.all });
    },
    meta: { errorTitle: 'Could not dismiss the list' },
  });
}

/** Skip the rest of the settle wait for one incoming file (or the whole folder without `path`). */
export function useStartNow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ folderId, path }: { folderId: number; path?: string }) =>
      api.post<StartNowResult>(`/watch-folders/${folderId}/start-now`, (path ? { path } : {}) satisfies StartNowRequest),
    onSettled: () => void qc.invalidateQueries({ queryKey: qk.home }),
    meta: { errorTitle: 'Could not start the file' },
  });
}

/* ================================================================== history */

export const HISTORY_PAGE_SIZE = 50;

/** GET /history, newest first, paged with the `before` cursor ("Show older"). */
export const useHistory = (h: HistoryQuery) =>
  useInfiniteQuery({
    queryKey: qk.history.list(h),
    queryFn: ({ pageParam, signal }) =>
      api.get<HistoryPage>(
        '/history',
        { filter: h.filter, q: h.q || undefined, kind: h.kind, before: pageParam ?? undefined, limit: HISTORY_PAGE_SIZE },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_before ?? undefined,
    placeholderData: keepPreviousData,
  });

/* ================================================================== jobs */

export const useJobs = (filter: JobListFilter = {}) =>
  useQuery({
    queryKey: qk.jobs.list(filter),
    queryFn: () => api.get<JobOut[]>('/jobs', { status: filter.status, limit: filter.limit, offset: filter.offset }),
    placeholderData: keepPreviousData,
  });

/** The job header (status, counts, progress) without items; page items with `useJobItems`. */
export const useJob = (id: number | undefined) =>
  useQuery({
    queryKey: qk.jobs.detail(id ?? -1),
    queryFn: () => api.get<JobDetail>(`/jobs/${id}`, { limit: 0 }),
    enabled: id !== undefined && Number.isFinite(id),
  });

export const ITEM_PAGE_SIZE = 250;

/** Slim items of a job, filtered server-side and loaded page by page (keyset on item id). */
export const useJobItems = (jobId: number | undefined, filter: JobItemFilter = {}) =>
  useInfiniteQuery({
    queryKey: qk.jobs.items(jobId ?? -1, filter),
    queryFn: ({ pageParam, signal }) =>
      api.get<JobItemPage>(
        `/jobs/${jobId}/items`,
        {
          status: filter.statuses?.length ? filter.statuses.join(',') : undefined,
          q: filter.q || undefined,
          limit: ITEM_PAGE_SIZE,
          after: pageParam ?? undefined,
        },
        signal,
      ),
    initialPageParam: null as number | null,
    getNextPageParam: (last) => last.next_after ?? undefined,
    enabled: jobId !== undefined && Number.isFinite(jobId),
    placeholderData: keepPreviousData,
  });

/** The full item (every candidate) for the inspector; `placeholder` (the slim row) shows meanwhile. */
export const useItem = (id: number | null, placeholder?: JobItemOut | null) =>
  useQuery({
    queryKey: qk.items.detail(id ?? -1),
    queryFn: ({ signal }) => api.get<JobItemOut>(`/items/${id}`, undefined, signal),
    enabled: id !== null,
    placeholderData: placeholder && placeholder.id === id ? placeholder : undefined,
    staleTime: 30_000,
  });

/** Write a JobOut into every cache that shows it (detail + lists). */
export function writeJob(qc: QueryClient, job: JobOut) {
  qc.setQueryData<JobDetail>(qk.jobs.detail(job.id), (d) => (d ? { ...d, ...job, items: d.items } : d));
  for (const [key, list] of qc.getQueriesData<JobOut[]>({ queryKey: qk.jobs.lists })) {
    if (Array.isArray(list) && list.some((j) => j.id === job.id)) {
      qc.setQueryData<JobOut[]>(key, upsertBy(list, job, (j) => j.id));
    }
  }
}

export function useCreateJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: JobCreate) => api.post<JobOut>('/jobs', body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.jobs.lists });
      void qc.invalidateQueries({ queryKey: qk.home });
    },
    meta: { errorTitle: 'Could not create the job' },
  });
}

export function useDeleteJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.delete(`/jobs/${id}`),
    onSuccess: (_d, id) => {
      qc.removeQueries({ queryKey: qk.jobs.detail(id), exact: true });
      void qc.invalidateQueries({ queryKey: qk.jobs.lists });
      void qc.invalidateQueries({ queryKey: qk.home });
    },
    meta: { errorTitle: 'Could not delete the job', successTitle: 'Job deleted' },
  });
}

function useJobAction<V>(build: (v: V) => Promise<JobOut>, errorTitle: string, successTitle?: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: build,
    onSuccess: (job) => writeJob(qc, job),
    meta: { errorTitle, successTitle },
  });
}

export const useExecuteJob = () =>
  useJobAction(
    ({ id, body }: { id: number; body: ExecuteRequest }) => api.post<JobOut>(`/jobs/${id}/execute`, body),
    'Could not start renaming',
  );
export const useCancelJob = () =>
  useJobAction((id: number) => api.post<JobOut>(`/jobs/${id}/cancel`), 'Could not cancel the job', 'Job cancelled');
export const useRematchJob = () =>
  useJobAction((id: number) => api.post<JobOut>(`/jobs/${id}/rematch`), 'Could not re-match the job', 'Re-matching');

/* ================================================================== items */

const pagesFilter = (key: QueryKey) => (key[3] && typeof key[3] === 'object' ? key[3] : {}) as JobItemFilter;
const isPages = (d: unknown): d is ItemPages => !!d && Array.isArray((d as ItemPages).pages);

/**
 * Write an item (full or slim) into every cache that shows it: the job detail, the paged item
 * lists and the full-item cache. A slim item never replaces full candidate data (`mergeItem`).
 */
export function writeItem(qc: QueryClient, item: JobItemOut) {
  qc.setQueryData<JobDetail>(qk.jobs.detail(item.job_id), (d) => (d ? applyItemToDetail(d, item) : d));
  for (const [key, data] of qc.getQueriesData<ItemPages>({ queryKey: qk.jobs.itemLists(item.job_id) })) {
    if (isPages(data)) qc.setQueryData<ItemPages>(key, applyItemToPages(data, item, pagesFilter(key)));
  }
  qc.setQueryData<JobItemOut>(qk.items.detail(item.id), (cur) => (cur || !item.slim ? mergeItem(cur, item) : cur));
}

/** `writeItem` for many items at once: one pass per cache, replacing items that are already there. */
export function writeItems(qc: QueryClient, items: readonly JobItemOut[]) {
  if (items.length <= 20) {
    items.forEach((i) => writeItem(qc, i));
    return;
  }
  const byId = new Map(items.map((i) => [i.id, i]));
  const swap = (list: JobItemOut[]) => {
    let changed = false;
    const next = list.map((cur) => {
      const inc = byId.get(cur.id);
      if (!inc) return cur;
      changed = true;
      return mergeItem(cur, inc);
    });
    return changed ? next : list;
  };
  for (const jobId of new Set(items.map((i) => i.job_id))) {
    qc.setQueryData<JobDetail>(qk.jobs.detail(jobId), (d) => (d ? { ...d, items: swap(d.items) } : d));
    for (const [key, data] of qc.getQueriesData<ItemPages>({ queryKey: qk.jobs.itemLists(jobId) })) {
      if (isPages(data)) qc.setQueryData<ItemPages>(key, { ...data, pages: data.pages.map((p) => ({ ...p, items: swap(p.items) })) });
    }
  }
  for (const item of items) {
    qc.setQueryData<JobItemOut>(qk.items.detail(item.id), (cur) => (cur ? mergeItem(cur, item) : cur));
  }
}

/**
 * Tab counts come from the job header (`counts`), which the server pushes as `job.updated`; refetch
 * the cheap header (`limit=0`) after item mutations too, so counts stay right if the socket is down.
 */
function refreshJobHeader(qc: QueryClient, jobId: number) {
  void qc.invalidateQueries({ queryKey: qk.jobs.detail(jobId), exact: true });
}

/** Every cache entry an item write can touch (for optimistic rollback). */
function snapshotItemCaches(qc: QueryClient, jobId: number | null, itemId?: number) {
  return [
    ...qc.getQueriesData<unknown>({ queryKey: jobId === null ? ['jobs', 'detail'] : qk.jobs.detail(jobId) }),
    ...qc.getQueriesData<unknown>({ queryKey: jobId === null ? ['jobs', 'items'] : qk.jobs.itemLists(jobId) }),
    ...(itemId !== undefined ? qc.getQueriesData<unknown>({ queryKey: qk.items.detail(itemId) }) : []),
  ];
}

/** Local, optimistic version of an ItemUpdate. */
export function optimisticItem(item: JobItemOut, patch: ItemUpdate): JobItemOut {
  const next: JobItemOut = { ...item };
  if (patch.status) next.status = patch.status;
  if (patch.selected_candidate_id !== undefined && patch.selected_candidate_id !== null) {
    next.selected_candidate_id = patch.selected_candidate_id;
    const c = item.candidates.find((x) => x.id === patch.selected_candidate_id);
    if (c) next.confidence = c.score;
  }
  if (patch.proposed_path) {
    next.proposed_path = patch.proposed_path;
    next.path_overridden = true;
  }
  if (patch.reset_path) next.path_overridden = false;
  return next;
}

/** PATCH /items/{id} with an optimistic cache update and rollback on error. */
export function useUpdateItem() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ item, patch }: { item: JobItemOut; patch: ItemUpdate }) =>
      api.patch<JobItemOut>(`/items/${item.id}`, patch),
    onMutate: async ({ item, patch }) => {
      await qc.cancelQueries({ queryKey: qk.items.detail(item.id) });
      const snapshots = snapshotItemCaches(qc, item.job_id, item.id);
      const current = qc.getQueryData<JobItemOut>(qk.items.detail(item.id)) ?? item;
      writeItem(qc, optimisticItem(current, patch));
      return { snapshots };
    },
    onError: (_e, _v, ctx) => {
      ctx?.snapshots.forEach(([key, d]) => qc.setQueryData(key, d));
    },
    onSuccess: (saved) => {
      writeItem(qc, saved);
      refreshJobHeader(qc, saved.job_id);
    },
    meta: { errorTitle: 'Could not update the item' },
  });
}

export function useRematchItem() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: number; body: ItemRematch }) => api.post<JobItemOut>(`/items/${id}/rematch`, body),
    onSuccess: (item) => {
      writeItem(qc, item);
      refreshJobHeader(qc, item.job_id);
    },
    meta: { errorTitle: 'Re-match failed' },
  });
}

export const useSearchItem = () =>
  useMutation({
    mutationFn: ({ id, body }: { id: number; body: ItemSearch }) => api.post<Candidate[]>(`/items/${id}/search`, body),
    meta: { errorTitle: 'Search failed' },
  });

export function useBulkItems() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: BulkItemAction) => api.post<JobItemOut[]>('/items/bulk', body),
    onMutate: async (body) => {
      // Optimistic for approve/skip/review: flip statuses in every cached detail / item page.
      if (body.action === 'rematch') return {};
      const status: ItemStatus = body.action === 'approve' ? 'approved' : body.action === 'skip' ? 'skipped' : 'review';
      const ids = body.item_ids ? new Set(body.item_ids) : null;
      const hits = (i: JobItemOut) =>
        ids ? ids.has(i.id) : i.job_id === body.job_id && (!body.statuses?.length || body.statuses.includes(i.status));
      const flip = (list: JobItemOut[]) =>
        list.some(hits) ? list.map((i) => (hits(i) ? { ...i, status } : i)) : list;
      const snapshots = snapshotItemCaches(qc, ids ? null : (body.job_id ?? null));
      for (const [key, d] of snapshots) {
        if (isPages(d)) {
          // The filter form with text: only the lists fetched with that same text are known to match.
          if (!ids && body.q && pagesFilter(key).q !== body.q) continue;
          qc.setQueryData<ItemPages>(key, { ...d, pages: d.pages.map((p) => ({ ...p, items: flip(p.items) })) });
        } else if (d && Array.isArray((d as JobDetail).items) && !body.q) {
          qc.setQueryData<JobDetail>(key, { ...(d as JobDetail), items: flip((d as JobDetail).items) });
        }
      }
      if (ids) {
        for (const id of ids) qc.setQueryData<JobItemOut>(qk.items.detail(id), (cur) => (cur ? { ...cur, status } : cur));
      }
      return { snapshots };
    },
    onError: (_e, _b, ctx) => {
      ctx?.snapshots?.forEach(([key, d]) => qc.setQueryData(key, d));
    },
    onSuccess: (items, body) => {
      if (body.item_ids) {
        writeItems(qc, items);
        new Set(items.map((i) => i.job_id)).forEach((jobId) => refreshJobHeader(qc, jobId));
        return;
      }
      // Filter form (possibly thousands of items): refetch what is on screen instead.
      if (typeof body.job_id === 'number') {
        void qc.invalidateQueries({ queryKey: qk.jobs.itemLists(body.job_id) });
        void qc.invalidateQueries({ queryKey: qk.jobs.detail(body.job_id) });
      }
      void qc.invalidateQueries({ queryKey: qk.items.all });
    },
    meta: { errorTitle: 'Bulk action failed' },
  });
}

/* ================================================================== preview / parse / search */

export const usePreview = () =>
  useMutation({
    mutationFn: (body: PreviewRequest) => api.post<PreviewItem[]>('/preview', body),
    meta: { errorTitle: 'Preview failed' },
  });

export const useParse = () =>
  useMutation({
    mutationFn: (names: string[]) => api.post<ParsedMedia[]>('/parse', { names }),
    meta: { errorTitle: 'Parse failed' },
  });

export const useSearch = (params: SearchParams | null) =>
  useQuery({
    queryKey: qk.search(params ?? { q: '' }),
    queryFn: () =>
      api.get<Candidate[]>('/search', {
        q: params?.q,
        kind: params?.kind,
        year: params?.year,
        provider: params?.provider,
      }),
    enabled: !!params && params.q.trim().length > 0,
  });

/* ================================================================== formats */

export const useFormats = () => useQuery({ queryKey: qk.formats.list, queryFn: () => api.get<FormatOut[]>('/formats') });

export const useFormatCatalog = () =>
  useQuery({
    queryKey: qk.formats.catalog,
    queryFn: () => api.get<FormatCatalog>('/formats/catalog'),
    staleTime: Infinity,
  });

/** Live validation. Callers debounce `expression` (250 ms, SPEC §9.4). */
export const useValidateFormat = (req: FormatValidateRequest | null) =>
  useQuery({
    queryKey: qk.formats.validate(req?.expression ?? '', req?.kind ?? 'unknown', req?.samples),
    queryFn: ({ signal }) => request<FormatValidateResponse>('POST', '/formats/validate', { body: req, signal }),
    enabled: !!req && req.expression.trim().length > 0,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
    retry: false,
    meta: { silent: true },
  });

function useFormatMutation<V>(fn: (v: V) => Promise<FormatOut | void>, errorTitle: string, successTitle?: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.formats.list }),
    meta: { errorTitle, successTitle },
  });
}

export const useCreateFormat = () =>
  useFormatMutation((body: FormatIn) => api.post<FormatOut>('/formats', body), 'Could not save the format', 'Format saved');
export const useUpdateFormat = () =>
  useFormatMutation(
    ({ id, body }: { id: number; body: FormatIn }) => api.put<FormatOut>(`/formats/${id}`, body),
    'Could not save the format',
    'Format saved',
  );
export const useDeleteFormat = () =>
  useFormatMutation((id: number) => api.delete(`/formats/${id}`), 'Could not delete the format', 'Format deleted');

/* ================================================================== operations (history) */

export const useOperations = (filter: OperationFilter = {}) =>
  useQuery({
    queryKey: qk.operations.list(filter),
    queryFn: () =>
      api.get<OperationOut[]>('/operations', { job_id: filter.job_id, limit: filter.limit, offset: filter.offset }),
    placeholderData: keepPreviousData,
  });

export function useUndoOperation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.post<OperationOut>(`/operations/${id}/undo`),
    onSuccess: (op) => {
      for (const [key, list] of qc.getQueriesData<OperationOut[]>({ queryKey: qk.operations.all })) {
        if (Array.isArray(list)) qc.setQueryData<OperationOut[]>(key, upsertBy(list, op, (o) => o.id));
      }
      void qc.invalidateQueries({ queryKey: qk.home });
      void qc.invalidateQueries({ queryKey: qk.history.all });
      if (op.job_id !== null) {
        void qc.invalidateQueries({ queryKey: qk.jobs.detail(op.job_id) });
        void qc.invalidateQueries({ queryKey: qk.jobs.itemLists(op.job_id) });
      }
      if (op.item_id !== null) void qc.invalidateQueries({ queryKey: qk.items.detail(op.item_id) });
    },
    meta: { errorTitle: 'Undo failed', successTitle: 'Rename undone' },
  });
}

/* ================================================================== watch folders */

export const useWatchFolders = () =>
  useQuery({ queryKey: qk.watchFolders, queryFn: () => api.get<WatchFolderOut[]>('/watch-folders') });

function useWatchMutation<V, R>(fn: (v: V) => Promise<R>, errorTitle: string, successTitle?: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.watchFolders });
      void qc.invalidateQueries({ queryKey: qk.home });
    },
    meta: { errorTitle, successTitle },
  });
}

export const useCreateWatchFolder = () =>
  useWatchMutation(
    (body: WatchFolderIn) => api.post<WatchFolderOut>('/watch-folders', body),
    'Could not add the watch folder',
    'Watch folder added',
  );
export const useUpdateWatchFolder = () =>
  useWatchMutation(
    ({ id, body }: { id: number; body: WatchFolderIn }) => api.put<WatchFolderOut>(`/watch-folders/${id}`, body),
    'Could not update the watch folder',
  );
export const useDeleteWatchFolder = () =>
  useWatchMutation((id: number) => api.delete(`/watch-folders/${id}`), 'Could not remove the watch folder', 'Watch folder removed');

export function useScanWatchFolder() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.post<JobOut>(`/watch-folders/${id}/scan`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.jobs.lists }),
    meta: { errorTitle: 'Scan failed' },
  });
}

/* ================================================================== settings & providers */

export const useSettings = () => useQuery({ queryKey: qk.settings, queryFn: () => api.get<AppSettings>('/settings') });

export function useUpdateSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: AppSettings) => api.put<AppSettings>('/settings', body),
    onSuccess: (s) => qc.setQueryData(qk.settings, s),
    meta: { errorTitle: 'Could not save settings', successTitle: 'Settings saved' },
  });
}

/** Reveals the webhook API key. Disabled until the user asks for it. */
export const useApiKey = (enabled: boolean) =>
  useQuery({
    queryKey: qk.apiKey,
    queryFn: () => api.get<ApiKeyOut>('/settings/api-key'),
    enabled,
    staleTime: Infinity,
  });

export function useRotateApiKey() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<ApiKeyOut>('/settings/api-key'),
    onSuccess: (k) => qc.setQueryData(qk.apiKey, k),
    meta: { errorTitle: 'Could not rotate the API key', successTitle: 'New API key generated' },
  });
}

export const useProviders = () => useQuery({ queryKey: qk.providers, queryFn: () => api.get<ProvidersOut>('/providers') });

export function useUpdateProviders() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ProvidersIn) => api.put<ProvidersOut>('/providers', body),
    onSuccess: (p) => qc.setQueryData(qk.providers, p),
    meta: { errorTitle: 'Could not save providers', successTitle: 'Providers saved' },
  });
}

export function useTestProvider() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => api.post<ProviderStatus>(`/providers/${encodeURIComponent(name)}/test`),
    onSuccess: (status) => {
      qc.setQueryData<ProvidersOut>(qk.providers, (p) =>
        p ? { ...p, statuses: upsertBy(p.statuses, status, (s) => s.name) } : p,
      );
    },
    meta: { errorTitle: 'Provider test failed' },
  });
}

export const useTestMediaServer = () =>
  useMutation({
    mutationFn: (cfg: MediaServerConfig) => api.post<MediaServerTest>('/mediaservers/test', cfg),
    meta: { errorTitle: 'Media server test failed' },
  });

/* ================================================================== events, fs, demo */

export const useEvents = (filter: EventFilter = {}) =>
  useQuery({
    queryKey: qk.events.list(filter),
    queryFn: () =>
      api.get<EventOut[]>('/events', {
        level: filter.level,
        category: filter.category,
        job_id: filter.job_id,
        limit: filter.limit,
        offset: filter.offset,
      }),
    placeholderData: keepPreviousData,
  });

export const useFsList = (path: string | undefined, enabled = true) =>
  useQuery({
    queryKey: qk.fs(path),
    queryFn: () => api.get<FsListing>('/fs/list', { path }),
    enabled,
    staleTime: 5_000,
    retry: false,
  });

export function useDemoReset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<{ ok: boolean }>('/demo/reset'),
    onSuccess: () => void qc.invalidateQueries(),
    meta: { errorTitle: 'Demo reset failed', successTitle: 'Demo library rebuilt' },
  });
}
