import { describe, expect, it } from 'vitest';
import { buildItems } from '@/mocks/data';
import type { AppSettings, JobItemOut, JobItemPage } from '@/api/types';
import { displayPath, displayPathUnder, displayRoots, findLoadedItem, itemAction, itemActionTitle, visibleRows } from './model';

const base = buildItems()[0];
const item = (patch: Partial<JobItemOut>): JobItemOut => ({ ...base, ...patch });
const rename = { source: '/media/movies/Alien.1979', destination: '/media/movies/Alien (1979)' };

describe('itemAction', () => {
  it('shows a planned or done folder rename', () => {
    const planned = item({ status: 'matched', proposed_path: '/media/movies/Alien (1979)/Alien (1979).mkv', folder_rename: rename });
    expect(itemAction(planned, 'move')).toBe('folder_renamed');
    expect(itemAction({ ...planned, status: 'done', final_path: planned.proposed_path }, 'move')).toBe('folder_renamed');
    expect(itemActionTitle(planned, 'folder_renamed')).toBe('Folder Alien.1979 → Alien (1979), renamed in place');
  });

  it('shows a plain move in move jobs only', () => {
    const moved = item({ status: 'matched', proposed_path: '/media/movies/X (2000)/X (2000).mkv', folder_rename: null });
    expect(itemAction(moved, 'move')).toBe('moved');
    expect(itemAction(moved, 'copy')).toBeNull();
    expect(itemAction(moved, 'hardlink')).toBeNull();
  });

  it('shows nothing when nothing moves', () => {
    const planned = { proposed_path: '/media/movies/X (2000)/X (2000).mkv', folder_rename: null };
    expect(itemAction(item({ ...planned, status: 'skipped' }), 'move')).toBeNull();
    expect(itemAction(item({ ...planned, status: 'error' }), 'move')).toBeNull();
    expect(itemAction(item({ ...planned, status: 'done', warnings: ['already_in_place'] }), 'move')).toBeNull();
    expect(itemAction(item({ status: 'review', proposed_path: null, final_path: null, folder_rename: null }), 'move')).toBeNull();
  });
});

describe('visible rows of a paged list', () => {
  const page = (items: JobItemOut[]): JobItemPage => ({
    job_id: base.job_id,
    items,
    total: 99,
    offset: 0,
    limit: 250,
    next_after: null,
    counts: {},
  });
  const a = item({ id: 1, status: 'review' });
  const b = item({ id: 2, status: 'matched' });
  const c = item({ id: 3, status: 'approved' });
  const d = item({ id: 4, status: 'done' });

  it('filters by tab and keeps the order of the pages', () => {
    const pages = [page([a, b]), page([c, d])];
    expect(visibleRows(pages, 'all').map((i) => i.id)).toEqual([1, 2, 3, 4]);
    expect(visibleRows(pages, 'matched').map((i) => i.id)).toEqual([2, 3]);
    expect(visibleRows(undefined, 'all')).toEqual([]);
  });

  it('re-filters only the page that changed', () => {
    const p1 = page([a, b]);
    const p2 = page([c, d]);
    const first = visibleRows([p1, p2], 'matched');
    expect(first.map((i) => i.id)).toEqual([2, 3]);
    // A push replaces page 2 only: c moved to done. Page 1 comes from the cache (same row objects).
    const p2b = page([{ ...c, status: 'done' }, d]);
    const next = visibleRows([p1, p2b], 'matched');
    expect(next.map((i) => i.id)).toEqual([2]);
    expect(next[0]).toBe(first[0]);
  });

  it('finds loaded items by id, also outside the tab', () => {
    const pages = [page([a, b]), page([c, d])];
    expect(findLoadedItem(pages, 4)).toBe(d);
    expect(findLoadedItem(pages, 9)).toBeNull();
    expect(findLoadedItem(pages, null)).toBeNull();
  });
});

describe('display paths', () => {
  const settings = { library_roots: { movie: '/media/movies', series: '/media/tv' } } as unknown as AppSettings;

  it('works out the roots once and gives the same answer as displayPath', () => {
    const roots = displayRoots('/dest', settings);
    expect(roots).toEqual(['/dest', '/media/movies', '/media/tv']);
    expect(displayRoots(null, undefined)).toEqual([]);
    const moved = item({ final_path: '/media/tv/Show/Season 01/Show - S01E01.mkv' });
    const overridden = item({ final_path: null, proposed_path: '/media/movies/X (2000)/X.mkv', path_overridden: true });
    for (const it of [moved, overridden]) {
      expect(displayPathUnder(it, roots)).toBe(displayPath(it, { dest_root: '/dest' }, settings));
    }
    expect(displayPathUnder(moved, roots)).toBe('Show/Season 01/Show - S01E01.mkv');
  });
});
