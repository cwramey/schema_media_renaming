import { describe, expect, it } from 'vitest';
import { dayLabel, groupByDay, localDayKey, relativeDay } from './days';

// Local-time fixtures so the test holds in any timezone.
const local = (y: number, mo: number, d: number, h = 12, mi = 0) => new Date(y, mo - 1, d, h, mi).toISOString();
const NOW = new Date(2026, 9, 30, 15, 0); // Oct 30 2026, 3 PM local

describe('dayLabel', () => {
  it('says Today, Yesterday, then a short date', () => {
    expect(dayLabel(local(2026, 10, 30, 0, 5), NOW, 'en-US')).toBe('Today');
    expect(dayLabel(local(2026, 10, 29, 23, 59), NOW, 'en-US')).toBe('Yesterday');
    expect(dayLabel(local(2026, 10, 28, 19, 4), NOW, 'en-US')).toBe('Oct 28');
  });

  it('adds the year for other years and handles month/year boundaries', () => {
    expect(dayLabel(local(2025, 12, 31), NOW, 'en-US')).toBe('Dec 31, 2025');
    const jan1 = new Date(2027, 0, 1, 9, 0);
    expect(dayLabel(local(2026, 12, 31, 22), jan1, 'en-US')).toBe('Yesterday');
  });

  it('copes with bad input', () => {
    expect(dayLabel('nope', NOW)).toBe('Unknown date');
  });
});

describe('groupByDay', () => {
  it('groups newest-first entries by local day, keeping order', () => {
    const entries = [
      { id: 1, ts: local(2026, 10, 30, 15, 8) },
      { id: 2, ts: local(2026, 10, 29, 18, 14) },
      { id: 3, ts: local(2026, 10, 29, 18, 8) },
      { id: 4, ts: local(2026, 10, 28, 19, 4) },
    ];
    const groups = groupByDay(entries, NOW, 'en-US');
    expect(groups.map((g) => [g.label, g.entries.map((e) => e.id)])).toEqual([
      ['Today', [1]],
      ['Yesterday', [2, 3]],
      ['Oct 28', [4]],
    ]);
    expect(groups[0].key).toBe(localDayKey(NOW));
  });

  it('returns nothing for no entries', () => {
    expect(groupByDay([], NOW)).toEqual([]);
  });
});

describe('relativeDay', () => {
  it('uses calendar days like the canvas ("Movie · yesterday")', () => {
    expect(relativeDay(local(2026, 10, 30, 14, 58), NOW)).toBe('2 minutes ago');
    expect(relativeDay(local(2026, 10, 30, 12, 0), NOW)).toBe('3 hours ago');
    expect(relativeDay(local(2026, 10, 29, 18, 14), NOW)).toBe('yesterday');
    expect(relativeDay(local(2026, 10, 28, 19, 4), NOW)).toBe('2 days ago');
    expect(relativeDay(local(2026, 10, 2, 9, 0), NOW, 'en-US')).toBe('Oct 2');
    expect(relativeDay(local(2026, 10, 30, 15, 0), NOW)).toBe('just now');
  });
});
