/** Settle-wait countdown math for Home's Incoming column (pure, unit tested). */
import type { IncomingFile } from '@/api/types';

/**
 * Milliseconds to add to the local clock to get the server's clock.
 * `receivedAt` is when the response carrying `serverTime` arrived (local epoch ms).
 */
export function clockSkew(serverTime: string | null | undefined, receivedAt: number): number {
  if (!serverTime) return 0;
  const t = Date.parse(serverTime);
  return Number.isFinite(t) && Number.isFinite(receivedAt) && receivedAt > 0 ? t - receivedAt : 0;
}

export interface SettleProgress {
  /** ms left until the file is processed (0 once due). */
  remainingMs: number;
  /** 0..1 share of the settle wait already elapsed. */
  fraction: number;
  due: boolean;
}

/** True when `settleProgress` has a countdown for this file (whatever the time). */
export function hasSettleWait(file: Pick<IncomingFile, 'state' | 'settles_at'>): boolean {
  return file.state === 'settling' && !!file.settles_at && Number.isFinite(Date.parse(file.settles_at));
}

/** Where a settling file is in its wait, at server time `serverNow`. Null for partial files. */
export function settleProgress(
  file: Pick<IncomingFile, 'state' | 'settle_seconds' | 'stable_since' | 'settles_at'>,
  serverNow: number,
): SettleProgress | null {
  if (!hasSettleWait(file)) return null;
  const end = Date.parse(file.settles_at as string);
  const totalMs = Math.max(0, file.settle_seconds * 1000);
  const parsedStart = file.stable_since ? Date.parse(file.stable_since) : NaN;
  const start = Number.isFinite(parsedStart) ? parsedStart : end - totalMs;
  const span = end - start;
  const remainingMs = Math.max(0, end - serverNow);
  const fraction = span > 0 ? Math.min(1, Math.max(0, (serverNow - start) / span)) : 1;
  return { remainingMs, fraction, due: remainingMs === 0 };
}

/** "0:48", "2:05", "1:02:03"; seconds round up so the display never shows 0:00 early. */
export function formatCountdown(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = String(s).padStart(2, '0');
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}
