/** Client-side day grouping for History (Today / Yesterday / "Oct 28"), in local time. */

export interface DayGroup<T> {
  /** Local calendar day, YYYY-MM-DD. */
  key: string;
  label: string;
  entries: T[];
}

const pad = (n: number) => String(n).padStart(2, '0');

export function localDayKey(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** "Today", "Yesterday", "Oct 28" (this year) or "Oct 28, 2025". */
export function dayLabel(iso: string, now: Date = new Date(), locale?: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return 'Unknown date';
  const key = localDayKey(d);
  if (key === localDayKey(now)) return 'Today';
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (key === localDayKey(yesterday)) return 'Yesterday';
  return d.toLocaleDateString(locale, {
    month: 'short',
    day: 'numeric',
    ...(d.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' }),
  });
}

/** Group newest-first entries by local day, keeping their order. */
export function groupByDay<T extends { ts: string }>(entries: readonly T[], now: Date = new Date(), locale?: string): DayGroup<T>[] {
  const groups: DayGroup<T>[] = [];
  const index = new Map<string, DayGroup<T>>();
  for (const e of entries) {
    const d = new Date(e.ts);
    const key = Number.isNaN(d.getTime()) ? 'unknown' : localDayKey(d);
    let g = index.get(key);
    if (!g) {
      g = { key, label: dayLabel(e.ts, now, locale), entries: [] };
      index.set(key, g);
      groups.push(g);
    }
    g.entries.push(e);
  }
  return groups;
}

/**
 * Short "when" for cards: "5 minutes ago" today, then "yesterday", "3 days ago" within a week,
 * then the date ("Oct 2"). Calendar days, so last night is "yesterday", not "14 hours ago".
 */
export function relativeDay(iso: string, now: Date = new Date(), locale?: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '–';
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(d)) / 86_400_000);
  if (days <= 0) {
    const mins = Math.max(0, Math.round((now.getTime() - d.getTime()) / 60_000));
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins} minute${mins === 1 ? '' : 's'} ago`;
    const hours = Math.round(mins / 60);
    return `${hours} hour${hours === 1 ? '' : 's'} ago`;
  }
  if (days === 1) return 'yesterday';
  if (days < 7) return `${days} days ago`;
  return dayLabel(iso, now, locale);
}
