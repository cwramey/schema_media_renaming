import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { notifyManager, QueryClient, QueryObserver, type QueryKey } from '@tanstack/react-query';
import { qk } from './keys';
import {
  applyEventToList,
  applyItemToDetail,
  applyItemToPages,
  applyJobToList,
  applyWsMessage,
  applyWsMessages,
  coalesceWsMessages,
  debouncedInvalidate,
  mergeItem,
  parseWsFrame,
  removeBy,
  resyncNow,
  RESYNC_KEEP_PAGES,
  schedule,
  scheduleResync,
  trimInfiniteQueries,
  upsertBy,
  type ItemPages,
} from './wsReducer';
import { backoffDelay, MessageBatcher, wsUrl } from './ws';
import type {
  Candidate,
  EventOut,
  JobDetail,
  JobItemOut,
  JobItemPage,
  JobOut,
  OperationOut,
  ParsedMedia,
  ProviderStatus,
  ProvidersOut,
  WatchFolderOut,
  WsMessage,
} from './types';

/* ------------------------------------------------------------------ factories */

const T = '2026-10-25T12:00:00Z';

const job = (id: number, over: Partial<JobOut> = {}): JobOut => ({
  id,
  label: `Job ${id}`,
  source: 'manual',
  status: 'review',
  kind: null,
  mode: 'move',
  format_id: null,
  dest_root: null,
  auto_execute: false,
  created_at: T,
  updated_at: T,
  finished_at: null,
  counts: {},
  total: 0,
  progress: 0,
  error: null,
  ...over,
});

const parsed = (name: string): ParsedMedia => ({
  path: `/in/${name}`,
  filename: name,
  extension: 'mkv',
  kind: 'series',
  title: 'Show',
  alt_titles: [],
  year: null,
  season: 1,
  episodes: [1],
  absolute: [],
  air_date: null,
  special: false,
  special_title: null,
  part: null,
  edition: null,
  release_group: null,
  crc32: null,
  version: null,
  quality: {
    resolution: null,
    source: null,
    remux: false,
    video_codec: null,
    bit_depth: null,
    hdr: [],
    audio_codec: null,
    audio_channels: null,
    atmos: false,
    streaming_service: null,
    proper: false,
    repack: false,
    s3d: null,
  },
  languages: [],
  is_sample: false,
  is_extra: false,
  folder_title: null,
  confidence: 1,
  tokens: [],
});

const item = (id: number, jobId: number, over: Partial<JobItemOut> = {}): JobItemOut => ({
  id,
  job_id: jobId,
  status: 'review',
  source_path: `/in/f${id}.mkv`,
  filename: `f${id}.mkv`,
  parsed: parsed(`f${id}.mkv`),
  candidates: [],
  selected_candidate_id: null,
  confidence: 0.5,
  proposed_path: null,
  proposed_relative: null,
  path_overridden: false,
  final_path: null,
  sidecars: [],
  crc_ok: null,
  probe: null,
  warnings: [],
  error: null,
  updated_at: T,
  ...over,
});

const detail = (j: JobOut, items: JobItemOut[]): JobDetail => ({ ...j, items });

const op = (id: number, jobId: number | null): OperationOut => ({
  id,
  job_id: jobId,
  item_id: null,
  mode: 'move',
  source: '/in/a.mkv',
  destination: '/lib/A/a.mkv',
  status: 'done',
  bytes: 10,
  sidecars: [],
  error: null,
  created_at: T,
  undone_at: null,
});

const ev = (id: number, over: Partial<EventOut> = {}): EventOut => ({
  id,
  ts: T,
  level: 'info',
  category: 'job',
  message: `event ${id}`,
  job_id: null,
  data: {},
  ...over,
});

const provider = (name: string, over: Partial<ProviderStatus> = {}): ProviderStatus => ({
  name,
  label: name.toUpperCase(),
  kinds: ['series'],
  requires_key: false,
  configured: true,
  enabled: true,
  healthy: null,
  last_error: null,
  last_checked: null,
  rate_limit: null,
  requests_made: 0,
  cache_hits: 0,
  ...over,
});

const watcher = (id: number, over: Partial<WatchFolderOut> = {}): WatchFolderOut => ({
  id,
  path: `/watch/${id}`,
  enabled: true,
  recursive: true,
  kind: null,
  auto_execute: true,
  settle_seconds: 30,
  format_id: null,
  dest_root: null,
  mode: null,
  status: 'watching',
  last_event_at: null,
  last_error: null,
  files_seen: 0,
  ...over,
});

/* ------------------------------------------------------------------ pure helpers */

describe('list helpers', () => {
  it('upsertBy replaces in place or inserts at the front/back, with a limit', () => {
    const list = [{ id: 1, v: 'a' }, { id: 2, v: 'b' }];
    expect(upsertBy(list, { id: 2, v: 'B' }, (x) => x.id)).toEqual([{ id: 1, v: 'a' }, { id: 2, v: 'B' }]);
    expect(upsertBy(list, { id: 3, v: 'c' }, (x) => x.id).map((x) => x.id)).toEqual([3, 1, 2]);
    expect(upsertBy(list, { id: 3, v: 'c' }, (x) => x.id, { prepend: false }).map((x) => x.id)).toEqual([1, 2, 3]);
    expect(upsertBy(list, { id: 3, v: 'c' }, (x) => x.id, { limit: 2 }).map((x) => x.id)).toEqual([3, 1]);
    expect(list).toHaveLength(2); // inputs are never mutated
  });

  it('removeBy returns the same array when nothing is removed', () => {
    const list = [{ id: 1 }];
    expect(removeBy(list, 9, (x) => x.id)).toBe(list);
    expect(removeBy(list, 1, (x) => x.id)).toEqual([]);
  });

  it('applyJobToList respects the status filter', () => {
    const list = [job(1, { status: 'review' })];
    expect(applyJobToList(list, job(1, { status: 'ready' }), { status: 'review' })).toEqual([]);
    expect(applyJobToList(list, job(2, { status: 'ready' }), { status: 'review' })).toBe(list);
    expect(applyJobToList(list, job(2, { status: 'review' }), { status: 'review' }).map((j) => j.id)).toEqual([2, 1]);
    expect(applyJobToList(list, job(1, { status: 'completed' })).map((j) => j.status)).toEqual(['completed']);
  });

  it('applyEventToList honours level/category/job filters and the page size', () => {
    const list = [ev(1), ev(2)];
    expect(applyEventToList(list, ev(3, { level: 'error' }), { level: 'warning' })).toBe(list);
    expect(applyEventToList(list, ev(3, { category: 'watcher' }), { category: 'job' })).toBe(list);
    expect(applyEventToList(list, ev(3, { job_id: 7 }), { job_id: 8 })).toBe(list);
    expect(applyEventToList(list, ev(3), { limit: 2 }).map((e) => e.id)).toEqual([3, 1]);
  });
});

/* ------------------------------------------------------------------ applyWsMessage */

describe('applyWsMessage', () => {
  let qc: QueryClient;
  let invalidated: QueryKey[];
  const apply = (msg: Parameters<typeof applyWsMessage>[1]) =>
    applyWsMessage(qc, msg, { invalidate: (k) => invalidated.push(k) });

  beforeEach(() => {
    qc = new QueryClient();
    invalidated = [];
  });

  it('ignores hello', () => {
    apply({ type: 'hello', data: { version: '1.0' } });
    expect(qc.getQueryCache().getAll()).toHaveLength(0);
    expect(invalidated).toEqual([]);
  });

  it('job.updated updates lists, the detail (keeping items) and refreshes Home', () => {
    qc.setQueryData(qk.jobs.list({}), [job(1), job(2)]);
    qc.setQueryData(qk.jobs.list({ status: 'review' }), [job(1), job(2)]);
    qc.setQueryData(qk.jobs.detail(1), detail(job(1), [item(10, 1)]));

    apply({ type: 'job.updated', data: job(1, { status: 'executing', progress: 0.5 }) });

    expect(qc.getQueryData<JobOut[]>(qk.jobs.list({}))?.find((j) => j.id === 1)?.status).toBe('executing');
    expect(qc.getQueryData<JobOut[]>(qk.jobs.list({ status: 'review' }))?.map((j) => j.id)).toEqual([2]);
    const d = qc.getQueryData<JobDetail>(qk.jobs.detail(1));
    expect(d?.progress).toBe(0.5);
    expect(d?.items.map((i) => i.id)).toEqual([10]);
    expect(invalidated).toContainEqual(qk.home);
  });

  it('job.updated adds a new job to matching lists and does not create missing caches', () => {
    qc.setQueryData(qk.jobs.list({}), [job(1)]);
    apply({ type: 'job.updated', data: job(5, { status: 'queued' }) });
    expect(qc.getQueryData<JobOut[]>(qk.jobs.list({}))?.map((j) => j.id)).toEqual([5, 1]);
    expect(qc.getQueryData(qk.jobs.detail(5))).toBeUndefined();
    expect(qc.getQueryData(qk.home)).toBeUndefined();
  });

  it('job.deleted removes the job everywhere', () => {
    qc.setQueryData(qk.jobs.list({}), [job(1), job(2)]);
    qc.setQueryData(qk.jobs.detail(1), detail(job(1), []));
    apply({ type: 'job.deleted', data: { id: 1 } });
    expect(qc.getQueryData<JobOut[]>(qk.jobs.list({}))?.map((j) => j.id)).toEqual([2]);
    expect(qc.getQueryData(qk.jobs.detail(1))).toBeUndefined();
    expect(invalidated).toContainEqual(qk.home);
  });

  it('item.updated replaces the item in its job detail, or appends it', () => {
    qc.setQueryData(qk.jobs.detail(1), detail(job(1), [item(10, 1), item(11, 1)]));
    qc.setQueryData(qk.jobs.detail(2), detail(job(2), [item(20, 2)]));

    apply({ type: 'item.updated', data: item(11, 1, { status: 'approved' }) });
    apply({ type: 'item.updated', data: item(12, 1, { status: 'matched' }) });

    const d1 = qc.getQueryData<JobDetail>(qk.jobs.detail(1));
    expect(d1?.items.map((i) => [i.id, i.status])).toEqual([
      [10, 'review'],
      [11, 'approved'],
      [12, 'matched'],
    ]);
    expect(qc.getQueryData<JobDetail>(qk.jobs.detail(2))?.items.map((i) => i.id)).toEqual([20]);
    expect(invalidated).toContainEqual(qk.home);
    expect(invalidated).not.toContainEqual(qk.history.all);
    apply({ type: 'item.updated', data: item(11, 1, { status: 'skipped' }) });
    expect(invalidated).toContainEqual(qk.history.all);
  });

  it('operation.created prepends to matching operation lists and refreshes Home and History', () => {
    qc.setQueryData(qk.operations.list({}), [op(1, 1)]);
    qc.setQueryData(qk.operations.list({ job_id: 1 }), [op(1, 1)]);
    qc.setQueryData(qk.operations.list({ job_id: 2 }), [op(5, 2)]);

    apply({ type: 'operation.created', data: op(2, 1) });

    expect(qc.getQueryData<OperationOut[]>(qk.operations.list({}))?.map((o) => o.id)).toEqual([2, 1]);
    expect(qc.getQueryData<OperationOut[]>(qk.operations.list({ job_id: 1 }))?.map((o) => o.id)).toEqual([2, 1]);
    expect(qc.getQueryData<OperationOut[]>(qk.operations.list({ job_id: 2 }))?.map((o) => o.id)).toEqual([5]);
    expect(invalidated).toContainEqual(qk.home);
    expect(invalidated).toContainEqual(qk.history.all);
  });

  it('event prepends to filtered logs and refreshes Home', () => {
    qc.setQueryData(qk.events.list({ limit: 50 }), [ev(1)]);
    qc.setQueryData(qk.events.list({ level: 'error', limit: 50 }), []);

    apply({ type: 'event', data: ev(2, { level: 'info' }) });
    apply({ type: 'event', data: ev(3, { level: 'error', message: 'boom' }) });

    expect(qc.getQueryData<EventOut[]>(qk.events.list({ limit: 50 }))?.map((e) => e.id)).toEqual([3, 2, 1]);
    expect(qc.getQueryData<EventOut[]>(qk.events.list({ level: 'error', limit: 50 }))?.map((e) => e.id)).toEqual([3]);
    expect(invalidated).toContainEqual(qk.home);
  });

  it('event does not duplicate an event that is already listed', () => {
    qc.setQueryData(qk.events.list({}), [ev(1)]);
    apply({ type: 'event', data: ev(1, { message: 'updated' }) });
    expect(qc.getQueryData<EventOut[]>(qk.events.list({}))).toEqual([ev(1, { message: 'updated' })]);
  });

  it('watcher.updated upserts the watch-folder list and refreshes Home', () => {
    qc.setQueryData(qk.watchFolders, [watcher(1), watcher(2)]);
    apply({ type: 'watcher.updated', data: watcher(2, { status: 'error', last_error: 'gone' }) });
    expect(qc.getQueryData<WatchFolderOut[]>(qk.watchFolders)?.[1]).toMatchObject({ id: 2, status: 'error' });
    expect(invalidated).toContainEqual(qk.home);
  });

  it('provider.updated replaces the provider status by name', () => {
    const providers: ProvidersOut = {
      statuses: [provider('tmdb'), provider('tvdb')],
      priority: {},
      disabled: [],
      language: 'en',
      credentials: {},
    };
    qc.setQueryData(qk.providers, providers);
    apply({ type: 'provider.updated', data: provider('tvdb', { healthy: false, last_error: '401' }) });
    expect(qc.getQueryData<ProvidersOut>(qk.providers)?.statuses.map((s) => [s.name, s.healthy])).toEqual([
      ['tmdb', null],
      ['tvdb', false],
    ]);
    expect(invalidated).toContainEqual(qk.home);
  });

  it('uses a debounced invalidation by default', () => {
    vi.useFakeTimers();
    try {
      const spy = vi.spyOn(qc, 'invalidateQueries');
      applyWsMessage(qc, { type: 'item.updated', data: item(1, 1) });
      applyWsMessage(qc, { type: 'item.updated', data: item(2, 1) });
      expect(spy).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1100);
      expect(spy).toHaveBeenCalledTimes(1);
      // Never aborts a slow in-flight request (Home / History on the NAS).
      expect(spy).toHaveBeenCalledWith({ queryKey: qk.home }, { cancelRefetch: false });
    } finally {
      vi.useRealTimers();
    }
  });

  it('job.items_changed with ids refetches the job items, header and the open inspectors among them', () => {
    qc.setQueryData(qk.items.detail(5), item(5, 1));
    qc.setQueryData(qk.items.detail(6), item(6, 1));
    apply({ type: 'job.items_changed', data: { job_id: 1, item_ids: [5, 7] } });
    expect(invalidated).toContainEqual(qk.jobs.itemLists(1));
    expect(invalidated).toContainEqual(qk.jobs.detail(1));
    expect(invalidated).toContainEqual(qk.items.detail(5));
    expect(invalidated).not.toContainEqual(qk.items.detail(6)); // not in the batch
    expect(invalidated).not.toContainEqual(qk.items.detail(7)); // nobody shows it
    expect(invalidated).toContainEqual(qk.home);
    expect(invalidated).toContainEqual(qk.history.all);
  });

  it('job.items_changed without ids refetches every open inspector of that job', () => {
    qc.setQueryData(qk.items.detail(5), item(5, 1));
    qc.setQueryData(qk.items.detail(9), item(9, 2));
    apply({ type: 'job.items_changed', data: { job_id: 1 } });
    expect(invalidated).toContainEqual(qk.jobs.itemLists(1));
    expect(invalidated).toContainEqual(qk.items.detail(5));
    expect(invalidated).not.toContainEqual(qk.items.detail(9));
    expect(invalidated).not.toContainEqual(qk.jobs.itemLists(2));
  });

  it('resync goes through the injected (debounced) resync', () => {
    const resync = vi.fn();
    applyWsMessage(qc, { type: 'resync', data: {} }, { resync });
    expect(resync).toHaveBeenCalledTimes(1);
  });
});

/* ------------------------------------------------------------------ scheduling */

describe('debounced invalidation and resync', () => {
  let qc: QueryClient;
  const pageOf = (id: number): JobItemPage => ({
    job_id: 1,
    items: [item(id, 1)],
    total: 10,
    offset: 0,
    limit: 1,
    next_after: id,
    counts: {},
  });
  const deepList = (n: number): ItemPages => ({
    pages: Array.from({ length: n }, (_, i) => pageOf(i + 1)),
    pageParams: Array.from({ length: n }, (_, i) => (i === 0 ? null : i)),
  });

  beforeEach(() => {
    vi.useFakeTimers();
    qc = new QueryClient();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('waits for an in-flight fetch instead of aborting it, then refetches', () => {
    const spy = vi.spyOn(qc, 'invalidateQueries');
    let fetching = 1;
    vi.spyOn(qc, 'isFetching').mockImplementation(() => fetching);
    debouncedInvalidate(qc, qk.home);
    vi.advanceTimersByTime(3000);
    expect(spy).not.toHaveBeenCalled(); // /home still loading: keep waiting
    fetching = 0;
    vi.advanceTimersByTime(300);
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenCalledWith({ queryKey: qk.home }, { cancelRefetch: false });
  });

  it('gives up waiting after maxWait', () => {
    const run = vi.fn();
    schedule(qc, 'x', run, { ms: 100, maxWait: 1000, busy: () => true });
    vi.advanceTimersByTime(900);
    expect(run).not.toHaveBeenCalled();
    vi.advanceTimersByTime(400);
    expect(run).toHaveBeenCalledTimes(1);
  });

  it('a burst of resyncs refetches once, after they stop, without cancelling', () => {
    const spy = vi.spyOn(qc, 'invalidateQueries');
    for (let i = 0; i < 5; i++) {
      scheduleResync(qc);
      vi.advanceTimersByTime(1000); // each one pushes the refetch back
    }
    expect(spy).not.toHaveBeenCalled();
    vi.advanceTimersByTime(600);
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenCalledWith({ refetchType: 'active' }, { cancelRefetch: false });
  });

  it('resyncs at most maxWait after the first one even if they keep coming', () => {
    const spy = vi.spyOn(qc, 'invalidateQueries');
    for (let i = 0; i < 12; i++) {
      scheduleResync(qc);
      vi.advanceTimersByTime(1000);
    }
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('cuts deep infinite lists down before refetching them', () => {
    qc.setQueryData(qk.jobs.items(1, {}), deepList(40));
    qc.setQueryData(qk.jobs.items(2, {}), deepList(2));
    const shallow = qc.getQueryData(qk.jobs.items(2, {}));
    resyncNow(qc);
    const deep = qc.getQueryData<ItemPages>(qk.jobs.items(1, {}));
    expect(deep?.pages).toHaveLength(RESYNC_KEEP_PAGES);
    expect(deep?.pageParams).toHaveLength(RESYNC_KEEP_PAGES);
    expect(deep?.pages[0].items[0].id).toBe(1);
    expect(qc.getQueryData(qk.jobs.items(2, {}))).toBe(shallow);
  });

  it('trimInfiniteQueries leaves plain queries alone', () => {
    qc.setQueryData(qk.jobs.detail(1), detail(job(1), []));
    const before = qc.getQueryData(qk.jobs.detail(1));
    trimInfiniteQueries(qc, 1);
    expect(qc.getQueryData(qk.jobs.detail(1))).toBe(before);
  });
});

/* ------------------------------------------------------------------ batching */

describe('message batches', () => {
  it('coalesceWsMessages keeps only the last push per item / job, in order', () => {
    const msgs: WsMessage[] = [
      { type: 'item.updated', data: item(1, 1, { status: 'matched' }) },
      { type: 'job.updated', data: job(1, { progress: 0.1 }) },
      { type: 'item.updated', data: item(2, 1) },
      { type: 'item.updated', data: item(1, 1, { status: 'approved' }) },
      { type: 'job.updated', data: job(1, { progress: 0.2 }) },
      { type: 'resync', data: {} },
    ];
    const out = coalesceWsMessages(msgs);
    expect(out.map((m) => m.type)).toEqual(['item.updated', 'item.updated', 'job.updated', 'resync']);
    expect((out[1].data as JobItemOut).status).toBe('approved');
    expect((out[2].data as JobOut).progress).toBe(0.2);
  });

  it('applyWsMessages notifies observers in one tick for the whole batch', () => {
    const qc = new QueryClient();
    qc.setQueryData(qk.jobs.detail(1), detail(job(1), [item(1, 1), item(2, 1), item(3, 1)]));
    // Subscribed the way useQuery subscribes (each tick is one React render).
    const observer = new QueryObserver(qc, { queryKey: qk.jobs.detail(1), enabled: false });
    const unsubscribe = observer.subscribe(notifyManager.batchCalls(() => undefined));
    let ticks = 0;
    notifyManager.setScheduler((cb) => {
      ticks += 1;
      cb();
    });
    const pushes = (status: JobItemOut['status']): WsMessage[] =>
      [1, 2, 3].map((id) => ({ type: 'item.updated', data: item(id, 1, { status }) }));
    const opts = { invalidate: () => undefined };
    try {
      pushes('matched').forEach((m) => applyWsMessage(qc, m, opts));
      expect(ticks).toBe(3); // one by one: a render per message
      ticks = 0;
      applyWsMessages(qc, pushes('approved'), opts);
      expect(ticks).toBe(1);
      expect(qc.getQueryData<JobDetail>(qk.jobs.detail(1))?.items.map((i) => i.status)).toEqual([
        'approved',
        'approved',
        'approved',
      ]);
    } finally {
      unsubscribe();
      notifyManager.setScheduler((cb) => setTimeout(cb, 0));
    }
  });

  it('MessageBatcher hands over everything received in one go', () => {
    vi.useFakeTimers();
    try {
      const flushed: WsMessage[][] = [];
      const b = new MessageBatcher((msgs) => flushed.push(msgs), 50);
      b.push({ type: 'hello', data: { version: '1' } });
      b.push({ type: 'job.deleted', data: { id: 1 } });
      expect(flushed).toEqual([]);
      vi.advanceTimersByTime(60); // no animation frames under node: the fallback timer flushes
      expect(flushed).toHaveLength(1);
      expect(flushed[0].map((m) => m.type)).toEqual(['hello', 'job.deleted']);
      b.push({ type: 'job.deleted', data: { id: 2 } });
      b.dispose();
      vi.advanceTimersByTime(60);
      expect(flushed).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('frames and reconnect', () => {
  it('parses only well-formed frames', () => {
    expect(parseWsFrame('{"type":"hello","data":{"version":"1"}}')).toEqual({ type: 'hello', data: { version: '1' } });
    expect(parseWsFrame('not json')).toBeNull();
    expect(parseWsFrame('{"data":{}}')).toBeNull();
    expect(parseWsFrame(new ArrayBuffer(2))).toBeNull();
  });

  it('backs off exponentially with jitter, capped at 15 s', () => {
    const mid = () => 0.5; // no jitter
    expect(backoffDelay(1, mid)).toBe(500);
    expect(backoffDelay(2, mid)).toBe(1000);
    expect(backoffDelay(4, mid)).toBe(4000);
    expect(backoffDelay(20, mid)).toBe(15000);
    expect(backoffDelay(3, () => 0)).toBe(1600);
    expect(backoffDelay(3, () => 1)).toBe(2400);
  });

  it('derives the socket URL from the page location', () => {
    expect(wsUrl({ protocol: 'http:', host: 'nas:8420' })).toBe('ws://nas:8420/api/ws');
    expect(wsUrl({ protocol: 'https:', host: 'schema.example' })).toBe('wss://schema.example/api/ws');
  });
});

/* ------------------------------------------------------------------ paged items & slim pushes */

describe('paged items and slim item pushes', () => {
  const cand = (id: string, over: Partial<Candidate> = {}): Candidate => ({
    id,
    kind: 'series',
    provider: 'tvdb',
    title: 'Show',
    year: 2010,
    score: 0.9,
    movie: null,
    series: null,
    episodes: [],
    episode_order: null,
    reasons: ['title match'],
    ...over,
  });
  const fullItem = (id: number, over: Partial<JobItemOut> = {}) =>
    item(id, 1, { candidates: [cand('a'), cand('b')], candidate_count: 2, slim: false, ...over });
  const slimItem = (id: number, over: Partial<JobItemOut> = {}) =>
    item(id, 1, { candidates: [cand('a', { reasons: [] })], candidate_count: 2, slim: true, ...over });
  const page = (items: JobItemOut[], next_after: number | null, total = 99): JobItemPage => ({
    job_id: 1,
    items,
    total,
    offset: 0,
    limit: 250,
    next_after,
    counts: {},
  });
  const pages = (...ps: JobItemPage[]): ItemPages => ({ pages: ps, pageParams: ps.map((_, i) => (i === 0 ? null : i)) });
  const ids = (d: ItemPages | undefined) => d?.pages.map((p) => p.items.map((i) => i.id));

  it('mergeItem keeps full candidates when a slim item arrives', () => {
    const merged = mergeItem(fullItem(1), slimItem(1, { status: 'approved' }));
    expect(merged.status).toBe('approved');
    expect(merged.candidates.map((c) => c.id)).toEqual(['a', 'b']);
    expect(merged.slim).toBe(false);
    expect(mergeItem(slimItem(1), fullItem(1, { status: 'done' })).candidates).toHaveLength(2);
    expect(mergeItem(undefined, slimItem(1)).slim).toBe(true);
  });

  it('applyItemToDetail never grows a header-only or partial detail', () => {
    const header: JobDetail = { ...job(1), items: [], items_total: 500 };
    expect(applyItemToDetail(header, slimItem(7))).toBe(header);
    const complete: JobDetail = { ...job(1), items: [fullItem(1)], items_total: 1 };
    const grown = applyItemToDetail(complete, slimItem(2));
    expect(grown.items.map((i) => i.id)).toEqual([1, 2]);
    expect(grown.items_total).toBe(2);
    expect(applyItemToDetail(complete, slimItem(1, { status: 'skipped' })).items[0].candidates).toHaveLength(2);
  });

  it('applyItemToPages replaces in place, inserts inside the loaded range, and waits for later pages', () => {
    const data = pages(page([item(1, 1), item(3, 1)], 3), page([item(5, 1)], 5));
    const replaced = applyItemToPages(data, item(3, 1, { status: 'approved' }));
    expect(replaced.pages[0].items[1].status).toBe('approved');
    expect(ids(replaced)).toEqual([[1, 3], [5]]);
    // id 2 sits inside the loaded range: insert in order (and count it)
    const inserted = applyItemToPages(data, item(2, 1));
    expect(ids(inserted)).toEqual([[1, 2, 3], [5]]);
    expect(inserted.pages[0].total).toBe(100);
    // id 9 is past what is loaded while more pages exist: it will come with a later page
    expect(applyItemToPages(data, item(9, 1))).toBe(data);
    // once everything is loaded, new items append
    const done = pages(page([item(1, 1)], null));
    expect(ids(applyItemToPages(done, item(4, 1)))).toEqual([[1, 4]]);
  });

  it('applyItemToPages respects the status filter and leaves text searches to the server', () => {
    const data = pages(page([item(1, 1)], null));
    expect(applyItemToPages(data, item(2, 1, { status: 'done' }), { statuses: ['review'] })).toBe(data);
    expect(ids(applyItemToPages(data, item(2, 1, { status: 'review' }), { statuses: ['review'] }))).toEqual([[1, 2]]);
    expect(applyItemToPages(data, item(2, 1), { q: 'show' })).toBe(data);
    // an item already listed is updated even if it no longer matches (the page hides it)
    expect(applyItemToPages(data, item(1, 1, { status: 'done' }), { statuses: ['review'] }).pages[0].items[0].status).toBe('done');
  });

  it('applyItemToPages finds a row again in its replaced page and leaves other pages as they are', () => {
    const first = page([item(1, 1), item(2, 1)], 2);
    let data = pages(first, page([item(3, 1), item(4, 1)], null));
    data = applyItemToPages(data, item(4, 1, { status: 'approved' }));
    data = applyItemToPages(data, item(4, 1, { status: 'done' }));
    expect(data.pages[1].items.map((i) => i.status)).toEqual(['review', 'done']);
    expect(ids(data)).toEqual([[1, 2], [3, 4]]);
    expect(data.pages[0]).toBe(first);
  });

  describe('applyWsMessage item.updated', () => {
    let qc: QueryClient;
    let invalidated: QueryKey[];
    const apply = (msg: Parameters<typeof applyWsMessage>[1]) =>
      applyWsMessage(qc, msg, { invalidate: (k) => invalidated.push(k) });

    beforeEach(() => {
      qc = new QueryClient();
      invalidated = [];
    });

    it('updates every paged list of the job, using the filter in the key', () => {
      qc.setQueryData(qk.jobs.items(1, {}), pages(page([slimItem(1), slimItem(2)], null)));
      qc.setQueryData(qk.jobs.items(1, { statuses: ['review'] }), pages(page([slimItem(1)], null)));
      qc.setQueryData(qk.jobs.items(2, {}), pages(page([item(50, 2)], null)));

      apply({ type: 'item.updated', data: slimItem(2, { status: 'approved' }) });
      apply({ type: 'item.updated', data: slimItem(3, { status: 'matched' }) });

      const all = qc.getQueryData<ItemPages>(qk.jobs.items(1, {}));
      expect(all?.pages[0].items.map((i) => [i.id, i.status])).toEqual([
        [1, 'review'],
        [2, 'approved'],
        [3, 'matched'],
      ]);
      expect(ids(qc.getQueryData<ItemPages>(qk.jobs.items(1, { statuses: ['review'] })))).toEqual([[1]]);
      expect(ids(qc.getQueryData<ItemPages>(qk.jobs.items(2, {})))).toEqual([[50]]);
    });

    it('does not clobber a fully loaded item, and refetches it only when it changed', () => {
      qc.setQueryData(qk.items.detail(1), fullItem(1));

      apply({ type: 'item.updated', data: slimItem(1, { status: 'approved' }) });
      let cached = qc.getQueryData<JobItemOut>(qk.items.detail(1));
      expect(cached?.status).toBe('approved');
      expect(cached?.candidates).toHaveLength(2);
      expect(invalidated).not.toContainEqual(qk.items.detail(1)); // same updated_at: nothing new

      apply({ type: 'item.updated', data: slimItem(1, { updated_at: '2026-10-25T12:05:00Z' }) });
      cached = qc.getQueryData<JobItemOut>(qk.items.detail(1));
      expect(cached?.candidates).toHaveLength(2);
      expect(invalidated).toContainEqual(qk.items.detail(1));

      apply({ type: 'item.updated', data: slimItem(9) });
      expect(qc.getQueryData(qk.items.detail(9))).toBeUndefined(); // no cache created for slim pushes
    });

    it('job.deleted drops the paged lists', () => {
      qc.setQueryData(qk.jobs.items(1, {}), pages(page([slimItem(1)], null)));
      apply({ type: 'job.deleted', data: { id: 1 } });
      expect(qc.getQueryData(qk.jobs.items(1, {}))).toBeUndefined();
    });
  });
});
