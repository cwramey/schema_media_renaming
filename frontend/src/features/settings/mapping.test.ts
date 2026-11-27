import { describe, expect, it } from 'vitest';
import {
  addChips,
  clientSnippet,
  moveItem,
  backupFileName,
  checkAbsolutePath,
  checkExtension,
  clearSolvedOptions,
  confidenceOptions,
  detectPreset,
  formatSeconds,
  hostOf,
  joinRoot,
  normalizeExtension,
  normalizePath,
  presetIdFor,
  providerConn,
  sameConfidence,
  sectionFromHash,
  selectedKey,
  settleOptions,
} from './mapping';

describe('settle presets', () => {
  it('maps the four presets to seconds', () => {
    const opts = settleOptions(120);
    expect(opts.map((o) => [o.label, o.value])).toEqual([
      ['30 sec', 30],
      ['1 min', 60],
      ['2 min', 120],
      ['5 min', 300],
    ]);
    expect(selectedKey(opts, 120)).toBe('120');
  });

  it('shows a stored non-preset value as an extra custom option', () => {
    const opts = settleOptions(45);
    expect(opts).toHaveLength(5);
    expect(opts[4]).toMatchObject({ key: 'custom', value: 45, label: '45 sec (custom)', custom: true });
    expect(selectedKey(opts, 45)).toBe('custom');
    expect(settleOptions(90)[4].label).toBe('1 min 30 sec (custom)');
  });

  it('formats seconds', () => {
    expect(formatSeconds(30)).toBe('30 sec');
    expect(formatSeconds(600)).toBe('10 min');
  });
});

describe('confidence presets', () => {
  it('maps Strict/Balanced/Relaxed to 0.95/0.85/0.70', () => {
    const opts = confidenceOptions(0.85);
    expect(opts.map((o) => [o.key, o.value])).toEqual([
      ['strict', 0.95],
      ['balanced', 0.85],
      ['relaxed', 0.7],
    ]);
  });

  it('tolerates float noise from the API', () => {
    expect(sameConfidence(0.8500000001, 0.85)).toBe(true);
    expect(selectedKey(confidenceOptions(0.70000001), 0.70000001, sameConfidence)).toBe('relaxed');
  });

  it('adds a custom option for other thresholds', () => {
    const opts = confidenceOptions(0.6);
    expect(opts.at(-1)).toMatchObject({ key: 'custom', label: 'Custom 60%', value: 0.6 });
    expect(selectedKey(opts, 0.6, sameConfidence)).toBe('custom');
  });
});

describe('clear solved mapping', () => {
  it('maps Right away / 1 day / 7 days / Never to 0 / 24 / 168 / null', () => {
    const opts = clearSolvedOptions(24);
    expect(opts.map((o) => [o.label, o.value])).toEqual([
      ['Right away', 0],
      ['After 1 day', 24],
      ['After 7 days', 168],
      ['Never', null],
    ]);
    expect(selectedKey(opts, null)).toBe('never');
    expect(selectedKey(opts, 0)).toBe('0');
  });

  it('treats a missing field (older server) as the 24 h default', () => {
    expect(clearSolvedOptions(undefined)).toHaveLength(4);
  });

  it('shows other values as custom', () => {
    const opts = clearSolvedOptions(48);
    expect(opts.at(-1)).toMatchObject({ key: 'custom', value: 48, label: 'After 2 days (custom)' });
    expect(clearSolvedOptions(5).at(-1)?.label).toBe('After 5 hours (custom)');
  });
});

describe('chip normalization', () => {
  it('lowercases extensions and drops leading dots', () => {
    expect(normalizeExtension(' .MKV ')).toBe('mkv');
    expect(normalizeExtension('..srt')).toBe('srt');
  });

  it('adds several chips at once, rejecting empties and duplicates', () => {
    expect(addChips(['mkv'], '.MP4, avi  mkv', normalizeExtension, checkExtension)).toEqual({
      next: ['mkv', 'mp4', 'avi'],
      error: '"mkv" is already in the list.',
    });
    expect(addChips(['mkv'], '   ', normalizeExtension)).toEqual({ next: ['mkv'], error: 'Type something first.' });
    expect(addChips([], 'a b a')).toEqual({ next: ['a', 'b'], error: '"a" is already in the list.' });
  });

  it('validates extensions and paths', () => {
    expect(addChips([], 'm k/v', normalizeExtension, checkExtension, false).error).toMatch(/letters and numbers/);
    expect(addChips([], 'media', normalizePath, checkAbsolutePath, false).error).toMatch(/full path/);
    expect(addChips([], '/media/My Movies/', normalizePath, checkAbsolutePath, false)).toEqual({
      next: ['/media/My Movies'],
      error: null,
    });
    expect(checkAbsolutePath('D:\\Media')).toBeNull();
    expect(normalizePath('/')).toBe('/');
    expect(normalizePath('C:\\')).toBe('C:\\');
  });
});

describe('naming preset detection', () => {
  const presets = [
    { id: 'jellyfin-movie', kind: 'movie' as const, expression: '{ny} [tmdbid-{tmdbid}]/{ny}' },
    { id: 'plex-movie', kind: 'movie' as const, expression: '{ny}/{ny}' },
    { id: 'default-movie', kind: 'movie' as const, expression: "{ny}/{ny}{' - ' + edition}" },
    { id: 'plex-series', kind: 'series' as const, expression: '{ny}/Season {s}/{ny} - {s00e00}' },
    { id: 'default-anime', kind: 'anime' as const, expression: '{n}/{n} - {s00e00}' },
    { id: 'anime-absolute', kind: 'anime' as const, expression: '{n}/{n} - {absolute}' },
  ];

  it('maps choices to catalog preset ids', () => {
    expect(presetIdFor('plex-ids', 'series')).toBe('plex-ids-series');
    expect(presetIdFor('default', 'movie')).toBe('default-movie');
    expect(presetIdFor('anime', 'anime')).toBe('default-anime');
    expect(presetIdFor('anime-absolute', 'anime')).toBe('anime-absolute');
    expect(presetIdFor('anime-absolute', 'movie')).toBeNull();
    expect(presetIdFor('custom', 'movie')).toBeNull();
  });

  it('recognises preset expressions, ignoring stray whitespace', () => {
    expect(detectPreset('{ny}/{ny}', 'movie', presets)).toBe('plex');
    expect(detectPreset('  {ny} [tmdbid-{tmdbid}]/{ny}\n', 'movie', presets)).toBe('jellyfin');
    expect(detectPreset("{ny}/{ny}{' - ' + edition}", 'movie', presets)).toBe('default');
    expect(detectPreset('{n}/{n} - {absolute}', 'anime', presets)).toBe('anime-absolute');
    expect(detectPreset('{n}/{n} - {s00e00}', 'anime', presets)).toBe('anime');
  });

  it('treats unknown, empty and other-kind expressions as custom', () => {
    expect(detectPreset('{n} hand made', 'movie', presets)).toBe('custom');
    expect(detectPreset(undefined, 'movie', presets)).toBe('custom');
    expect(detectPreset('{ny}/Season {s}/{ny} - {s00e00}', 'movie', presets)).toBe('custom');
  });
});

describe('provider connection status', () => {
  const base = { requires_key: true, configured: false, healthy: null, last_error: null, requests_made: 0 };

  it('shows unconfigured optional providers as grey, never amber', () => {
    for (const name of ['tvdb', 'anidb', 'trakt']) {
      expect(providerConn({ ...base, name }, false)).toEqual({ tone: 'neutral', status: 'Not set up · optional', needsSetup: true });
    }
    expect(providerConn({ ...base, name: 'tmdb' }, false).tone).toBe('neutral');
  });

  it('is green when connected, red only when a configured provider fails', () => {
    expect(providerConn({ ...base, name: 'tmdb', configured: true, healthy: true, requests_made: 405 }, false)).toMatchObject({
      tone: 'success',
      status: 'Connected · 405 lookups',
    });
    expect(providerConn({ ...base, name: 'tmdb', configured: true, healthy: false }, false).tone).toBe('danger');
    expect(providerConn({ ...base, name: 'tvmaze', requires_key: false, configured: true }, false)).toMatchObject({
      tone: 'success',
      status: 'On · no key needed',
    });
    expect(providerConn({ ...base, name: 'tvmaze', requires_key: false, configured: true }, true).status).toBe('Turned off');
  });
});

describe('misc helpers', () => {
  it('parses section hashes', () => {
    expect(sectionFromHash('#naming')).toBe('naming');
    expect(sectionFromHash('#nope')).toBeNull();
    expect(sectionFromHash('')).toBeNull();
  });

  it('joins library roots and relative paths', () => {
    expect(joinRoot('/media/movies/', 'A (2020)/A (2020).mkv')).toBe('/media/movies/A (2020)/A (2020).mkv');
    expect(joinRoot('D:\\Media', 'A.mkv')).toBe('D:\\Media\\A.mkv');
    expect(joinRoot(undefined, 'A.mkv')).toBe('A.mkv');
  });

  it('shows host:port of a server URL', () => {
    expect(hostOf('http://192.168.1.20:8096/')).toBe('192.168.1.20:8096');
    expect(hostOf('not a url')).toBe('not a url');
  });

  it('names backups by date', () => {
    expect(backupFileName(new Date(2026, 9, 3))).toBe('schema-settings-20261003.json');
  });

  it('reorders lists', () => {
    expect(moveItem(['a', 'b', 'c'], 0, 2)).toEqual(['b', 'c', 'a']);
    expect(moveItem(['a', 'b', 'c'], 2, 0)).toEqual(['c', 'a', 'b']);
    expect(moveItem(['a', 'b'], 0, 5)).toEqual(['a', 'b']);
  });

  it('builds download-client hooks with the real origin and key', () => {
    const q = clientSnippet('qbittorrent', 'http://nas:8420', 'KEY');
    expect(q.code).toContain('"http://nas:8420/api/webhooks/qbittorrent"');
    expect(q.keyFile).toMatchObject({ path: '/config/schema-api-key', content: 'X-Api-Key: KEY' });
    expect(clientSnippet('sabnzbd', 'http://nas:8420', 'KEY').code).toBe('SCHEMA_URL=http://nas:8420\nSCHEMA_API_KEY=KEY');
  });

  it('keeps the key out of command lines', () => {
    for (const client of ['qbittorrent', 'generic'] as const) {
      const s = clientSnippet(client, 'http://nas:8420', 'SECRET-KEY');
      expect(s.code).not.toContain('SECRET-KEY');
      expect(s.code).not.toContain('apikey');
      expect(s.code).toContain(`-H @${s.keyFile?.path}`);
      expect(s.keyFile?.content).toBe('X-Api-Key: SECRET-KEY');
    }
    expect(clientSnippet('sabnzbd', 'http://nas:8420', 'K').keyFile).toBeUndefined();
  });
});
