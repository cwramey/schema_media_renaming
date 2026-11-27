/**
 * Settings-only data helpers: autosave queues with optimistic cache updates, the save-status
 * store behind the "Changes save automatically" indicator, and backup/restore.
 *
 * Every autosave runs serially per resource so responses can never arrive out of order.
 * Failures revert the optimistic change, toast with a Retry, and flip the indicator to
 * "Couldn't save — retry".
 */
import { useCallback, useSyncExternalStore } from 'react';
import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { api, errorMessage } from '@/api/client';
import { qk } from '@/api/keys';
import type {
  AppSettings,
  FormatIn,
  FormatOut,
  ProvidersIn,
  ProvidersOut,
  WatchFolderIn,
  WatchFolderOut,
} from '@/api/types';
import { toast } from '@/lib/toast';
import { KIND_PLURAL, type EditKind } from './mapping';

/* ================================================================== save status store */

export type SaveStatus = 'idle' | 'saving' | 'saved' | 'error';
export interface SaveState {
  status: SaveStatus;
  retry: (() => void) | null;
}

let state: SaveState = { status: 'idle', retry: null };
let pending = 0;
let savedTimer: ReturnType<typeof setTimeout> | null = null;
const listeners = new Set<() => void>();

function setState(next: SaveState) {
  state = next;
  listeners.forEach((l) => l());
}

function trackStart() {
  pending++;
  if (savedTimer) clearTimeout(savedTimer);
  savedTimer = null;
  setState({ status: 'saving', retry: null });
}

function trackDone() {
  pending = Math.max(0, pending - 1);
  if (pending > 0 || state.status === 'error') return;
  setState({ status: 'saved', retry: null });
  savedTimer = setTimeout(() => {
    if (state.status === 'saved') setState({ status: 'idle', retry: null });
  }, 4000);
}

function trackFail(what: string, error: unknown, retry: () => void) {
  pending = Math.max(0, pending - 1);
  setState({ status: 'error', retry });
  toast.error(`Couldn't save ${what}`, {
    description: `${errorMessage(error)} The change was undone.`,
    action: { label: 'Retry', onClick: retry },
  });
}

export function useSaveState(): SaveState {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => state,
    () => state,
  );
}

/** Run `job` after every earlier job on the same chain. */
function makeChain() {
  let tail: Promise<unknown> = Promise.resolve();
  return <T>(job: () => Promise<T>): Promise<T> => {
    const run = tail.then(job, job);
    tail = run.catch(() => undefined);
    return run;
  };
}


/* ================================================================== app settings (PUT /settings) */

/**
 * PUT /settings takes the whole object. The cache shows `confirmed` with every pending updater
 * applied; each request sends its own updater applied to the latest server state, so a failure
 * drops just that change and later changes still land.
 */
type Updater = (s: AppSettings) => AppSettings;
const settingsChain = makeChain();
let confirmed: AppSettings | null = null;
const pendingUpdaters: { id: number; fn: Updater }[] = [];
let updaterSeq = 0;

function rebuild(qc: QueryClient) {
  if (!confirmed) return;
  qc.setQueryData<AppSettings>(qk.settings, pendingUpdaters.reduce((s, u) => u.fn(s), confirmed));
}

export function saveSettings(qc: QueryClient, fn: Updater, what = 'that setting') {
  const current = qc.getQueryData<AppSettings>(qk.settings);
  if (!current) return;
  if (pendingUpdaters.length === 0) confirmed = current;
  const id = ++updaterSeq;
  pendingUpdaters.push({ id, fn });
  rebuild(qc);
  trackStart();
  void settingsChain(async () => {
    const base = confirmed ?? current;
    try {
      const saved = await api.put<AppSettings>('/settings', fn(base));
      confirmed = saved;
      pendingUpdaters.splice(pendingUpdaters.findIndex((u) => u.id === id), 1);
      rebuild(qc);
      trackDone();
      void qc.invalidateQueries({ queryKey: qk.home });
    } catch (e) {
      pendingUpdaters.splice(pendingUpdaters.findIndex((u) => u.id === id), 1);
      rebuild(qc);
      trackFail(what, e, () => saveSettings(qc, fn, what));
    }
  });
}

/** `save(s => ({...s, verify_crc: true}), 'checksum setting')` */
export function useSaveSettings() {
  const qc = useQueryClient();
  return useCallback((fn: Updater, what?: string) => saveSettings(qc, fn, what), [qc]);
}

/** Shorthand for a single top-level key. */
export function useSaveSetting() {
  const save = useSaveSettings();
  return useCallback(
    <K extends keyof AppSettings>(key: K, value: AppSettings[K], what?: string) =>
      save((s) => ({ ...s, [key]: value }), what),
    [save],
  );
}

/* ================================================================== providers (PUT /providers, partial) */

const providersChain = makeChain();
let providersPending = 0;

function applyProviders(p: ProvidersOut, body: ProvidersIn): ProvidersOut {
  return {
    ...p,
    priority: body.priority ? { ...p.priority, ...body.priority } : p.priority,
    disabled: body.disabled ?? p.disabled,
    language: body.language ?? p.language,
    statuses: body.disabled
      ? p.statuses.map((s) => ({ ...s, enabled: !body.disabled!.includes(s.name) }))
      : p.statuses,
  };
}

export function useSaveProviders() {
  const qc = useQueryClient();
  const save = useCallback(
    (body: ProvidersIn, what = 'provider settings'): Promise<ProvidersOut | null> => {
      const prev = qc.getQueryData<ProvidersOut>(qk.providers);
      if (prev) qc.setQueryData<ProvidersOut>(qk.providers, applyProviders(prev, body));
      providersPending++;
      trackStart();
      return providersChain(async () => {
        try {
          const saved = await api.put<ProvidersOut>('/providers', body);
          providersPending--;
          if (providersPending === 0) qc.setQueryData(qk.providers, saved);
          trackDone();
          void qc.invalidateQueries({ queryKey: qk.home });
          return saved;
        } catch (e) {
          providersPending--;
          void qc.invalidateQueries({ queryKey: qk.providers });
          trackFail(what, e, () => void save(body, what));
          return null;
        }
      });
    },
    [qc],
  );
  return save;
}

/* ================================================================== watch folders */

export function toWatchIn(w: WatchFolderOut): WatchFolderIn {
  return {
    path: w.path,
    enabled: w.enabled,
    recursive: w.recursive,
    kind: w.kind,
    auto_execute: w.auto_execute,
    settle_seconds: w.settle_seconds,
    format_id: w.format_id,
    dest_root: w.dest_root,
    mode: w.mode,
  };
}

const watchChain = makeChain();

export function useWatchFolderActions() {
  const qc = useQueryClient();

  const patchCache = useCallback(
    (fn: (list: WatchFolderOut[]) => WatchFolderOut[]) =>
      qc.setQueryData<WatchFolderOut[]>(qk.watchFolders, (list) => (list ? fn(list) : list)),
    [qc],
  );

  const settle = useCallback(() => {
    void qc.invalidateQueries({ queryKey: qk.watchFolders });
    void qc.invalidateQueries({ queryKey: qk.home });
  }, [qc]);

  const update = useCallback(
    (id: number, patch: Partial<WatchFolderIn>, what = 'the watch folder') => {
      patchCache((list) => list.map((w) => (w.id === id ? { ...w, ...patch } : w)));
      trackStart();
      void watchChain(async () => {
        // Build the body from the cache at send time so earlier queued edits are included.
        const current = qc.getQueryData<WatchFolderOut[]>(qk.watchFolders)?.find((w) => w.id === id);
        if (!current) {
          trackDone();
          return;
        }
        try {
          const saved = await api.put<WatchFolderOut>(`/watch-folders/${id}`, { ...toWatchIn(current), ...patch });
          patchCache((list) => list.map((w) => (w.id === id ? { ...saved, ...pickPatch(w, patch) } : w)));
          trackDone();
          settle();
        } catch (e) {
          settle();
          trackFail(what, e, () => update(id, patch, what));
        }
      });
    },
    [qc, patchCache, settle],
  );

  const create = useCallback(
    async (body: WatchFolderIn): Promise<WatchFolderOut | null> => {
      trackStart();
      return watchChain(async () => {
        try {
          const saved = await api.post<WatchFolderOut>('/watch-folders', body);
          patchCache((list) => [...list, saved]);
          trackDone();
          settle();
          return saved;
        } catch (e) {
          trackFail('the new watch folder', e, () => void create(body));
          return null;
        }
      });
    },
    [patchCache, settle],
  );

  const remove = useCallback(
    (id: number) => {
      patchCache((list) => list.filter((w) => w.id !== id));
      trackStart();
      void watchChain(async () => {
        try {
          await api.delete(`/watch-folders/${id}`);
          trackDone();
          settle();
        } catch (e) {
          settle();
          trackFail('the watch folder removal', e, () => remove(id));
        }
      });
    },
    [patchCache, settle],
  );

  return { update, create, remove };
}

/** Keep the fields of `patch` from the cache entry (a newer queued edit may have changed them). */
function pickPatch(w: WatchFolderOut, patch: Partial<WatchFolderIn>): Partial<WatchFolderOut> {
  const out: Partial<WatchFolderOut> = {};
  for (const k of Object.keys(patch) as (keyof WatchFolderIn)[]) {
    (out as Record<string, unknown>)[k] = w[k];
  }
  return out;
}

/* ================================================================== default format per kind */

const formatsChain = makeChain();

/**
 * Point a kind's default format at `expression`. Updates the kind's current (user) default in
 * place when it holds a preset; when it holds a custom formula, saves a new default instead so
 * the custom one is never overwritten.
 */
export function useSetDefaultFormat() {
  const qc = useQueryClient();
  const setDefault = useCallback(
    (kind: EditKind, expression: string, presetLabel: string, overwriteCurrent: boolean) => {
      const list = qc.getQueryData<FormatOut[]>(qk.formats.list) ?? [];
      const current = list.find((f) => f.kind === kind && f.is_default && !f.builtin);
      const inPlace = !!current && overwriteCurrent;
      const name = inPlace && current ? current.name : `${KIND_PLURAL[kind]} · ${presetLabel}`;
      qc.setQueryData<FormatOut[]>(qk.formats.list, (fs) => {
        if (!fs) return fs;
        if (inPlace) return fs.map((f) => (f.id === current?.id ? { ...f, expression } : f));
        const now = new Date().toISOString();
        const temp: FormatOut = {
          id: -Date.now(),
          name,
          kind,
          expression,
          is_default: true,
          builtin: false,
          created_at: now,
          updated_at: now,
        };
        return [...fs.map((f) => (f.kind === kind && f.is_default ? { ...f, is_default: false } : f)), temp];
      });
      trackStart();
      void formatsChain(async () => {
        try {
          const body: FormatIn = { name, kind, expression, is_default: true };
          if (inPlace && current) await api.put<FormatOut>(`/formats/${current.id}`, body);
          else await api.post<FormatOut>('/formats', body);
          trackDone();
        } catch (e) {
          trackFail(`the ${KIND_PLURAL[kind].toLowerCase()} naming`, e, () =>
            setDefault(kind, expression, presetLabel, overwriteCurrent),
          );
        } finally {
          void qc.invalidateQueries({ queryKey: qk.formats.list });
        }
      });
    },
    [qc],
  );
  return setDefault;
}

/* ================================================================== backup & restore */

export interface RestoreResult {
  app: boolean;
  providers: boolean;
  watch_folders: number;
  formats: number;
}

export const fetchBackup = (includeSecrets: boolean) =>
  api.get<unknown>('/settings/backup', { include_secrets: includeSecrets ? 'true' : 'false' });

export const restoreBackup = (data: unknown) => api.post<RestoreResult>('/settings/restore', data);

/** After a restore everything settings-shaped may have changed. */
export function invalidateAllSettings(qc: QueryClient) {
  for (const key of [qk.settings, qk.providers, qk.watchFolders, qk.formats.all, qk.apiKey, qk.home]) {
    void qc.invalidateQueries({ queryKey: key });
  }
}

/** Save `data` as a pretty-printed JSON file in the browser. */
export function downloadJson(data: unknown, filename: string) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
