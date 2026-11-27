import { describe, expect, it } from 'vitest';
import { safeNext } from './safeNext';

const ORIGIN = 'http://nas.lan:8420';

describe('safeNext', () => {
  it('keeps paths inside the app', () => {
    expect(safeNext('/jobs/12', ORIGIN)).toBe('/jobs/12');
    expect(safeNext('/history?q=dune#top', ORIGIN)).toBe('/history?q=dune#top');
    expect(safeNext('/settings/a/../b', ORIGIN)).toBe('/settings/b');
  });

  it('falls back to / for anything that could leave the origin', () => {
    for (const raw of [
      null,
      '',
      'jobs',
      'https://evil.example/',
      '//evil.example/',
      '/\\evil.example/',
      '\\\\evil.example',
      '/\t/evil.example',
      '/\n/evil.example',
      '/%0a/x\u0000',
      'javascript:alert(1)',
      '/\u007f',
    ]) {
      expect(safeNext(raw, ORIGIN)).toBe('/');
    }
  });

  it('never loops back to the sign-in pages', () => {
    expect(safeNext('/login', ORIGIN)).toBe('/');
    expect(safeNext('/setup?x=1', ORIGIN)).toBe('/');
    expect(safeNext('/a/../login', ORIGIN)).toBe('/');
  });
});
