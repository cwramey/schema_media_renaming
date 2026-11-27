/** Where retired routes went in the Home · History · Settings redesign. */

/** Settings section anchors, in page order. */
export const SETTINGS_SECTIONS = ['folders', 'naming', 'matching', 'moving', 'connections', 'downloads', 'files', 'account'] as const;
export type SettingsSection = (typeof SETTINGS_SECTIONS)[number];

const TAB_TO_SECTION: Record<string, SettingsSection> = {
  general: 'folders',
  providers: 'connections',
  'media-servers': 'connections',
  mediaservers: 'connections',
  automation: 'downloads',
  account: 'account',
  about: 'account',
};

/** Old `/settings/:tab` URL → the anchor on the one-page Settings (unknown tabs → the page top). */
export function settingsTabTarget(tab: string | undefined): string {
  const section = tab ? TAB_TO_SECTION[tab.toLowerCase()] : undefined;
  return section ? `/settings#${section}` : '/settings';
}

/** Fixed redirects for the old top-level pages. */
export const LEGACY_REDIRECTS: Readonly<Record<string, string>> = {
  jobs: '/',
  activity: '/history',
  watch: '/settings#folders',
  formats: '/settings#naming',
  import: '/?import=1',
};

/** Link to one item in the match workspace (Home "Choose match", History "Fix"). */
export function itemLink(jobId: number, itemId: number | null | undefined): string {
  return itemId === null || itemId === undefined ? `/jobs/${jobId}` : `/jobs/${jobId}?item=${itemId}`;
}

/** The History page, optionally searching for `q`. */
export function historySearchLink(q: string): string {
  const t = q.trim();
  return t ? `/history?q=${encodeURIComponent(t)}` : '/history';
}
