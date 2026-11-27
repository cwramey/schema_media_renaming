import { describe, expect, it } from 'vitest';
import { historySearchLink, itemLink, LEGACY_REDIRECTS, settingsTabTarget } from './routes';

describe('settingsTabTarget', () => {
  it('maps every old settings tab to its section anchor', () => {
    expect(settingsTabTarget('general')).toBe('/settings#folders');
    expect(settingsTabTarget('providers')).toBe('/settings#connections');
    expect(settingsTabTarget('media-servers')).toBe('/settings#connections');
    expect(settingsTabTarget('mediaservers')).toBe('/settings#connections');
    expect(settingsTabTarget('automation')).toBe('/settings#downloads');
    expect(settingsTabTarget('account')).toBe('/settings#account');
    expect(settingsTabTarget('about')).toBe('/settings#account');
    expect(settingsTabTarget('General')).toBe('/settings#folders');
  });

  it('sends anything else to the top of Settings', () => {
    expect(settingsTabTarget('nope')).toBe('/settings');
    expect(settingsTabTarget(undefined)).toBe('/settings');
  });
});

describe('legacy redirects and links', () => {
  it('covers the retired pages', () => {
    expect(LEGACY_REDIRECTS).toEqual({
      jobs: '/',
      activity: '/history',
      watch: '/settings#folders',
      formats: '/settings#naming',
      import: '/?import=1',
    });
  });

  it('builds item and search links', () => {
    expect(itemLink(6, 123)).toBe('/jobs/6?item=123');
    expect(itemLink(6, null)).toBe('/jobs/6');
    expect(historySearchLink('  Blade Runner ')).toBe('/history?q=Blade%20Runner');
    expect(historySearchLink('')).toBe('/history');
  });
});
