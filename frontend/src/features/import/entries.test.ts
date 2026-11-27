import { describe, expect, it } from 'vitest';
import type { FsEntry } from '@/api/types';
import { filterEntries, sortEntries } from './entries';

const entry = (name: string, over: Partial<FsEntry> = {}): FsEntry => ({
  name,
  path: `/media/${name}`,
  is_dir: false,
  size: 1,
  modified: null,
  is_video: name.endsWith('.mkv'),
  ...over,
});

describe('folder listing order and filter', () => {
  const listing = [
    entry('Show - E10.mkv'),
    entry('notes.txt'),
    entry('Show - E2.mkv'),
    entry('extras', { is_dir: true }),
    entry('Alpha', { is_dir: true }),
    entry('show - e1.mkv'),
  ];

  it('lists folders first, then names in natural, case-insensitive order', () => {
    expect(sortEntries(listing).map((e) => e.entry.name)).toEqual([
      'Alpha',
      'extras',
      'notes.txt',
      'show - e1.mkv',
      'Show - E2.mkv',
      'Show - E10.mkv',
    ]);
    expect(listing[0].name).toBe('Show - E10.mkv'); // input untouched
  });

  it('can list folders only', () => {
    expect(sortEntries(listing, true).map((e) => e.entry.name)).toEqual(['Alpha', 'extras']);
  });

  it('filters case-insensitively and keeps the order', () => {
    const sorted = sortEntries(listing);
    expect(filterEntries(sorted, '  SHOW ').map((e) => e.name)).toEqual(['show - e1.mkv', 'Show - E2.mkv', 'Show - E10.mkv']);
    expect(filterEntries(sorted, '')).toHaveLength(listing.length);
    expect(filterEntries(sorted, 'zzz')).toEqual([]);
  });
});
