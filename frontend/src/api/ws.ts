/**
 * WebSocket client for `/api/ws` with exponential-backoff reconnect, plus a tiny
 * connection-state store consumed with useSyncExternalStore.
 */
import { useEffect, useSyncExternalStore } from 'react';
import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { applyWsMessages, parseWsFrame, scheduleResync } from './wsReducer';
import type { WsMessage } from './types';

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'offline';

export interface ConnectionSnapshot {
  state: ConnectionState;
  attempt: number;
  /** epoch ms of the next reconnect attempt (when reconnecting) */
  nextRetryAt: number | null;
  lastMessageAt: number | null;
  serverVersion: string | null;
}

/* ------------------------------------------------------------------ store */

let snapshot: ConnectionSnapshot = {
  state: 'idle',
  attempt: 0,
  nextRetryAt: null,
  lastMessageAt: null,
  serverVersion: null,
};
const listeners = new Set<() => void>();

function setSnapshot(patch: Partial<ConnectionSnapshot>) {
  snapshot = { ...snapshot, ...patch };
  listeners.forEach((l) => l());
}

export const connectionStore = {
  subscribe(listener: () => void) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
  getSnapshot: () => snapshot,
  /** test helper */
  reset() {
    setSnapshot({ state: 'idle', attempt: 0, nextRetryAt: null, lastMessageAt: null, serverVersion: null });
  },
};

export function useConnection(): ConnectionSnapshot {
  return useSyncExternalStore(connectionStore.subscribe, connectionStore.getSnapshot, connectionStore.getSnapshot);
}

const isLive = () => snapshot.state === 'open';

/** True while the socket is open. Re-renders only when that flips (not on every message). */
export function useLiveConnected(): boolean {
  return useSyncExternalStore(connectionStore.subscribe, isLive, isLive);
}

/* ------------------------------------------------------------------ backoff */

/** 0.5 s, 1 s, 2 s, 4 s … capped at 15 s, with ±20 % jitter. */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(15_000, 500 * 2 ** Math.max(0, attempt - 1));
  const jitter = base * 0.2 * (random() * 2 - 1);
  return Math.round(base + jitter);
}

export function wsUrl(loc: Pick<Location, 'protocol' | 'host'> = window.location): string {
  const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${loc.host}/api/ws`;
}

/* ------------------------------------------------------------------ batching */

type Cancel = () => void;

/** Next animation frame, or `fallbackMs` if frames are not coming (hidden tab, tests). */
function nextFrame(fn: () => void, fallbackMs: number): Cancel {
  // Whichever fires first runs `fn` and cancels the other.
  const run = () => {
    cancel();
    fn();
  };
  const timer = setTimeout(run, fallbackMs);
  const raf = typeof requestAnimationFrame === 'function' ? requestAnimationFrame(run) : null;
  function cancel() {
    clearTimeout(timer);
    if (raf !== null) cancelAnimationFrame(raf);
  }
  return cancel;
}

/**
 * Collects socket messages and hands them over once per animation frame (or every `fallbackMs`
 * while frames are paused), so a burst of pushes is applied, and rendered, once.
 */
export class MessageBatcher {
  private queue: WsMessage[] = [];
  private cancel: Cancel | null = null;

  constructor(
    private readonly onFlush: (msgs: WsMessage[]) => void,
    private readonly fallbackMs = 250,
  ) {}

  push(msg: WsMessage) {
    this.queue.push(msg);
    if (!this.cancel) this.cancel = nextFrame(() => this.flush(), this.fallbackMs);
  }

  flush() {
    this.cancel?.();
    this.cancel = null;
    const msgs = this.queue;
    if (msgs.length === 0) return;
    this.queue = [];
    this.onFlush(msgs);
  }

  /** Drop anything queued (the client is stopping). */
  dispose() {
    this.cancel?.();
    this.cancel = null;
    this.queue = [];
  }
}

/* ------------------------------------------------------------------ client */

export interface LiveClientOptions {
  url?: string;
  /** Messages received since the last call, in order (at most once per animation frame). */
  onMessages: (msgs: WsMessage[]) => void;
  /** Called after a successful reconnect so the cache can resync missed updates. */
  onReconnect?: () => void;
}

export class LiveClient {
  private socket: WebSocket | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  private stopped = true;
  private everOpened = false;
  private readonly opts: LiveClientOptions;
  private readonly batcher: MessageBatcher;

  constructor(opts: LiveClientOptions) {
    this.opts = opts;
    this.batcher = new MessageBatcher((msgs) => this.deliver(msgs));
  }

  private deliver(msgs: WsMessage[]) {
    let version: string | null | undefined;
    for (const m of msgs) if (m.type === 'hello') version = m.data.version ?? null;
    setSnapshot({ lastMessageAt: Date.now(), ...(version !== undefined ? { serverVersion: version } : {}) });
    this.opts.onMessages(msgs);
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    window.addEventListener('online', this.handleOnline);
    document.addEventListener('visibilitychange', this.handleVisibility);
    this.open();
  }

  stop() {
    this.stopped = true;
    window.removeEventListener('online', this.handleOnline);
    document.removeEventListener('visibilitychange', this.handleVisibility);
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.batcher.dispose();
    const s = this.socket;
    this.socket = null;
    if (s) {
      s.onopen = s.onclose = s.onerror = s.onmessage = null;
      s.close();
    }
    setSnapshot({ state: 'idle', attempt: 0, nextRetryAt: null });
  }

  private handleOnline = () => this.retryNow();
  private handleVisibility = () => {
    if (document.visibilityState === 'visible') this.retryNow();
  };

  private retryNow() {
    if (this.stopped || this.socket) return;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.open();
  }

  private open() {
    if (this.stopped) return;
    setSnapshot({ state: this.attempt === 0 ? 'connecting' : 'reconnecting', attempt: this.attempt, nextRetryAt: null });
    let s: WebSocket;
    try {
      s = new WebSocket(this.opts.url ?? wsUrl());
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = s;

    s.onopen = () => {
      const wasReconnect = this.everOpened;
      this.everOpened = true;
      this.attempt = 0;
      setSnapshot({ state: 'open', attempt: 0, nextRetryAt: null });
      if (wasReconnect) this.opts.onReconnect?.();
    };
    s.onmessage = (ev: MessageEvent) => {
      const msg = parseWsFrame(ev.data);
      if (msg) this.batcher.push(msg);
    };
    s.onerror = () => {
      /* onclose follows and handles the retry */
    };
    s.onclose = () => {
      if (this.socket === s) this.socket = null;
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect() {
    if (this.stopped) return;
    this.attempt += 1;
    const delay = backoffDelay(this.attempt);
    const offline = typeof navigator !== 'undefined' && navigator.onLine === false;
    setSnapshot({
      state: offline ? 'offline' : 'reconnecting',
      attempt: this.attempt,
      nextRetryAt: Date.now() + delay,
    });
    this.timer = setTimeout(() => {
      this.timer = null;
      this.open();
    }, delay);
  }
}

/** Mount once (inside the authenticated shell) to stream live updates into the query cache. */
export function useLiveUpdates(enabled: boolean) {
  const qc = useQueryClient();
  useEffect(() => {
    if (!enabled) return;
    if (import.meta.env.MODE === 'mock') {
      setSnapshot({ state: 'open', attempt: 0, nextRetryAt: null, serverVersion: 'mock' });
      return () => setSnapshot({ state: 'idle' });
    }
    const client = new LiveClient({
      onMessages: (msgs) => applyWsMessages(qc, msgs),
      onReconnect: () => resyncAfterReconnect(qc),
    });
    client.start();
    return () => client.stop();
  }, [enabled, qc]);
}

/** Anything may have changed while disconnected: refetch active queries (debounced, see `scheduleResync`). */
export function resyncAfterReconnect(qc: QueryClient) {
  scheduleResync(qc);
}
