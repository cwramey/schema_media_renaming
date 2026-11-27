/** Display formatting helpers (bytes, times, episode numbers). */
import type { ParsedMedia } from '@/api/types';

export function formatBytes(bytes: number | null | undefined, digits = 1): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '–';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  let v = bytes / 1024;
  let u = 0;
  while (v >= 1024 && u < units.length - 1) {
    v /= 1024;
    u += 1;
  }
  return `${v.toFixed(v >= 100 ? 0 : digits)} ${units[u]}`;
}

export function formatNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return '–';
  return new Intl.NumberFormat().format(n);
}

export function formatPercent(v: number, digits = 0): string {
  return `${(v * 100).toFixed(digits)}%`;
}

const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });

const UNITS: [number, Intl.RelativeTimeFormatUnit, number][] = [
  [3600, 'minute', 60],
  [86400, 'hour', 3600],
  [604800, 'day', 86400],
  [2629800, 'week', 604800],
  [31557600, 'month', 2629800],
  [Infinity, 'year', 31557600],
];

export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return '–';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '–';
  const diff = (t - now) / 1000;
  const abs = Math.abs(diff);
  if (abs < 45) return 'just now';
  for (const [limit, unit, div] of UNITS) {
    if (abs < limit) return rtf.format(Math.round(diff / div), unit);
  }
  return new Date(iso).toLocaleString();
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '–';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '–';
  return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '–';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '–';
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

/** Wall-clock time without seconds, e.g. "3:08 PM" (locale-dependent). */
export function formatClock(iso: string | null | undefined, locale?: string): string {
  if (!iso) return '–';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '–';
  return d.toLocaleTimeString(locale, { hour: 'numeric', minute: '2-digit' });
}

export const pad2 = (n: number) => String(n).padStart(2, '0');

/** "S01E02-E03" style label, "#015" for absolute-only, "2024-03-04" for daily, null if nothing. */
export function episodeLabel(
  p: Pick<ParsedMedia, 'season' | 'episodes' | 'absolute' | 'air_date' | 'special'>,
): string | null {
  if (p.season !== null && p.episodes.length > 0) {
    const first = `S${pad2(p.season)}E${pad2(p.episodes[0])}`;
    if (p.episodes.length === 1) return first;
    return `${first}-E${pad2(p.episodes[p.episodes.length - 1])}`;
  }
  if (p.absolute.length > 0) {
    const a = p.absolute.map((x) => String(x).padStart(3, '0'));
    return a.length === 1 ? `#${a[0]}` : `#${a[0]}-${a[a.length - 1]}`;
  }
  if (p.air_date) return p.air_date;
  if (p.special) return 'Special';
  if (p.season !== null) return `S${pad2(p.season)}`;
  return null;
}

export function parseIntList(s: string): number[] {
  return s
    .split(/[\s,;-]+/)
    .map((x) => Number.parseInt(x, 10))
    .filter((x) => Number.isFinite(x));
}
