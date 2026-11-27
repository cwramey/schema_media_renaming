/** Ordering and filtering of a server folder listing (pure, unit tested). */
import type { FsEntry } from '@/api/types';

/** Created once: building a collator per comparison (what `localeCompare` does) is the slow part of sorting. */
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

export interface ListedEntry {
  entry: FsEntry;
  /** Lower-cased name, for the filter box. */
  lower: string;
}

/** Folders first, then natural name order ("Episode 2" before "Episode 10"). Done once per listing. */
export function sortEntries(entries: readonly FsEntry[], dirsOnly = false): ListedEntry[] {
  const visible = dirsOnly ? entries.filter((e) => e.is_dir) : entries.slice();
  visible.sort((a, b) => Number(b.is_dir) - Number(a.is_dir) || collator.compare(a.name, b.name));
  return visible.map((entry) => ({ entry, lower: entry.name.toLowerCase() }));
}

/** Entries whose name contains `text` (case-insensitive); the sorted list itself when `text` is blank. */
export function filterEntries(sorted: readonly ListedEntry[], text: string): FsEntry[] {
  const needle = text.trim().toLowerCase();
  const out: FsEntry[] = [];
  for (const it of sorted) if (!needle || it.lower.includes(needle)) out.push(it.entry);
  return out;
}
