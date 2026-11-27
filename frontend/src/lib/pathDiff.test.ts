import { describe, expect, it } from 'vitest';
import { basename, diffPath, dirname, lcsKeep, relativeTo, similarity, splitPath, wordTokens } from './pathDiff';

const render = (segs: { text: string; kind: string }[]) =>
  segs.map((s) => (s.kind === 'changed' ? `[${s.text}]` : s.text)).join('');

describe('splitPath', () => {
  it('splits POSIX paths', () => {
    expect(splitPath('/storage/TV/Show (2016)/Season 01/Show - S01E01.mkv')).toEqual({
      root: '/',
      folders: ['storage', 'TV', 'Show (2016)', 'Season 01'],
      sep: '/',
      stem: 'Show - S01E01',
      ext: 'mkv',
    });
  });

  it('splits Windows and UNC paths', () => {
    expect(splitPath('C:\\Media\\Movies\\Film (2019).mkv')).toMatchObject({
      root: 'C:\\',
      folders: ['Media', 'Movies'],
      sep: '\\',
      stem: 'Film (2019)',
      ext: 'mkv',
    });
    expect(splitPath('\\\\nas\\media\\a.avi')).toMatchObject({ root: '\\\\', folders: ['nas', 'media'], stem: 'a', ext: 'avi' });
  });

  it('handles relative paths, dotted names and missing extensions', () => {
    expect(splitPath('Show/Season 01/Show.S01E01.WEB.mkv')).toMatchObject({ root: '', folders: ['Show', 'Season 01'], stem: 'Show.S01E01.WEB', ext: 'mkv' });
    expect(splitPath('Folder.With.Dots')).toMatchObject({ stem: 'Folder.With', ext: 'Dots' });
    expect(splitPath('Mr. Robot')).toMatchObject({ stem: 'Mr. Robot', ext: '' });
    expect(splitPath('.hidden')).toMatchObject({ stem: '.hidden', ext: '' });
    expect(splitPath('trailing/')).toMatchObject({ folders: [], stem: 'trailing', ext: '' });
  });

  it('offers basename/dirname helpers', () => {
    expect(basename('/a/b/c.mkv')).toBe('c.mkv');
    expect(dirname('/a/b/c.mkv')).toBe('/a/b');
  });
});

describe('relativeTo', () => {
  it('strips a root prefix regardless of separators, trailing slashes and case', () => {
    expect(relativeTo('/storage/TV/Show/x.mkv', '/storage/TV')).toBe('Show/x.mkv');
    expect(relativeTo('/storage/TV/Show/x.mkv', '/storage/TV/')).toBe('Show/x.mkv');
    expect(relativeTo('D:\\Media\\TV\\Show\\x.mkv', 'd:/media/tv')).toBe('Show\\x.mkv');
  });

  it('leaves unrelated paths alone', () => {
    expect(relativeTo('/storage/TVX/a.mkv', '/storage/TV')).toBe('/storage/TVX/a.mkv');
    expect(relativeTo('/x/a.mkv', null)).toBe('/x/a.mkv');
  });
});

describe('word diff', () => {
  it('tokenizes into words and separators (unicode aware)', () => {
    expect(wordTokens('Kaze no Tsubasa - 風の翼').map((t) => t.kind)).toEqual(['word', 'sep', 'word', 'sep', 'word', 'sep', 'word']);
  });

  it('computes an LCS case-insensitively and ignoring leading zeros', () => {
    expect([...lcsKeep(['Starfall', 'Station', 'S01E01'], ['starfall', 'station', 'S01E01', 'Arrival'])].sort()).toEqual([0, 1, 2]);
    expect([...lcsKeep(['Episode', '07'], ['Episode', '7'])]).toEqual([0, 1]);
    expect([...lcsKeep([], ['a'])]).toEqual([]);
  });
});

describe('diffPath', () => {
  it('dims folders and highlights only the new words of the filename', () => {
    const segs = diffPath(
      'Starfall.Station.S01E01.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv',
      'Starfall Station (2016)/Season 01/Starfall Station - S01E01 - Arrival.mkv',
    );
    expect(segs.filter((s) => s.kind === 'folder').map((s) => s.text)).toEqual(['Starfall Station (2016)', 'Season 01']);
    expect(render(segs)).toBe('Starfall Station (2016)/Season 01/Starfall Station - S01E01 - [Arrival].mkv');
    expect(segs[segs.length - 1]).toEqual({ text: '.mkv', kind: 'ext' });
  });

  it('highlights a run of new words including the separators between them', () => {
    const segs = diffPath('Show.S01E02E03.mkv', 'Show - S01E02-E03 - Dead Reckoning & Signal Fire.mkv');
    expect(render(segs)).toBe('Show - [S01E02-E03 - Dead Reckoning & Signal Fire].mkv');
  });

  it('marks a changed extension', () => {
    const segs = diffPath('movie.avi', 'Movie (2004)/Movie (2004).mkv');
    expect(segs[segs.length - 1]).toEqual({ text: '.mkv', kind: 'changed' });
  });

  it('marks everything as changed when nothing is shared', () => {
    expect(render(diffPath('abc.mkv', 'Totally Different.mkv'))).toBe('[Totally Different].mkv');
  });

  it('keeps the absolute root as its own segment', () => {
    const segs = diffPath('x.mkv', '/lib/X (2001)/X (2001).mkv');
    expect(segs[0]).toEqual({ text: '/', kind: 'root' });
    expect(segs[1]).toEqual({ text: 'lib', kind: 'folder' });
  });

  it('round-trips the proposed path text exactly', () => {
    const proposed = "Kaze no Tsubasa/Specials/Kaze no Tsubasa - S00E01 - Beyond the Clouds (OVA).mkv";
    expect(diffPath('[Group] Kaze no Tsubasa - OVA [BD 1080p].mkv', proposed).map((s) => s.text).join('')).toBe(proposed);
  });

  it('scores similarity between 0 and 1', () => {
    expect(similarity('a b c.mkv', 'a b c.mkv')).toBe(1);
    expect(similarity('abc.mkv', 'xyz.mkv')).toBe(0);
    const s = similarity('Show.S01E01.mkv', 'Show - S01E01 - Pilot.mkv');
    expect(s).toBeGreaterThan(0.3);
    expect(s).toBeLessThan(1);
  });
});
