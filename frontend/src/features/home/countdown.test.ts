import { describe, expect, it } from 'vitest';
import { clockSkew, formatCountdown, hasSettleWait, settleProgress } from './countdown';
import type { IncomingFile } from '@/api/types';

const T0 = Date.parse('2026-10-30T19:00:00Z');
const iso = (ms: number) => new Date(ms).toISOString();

const settling = (over: Partial<IncomingFile> = {}): IncomingFile => ({
  folder_id: 1,
  path: '/media/downloads/a.mkv',
  name: 'a.mkv',
  size: 1,
  state: 'settling',
  settle_seconds: 120,
  stable_since: iso(T0 - 72_000),
  settles_at: iso(T0 + 48_000),
  title_guess: null,
  ...over,
});

describe('clockSkew', () => {
  it('is server time minus local receive time', () => {
    expect(clockSkew(iso(T0 + 5_000), T0)).toBe(5_000); // server clock ahead
    expect(clockSkew(iso(T0 - 2_000), T0)).toBe(-2_000); // server clock behind
  });

  it('falls back to no correction for missing or bad input', () => {
    expect(clockSkew(null, T0)).toBe(0);
    expect(clockSkew('not a date', T0)).toBe(0);
    expect(clockSkew(iso(T0), 0)).toBe(0);
  });
});

describe('settleProgress', () => {
  it('counts down to settles_at with the elapsed share of the wait', () => {
    const p = settleProgress(settling(), T0);
    expect(p).toEqual({ remainingMs: 48_000, fraction: 0.6, due: false });
    expect(formatCountdown(p!.remainingMs)).toBe('0:48');
  });

  it('applies skew: a local clock 10 s behind still shows the server countdown', () => {
    const localNow = T0 - 10_000;
    const skew = clockSkew(iso(T0), localNow);
    expect(settleProgress(settling(), localNow + skew)?.remainingMs).toBe(48_000);
  });

  it('clamps once due and before the wait started', () => {
    expect(settleProgress(settling(), T0 + 60_000)).toEqual({ remainingMs: 0, fraction: 1, due: true });
    expect(settleProgress(settling(), T0 - 100_000)?.fraction).toBe(0);
  });

  it('derives the start from settle_seconds when stable_since is missing', () => {
    expect(settleProgress(settling({ stable_since: null }), T0)?.fraction).toBeCloseTo(0.6);
  });

  it('is null for partial (still downloading) files', () => {
    expect(settleProgress(settling({ state: 'partial', settles_at: null, stable_since: null }), T0)).toBeNull();
  });
});

describe('formatCountdown', () => {
  it('rounds seconds up and formats minutes and hours', () => {
    expect(formatCountdown(48_000)).toBe('0:48');
    expect(formatCountdown(47_100)).toBe('0:48');
    expect(formatCountdown(125_000)).toBe('2:05');
    expect(formatCountdown(3_723_000)).toBe('1:02:03');
    expect(formatCountdown(0)).toBe('0:00');
    expect(formatCountdown(-5)).toBe('0:00');
  });
});

describe('hasSettleWait', () => {
  it('is true exactly when settleProgress has a countdown, whatever the time', () => {
    const cases = [
      settling(),
      settling({ state: 'partial' }),
      settling({ settles_at: null }),
      settling({ settles_at: 'not a date' }),
    ];
    for (const f of cases) {
      expect(hasSettleWait(f)).toBe(settleProgress(f, T0) !== null);
      expect(hasSettleWait(f)).toBe(settleProgress(f, T0 + 10 * 60_000) !== null);
    }
    expect(hasSettleWait(settling())).toBe(true);
  });
});
