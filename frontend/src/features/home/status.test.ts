import { describe, expect, it } from 'vitest';
import type { HomeOut, LibraryCard, ProviderStatus } from '@/api/types';
import {
  episodeCode,
  episodeRange,
  libraryCaption,
  libraryRows,
  needsYouHeading,
  parentFolder,
  primaryAction,
  statusChips,
  totalsLabel,
} from './status';

const provider = (name: string, over: Partial<ProviderStatus> = {}): ProviderStatus => ({
  name,
  label: name,
  kinds: ['movie'],
  requires_key: true,
  configured: true,
  enabled: true,
  healthy: true,
  last_error: null,
  last_checked: null,
  rate_limit: null,
  requests_made: 0,
  cache_hits: 0,
  ...over,
});

const status = (over: Partial<HomeOut['status']> = {}): HomeOut['status'] => ({
  watch: [{ id: 1, path: '/media/downloads', status: 'watching', enabled: true }],
  providers: [provider('tmdb')],
  media_servers: [{ id: 'j', kind: 'jellyfin', name: 'Jellyfin', url: 'http://jf', enabled: true }],
  ...over,
});

describe('statusChips', () => {
  it('matches the canvas strip', () => {
    expect(statusChips(status()).map((c) => [c.tone, c.label])).toEqual([
      ['success', 'Watching /media/downloads'],
      ['success', 'TMDb connected'],
      ['success', 'Jellyfin connected'],
    ]);
  });

  it('keeps optional providers quiet unless they are set up and failing', () => {
    const chips = statusChips(
      status({
        providers: [
          provider('tmdb'),
          provider('tvdb', { configured: false, healthy: null }),
          provider('anidb', { healthy: false }),
          provider('trakt', { enabled: false, healthy: false }),
        ],
      }),
    );
    expect(chips.map((c) => c.label)).toEqual([
      'Watching /media/downloads',
      'TMDb connected',
      "AniDB can't connect",
      'Jellyfin connected',
    ]);
  });

  it('explains watch folder problems', () => {
    const chips = statusChips(
      status({
        watch: [
          { id: 1, path: '/a', status: 'missing', enabled: true },
          { id: 2, path: '/b', status: 'error', enabled: true },
          { id: 3, path: '/c', status: 'watching', enabled: false },
        ],
        providers: [],
        media_servers: [],
      }),
    );
    expect(chips.map((c) => [c.tone, c.label])).toEqual([
      ['warning', '/a is missing'],
      ['danger', "Can't watch /b"],
      ['neutral', 'Paused /c'],
    ]);
    expect(statusChips(status({ watch: [], providers: [], media_servers: [] }))[0].label).toBe('No watch folder');
  });

  it('reports TMDb problems plainly', () => {
    const label = (p: ProviderStatus) => statusChips(status({ watch: [], media_servers: [], providers: [p] }))[1].label;
    expect(label(provider('tmdb', { configured: false }))).toBe('TMDb not set up');
    expect(label(provider('tmdb', { healthy: false }))).toBe("TMDb can't connect");
  });
});

describe('labels', () => {
  it('heading', () => {
    expect(needsYouHeading(0)).toBe('All caught up');
    expect(needsYouHeading(1)).toBe('1 file needs review');
    expect(needsYouHeading(3)).toBe('3 files need review');
  });

  it('library caption names the media servers that get refreshed', () => {
    expect(libraryCaption(status().media_servers)).toBe('Done · Jellyfin was told');
    expect(libraryCaption([])).toBe('Done');
  });

  it('totals', () => {
    expect(totalsLabel(156, 2.7 * 1024 ** 4)).toBe('156 files · 2.7 TiB organized');
    expect(totalsLabel(1, 1024)).toBe('1 file · 1.0 KiB organized');
  });

  it('event cards always offer Dismiss', () => {
    expect(primaryAction({ kind: 'event', primary: null })).toEqual({ label: 'Dismiss', action: 'dismiss' });
    expect(primaryAction({ kind: 'item', primary: null })).toBeNull();
    expect(primaryAction({ kind: 'item', primary: { label: 'Skip as extra', action: 'skip' } })?.label).toBe('Skip as extra');
  });
});

const lib = (id: number, over: Partial<LibraryCard> = {}): LibraryCard => ({
  operation_id: id,
  title: `Film ${id}`,
  kind: 'movie',
  destination: `/media/movies/Film ${id}.mkv`,
  ts: '2026-11-20T10:00:00Z',
  bytes: 1,
  can_undo: true,
  ...over,
});
const ep = (id: number, show: string, season: number, episode: number) =>
  lib(id, { kind: 'series', show, season, episode, title: `${show} · ${episodeCode(season, episode)}` });

describe('libraryRows', () => {
  it('folds a run of episodes of one show and season into one row', () => {
    const rows = libraryRows([lib(1), ep(2, 'Show (2013)', 4, 12), ep(3, 'Show (2013)', 4, 11), ep(4, 'Show (2013)', 4, 10), lib(5)]);
    expect(rows.map((r) => (r.type === 'group' ? r.cards.map((c) => c.operation_id) : r.card.operation_id))).toEqual([
      1,
      [2, 3, 4],
      5,
    ]);
  });

  it('keeps a lone episode, other seasons and interrupted runs apart', () => {
    const rows = libraryRows([ep(1, 'A', 1, 1), ep(2, 'A', 2, 1), lib(3), ep(4, 'A', 2, 2)]);
    expect(rows.map((r) => r.type)).toEqual(['single', 'single', 'single', 'single']);
  });

  it('stops at the row limit but still finishes the last group', () => {
    const rows = libraryRows([lib(1), ep(2, 'A', 1, 2), ep(3, 'A', 1, 1), lib(4)], 2);
    expect(rows).toHaveLength(2);
    expect(rows[1].type === 'group' && rows[1].cards).toHaveLength(2);
  });
});

describe('episode labels', () => {
  it('names runs and gaps', () => {
    expect(episodeCode(4, 12)).toBe('S04E12');
    expect(episodeRange(4, [ep(1, 'A', 4, 12), ep(2, 'A', 4, 10), ep(3, 'A', 4, 11)])).toBe('S04E10–E12');
    expect(episodeRange(4, [ep(1, 'A', 4, 12), ep(2, 'A', 4, 9)])).toBe('Season 4');
  });

  it('finds the folder a file went into', () => {
    expect(parentFolder('/media/tv/Show/Season 04/x.mkv')).toBe('/media/tv/Show/Season 04');
    expect(parentFolder('D:\\TV\\Show\\x.mkv')).toBe('D:\\TV\\Show');
  });
});
