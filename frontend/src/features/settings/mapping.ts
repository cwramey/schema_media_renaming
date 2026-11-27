/**
 * Pure mappings between the Settings page's friendly controls and the stored settings values.
 * Kept free of React so they can be unit tested (mapping.test.ts).
 */
import type { ConflictPolicy, FormatOut, FormatPreset, MediaKind, RenameMode } from '@/api/types';

export type EditKind = Exclude<MediaKind, 'unknown'>;
export const EDIT_KINDS: readonly EditKind[] = ['movie', 'series', 'anime'];

/** Plural, user-facing kind names (the Settings copy says "Movies / TV / Anime"). */
export const KIND_PLURAL: Record<EditKind, string> = { movie: 'Movies', series: 'TV', anime: 'Anime' };

/* ------------------------------------------------------------------ segmented presets */

export interface PresetOption<V> {
  /** Stable key used as the segmented button value. */
  key: string;
  label: string;
  value: V;
  /** True for the extra option that shows a stored value no preset matches. */
  custom?: boolean;
}

/** Add a "custom" option when the stored value matches none of the presets, so the UI never lies. */
function withCustom<V>(
  presets: PresetOption<V>[],
  value: V,
  same: (a: V, b: V) => boolean,
  customLabel: (v: V) => string,
): PresetOption<V>[] {
  if (presets.some((p) => same(p.value, value))) return presets;
  return [...presets, { key: 'custom', label: customLabel(value), value, custom: true }];
}

export function selectedKey<V>(options: PresetOption<V>[], value: V, same: (a: V, b: V) => boolean = Object.is): string {
  return options.find((o) => same(o.value, value))?.key ?? 'custom';
}

/* settle_seconds */

export const SETTLE_PRESETS: PresetOption<number>[] = [
  { key: '30', label: '30 sec', value: 30 },
  { key: '60', label: '1 min', value: 60 },
  { key: '120', label: '2 min', value: 120 },
  { key: '300', label: '5 min', value: 300 },
];

/** "45 sec", "1 min 30 sec", "10 min". */
export function formatSeconds(s: number): string {
  if (s < 60) return `${s} sec`;
  const m = Math.floor(s / 60);
  const rest = s % 60;
  return rest ? `${m} min ${rest} sec` : `${m} min`;
}

export const settleOptions = (seconds: number) =>
  withCustom(SETTLE_PRESETS, seconds, (a, b) => a === b, (v) => `${formatSeconds(v)} (custom)`);

/* min_confidence */

export const CONFIDENCE_PRESETS: PresetOption<number>[] = [
  { key: 'strict', label: 'Strict 95%', value: 0.95 },
  { key: 'balanced', label: 'Balanced 85%', value: 0.85 },
  { key: 'relaxed', label: 'Relaxed 70%', value: 0.7 },
];

/** Floats from the API (0.8500000001) still count as the preset. */
export const sameConfidence = (a: number, b: number) => Math.abs(a - b) < 0.005;

export const confidenceOptions = (value: number) =>
  withCustom(CONFIDENCE_PRESETS, value, sameConfidence, (v) => `Custom ${Math.round(v * 100)}%`);

/* clear_solved_after_hours */

export const CLEAR_SOLVED_PRESETS: PresetOption<number | null>[] = [
  { key: '0', label: 'Right away', value: 0 },
  { key: '24', label: 'After 1 day', value: 24 },
  { key: '168', label: 'After 7 days', value: 168 },
  { key: 'never', label: 'Never', value: null },
];

/** The backend default is 24 h; an older server without the field behaves like that. */
export const CLEAR_SOLVED_DEFAULT = 24;

export function formatHours(h: number): string {
  if (h % 24 === 0 && h >= 24) return `After ${h / 24} days`;
  return `After ${h} hour${h === 1 ? '' : 's'}`;
}

export const clearSolvedOptions = (hours: number | null | undefined) =>
  withCustom(
    CLEAR_SOLVED_PRESETS,
    hours === undefined ? CLEAR_SOLVED_DEFAULT : hours,
    (a, b) => a === b,
    (v) => (v === null ? 'Never' : `${formatHours(v)} (custom)`),
  );

/* ------------------------------------------------------------------ radio cards */

export const MODE_CARDS: { value: RenameMode; label: string; description: string }[] = [
  { value: 'move', label: 'Move', description: 'The file leaves downloads and goes into your library. Uses no extra space.' },
  { value: 'copy', label: 'Copy', description: 'Keeps the original in downloads. Uses double the space.' },
  {
    value: 'hardlink',
    label: 'Hardlink',
    description: 'The file appears in both places but only takes space once. Best if you still seed torrents.',
  },
  { value: 'symlink', label: 'Symlink', description: 'Puts a shortcut in your library that points back to downloads.' },
  {
    value: 'keeplink',
    label: 'Keep link',
    description: 'Moves the file, then leaves a shortcut in downloads so your torrent app can keep seeding.',
  },
  {
    value: 'test',
    label: 'Test run',
    description: 'Shows exactly what would happen, but changes nothing. Good for trying new settings.',
  },
];

export const CONFLICT_CARDS: { value: ConflictPolicy; label: string; description: string }[] = [
  { value: 'skip', label: 'Ask me', description: 'Leave both files alone and show it in "Needs review". The safest choice.' },
  {
    value: 'upgrade',
    label: 'Keep the better one',
    description: 'Replace the old file only if the new one is higher quality (resolution, codec, HDR).',
  },
  { value: 'overwrite', label: 'Always replace', description: 'The new file wins every time. The old one is deleted.' },
  { value: 'index', label: 'Keep both', description: 'Adds a number to the new file, like "Movie (2026) (2).mkv".' },
  { value: 'fail', label: 'Stop with an error', description: 'Mark the file as failed and do nothing else.' },
];

/* ------------------------------------------------------------------ chips */

/** Extensions are stored lowercase without the leading dot ("MKV", ".mkv" → "mkv"). */
export const normalizeExtension = (raw: string) => raw.trim().replace(/^\.+/, '').toLowerCase();
export const normalizePattern = (raw: string) => raw.trim();
/** Paths: trimmed, trailing separators dropped (but "/" and "C:\" stay intact). */
export const normalizePath = (raw: string) => {
  const t = raw.trim();
  if (/^([A-Za-z]:)?[\\/]+$/.test(t)) return t;
  return t.replace(/[\\/]+$/, '');
};

export const isAbsolutePath = (p: string) => /^(\/|[A-Za-z]:[\\/]|\\\\)/.test(p.trim());

export type ChipCheck = (value: string) => string | null;

export const checkExtension: ChipCheck = (v) =>
  /^[a-z0-9][a-z0-9._!-]*$/.test(v) ? null : 'Use letters and numbers only, like "mkv".';
export const checkAbsolutePath: ChipCheck = (v) => (isAbsolutePath(v) ? null : 'Use a full path, like /media.');

export interface ChipAddResult {
  next: string[];
  /** Why nothing (or not everything) was added. */
  error: string | null;
}

/**
 * Add one or more chips typed into the inline input. Commas, spaces and newlines separate several
 * (except for paths, which may contain spaces: pass `split: false`). Empty and duplicate entries
 * are rejected with a reason.
 */
export function addChips(
  list: readonly string[],
  raw: string,
  normalize: (s: string) => string = normalizePattern,
  check?: ChipCheck,
  split = true,
): ChipAddResult {
  const parts = (split ? raw.split(/[\s,]+/) : [raw]).map(normalize).filter((p) => p.length > 0);
  if (parts.length === 0) return { next: [...list], error: 'Type something first.' };
  const next = [...list];
  let error: string | null = null;
  for (const p of parts) {
    const problem = check?.(p) ?? null;
    if (problem) {
      error = problem;
      continue;
    }
    if (next.includes(p)) {
      error = `"${p}" is already in the list.`;
      continue;
    }
    next.push(p);
  }
  return { next, error };
}

/* ------------------------------------------------------------------ naming presets */

export type NamingChoice =
  | 'jellyfin'
  | 'plex'
  | 'plex-ids'
  | 'emby'
  | 'kodi'
  | 'default'
  | 'anime'
  | 'anime-absolute'
  | 'custom';

export interface NamingOption {
  value: NamingChoice;
  label: string;
}

const COMMON: NamingOption[] = [
  { value: 'jellyfin', label: 'Jellyfin' },
  { value: 'plex', label: 'Plex' },
  { value: 'plex-ids', label: 'Plex + IDs' },
  { value: 'emby', label: 'Emby' },
  { value: 'kodi', label: 'Kodi' },
];

/**
 * Select options per kind. For anime, Schema's default anime layout is offered as "Anime"
 * (the canvas copy) rather than a second "Schema default" entry with the same expression.
 */
export const NAMING_OPTIONS: Record<EditKind, NamingOption[]> = {
  movie: [...COMMON, { value: 'default', label: 'Schema default' }, { value: 'custom', label: 'Custom…' }],
  series: [...COMMON, { value: 'default', label: 'Schema default' }, { value: 'custom', label: 'Custom…' }],
  anime: [
    ...COMMON,
    { value: 'anime', label: 'Anime' },
    { value: 'anime-absolute', label: 'Anime absolute' },
    { value: 'custom', label: 'Custom…' },
  ],
};

/** Catalog preset id behind a choice ("plex-ids" + series → "plex-ids-series"). */
export function presetIdFor(choice: NamingChoice, kind: EditKind): string | null {
  switch (choice) {
    case 'custom':
      return null;
    case 'anime-absolute':
      return kind === 'anime' ? 'anime-absolute' : null;
    case 'anime':
      return kind === 'anime' ? 'default-anime' : null;
    case 'default':
      return `default-${kind}`;
    default:
      return `${choice}-${kind}`;
  }
}

/** Whitespace-insensitive comparison (formats are often pasted with stray spaces/newlines). */
const squash = (s: string) => s.replace(/\s+/g, ' ').trim();

/** Which select option an expression corresponds to; anything unrecognised is "custom". */
export function detectPreset(
  expression: string | null | undefined,
  kind: EditKind,
  presets: readonly Pick<FormatPreset, 'id' | 'kind' | 'expression'>[],
): NamingChoice {
  if (!expression) return 'custom';
  const target = squash(expression);
  for (const opt of NAMING_OPTIONS[kind]) {
    const id = presetIdFor(opt.value, kind);
    if (!id) continue;
    const preset = presets.find((p) => p.id === id);
    if (preset && squash(preset.expression) === target) return opt.value;
  }
  return 'custom';
}

/** Sample release names for the live naming preview (fictional titles, matching the canvas). */
export const PREVIEW_SAMPLES: Record<EditKind, string> = {
  movie: 'Never.After.Dark.2026.1080p.WEBRip.DUAL.10Bit.DDP5.1.x265-NeoNoir.mkv',
  series: 'Starfall.Station.S01E02.1080p.WEB-DL.mkv',
  anime: '[NeoSubs] Moonlit Harbor - 15 (1080p) [8F3A21C0].mkv',
};

/** Join a library root and a format output for display ("/media/movies" + "X/X.mkv"). */
export function joinRoot(root: string | undefined, rel: string): string {
  if (!root) return rel;
  const sep = root.includes('\\') && !root.includes('/') ? '\\' : '/';
  return `${root.replace(/[\\/]+$/, '')}${sep}${rel.replace(/^[\\/]+/, '')}`;
}

/* ------------------------------------------------------------------ misc */

/** Ids a settings row hands to its control: `<label for={id}>`, help text at `${id}-help`. */
export const rowIds = (id: string) => ({ id, labelId: `${id}-label`, helpId: `${id}-help` });

export const SECTION_IDS = ['folders', 'naming', 'matching', 'moving', 'connections', 'downloads', 'files', 'account'] as const;
export type SectionId = (typeof SECTION_IDS)[number];

/** Old `/settings/:tab` URLs → the section that replaced the tab. */
export const TAB_TO_SECTION: Record<string, SectionId> = {
  general: 'folders',
  providers: 'connections',
  'media-servers': 'connections',
  mediaservers: 'connections',
  automation: 'downloads',
  account: 'account',
  about: 'account',
};

/** "#naming" → "naming" when it names a section, else null. */
export function sectionFromHash(hash: string): SectionId | null {
  const id = decodeURIComponent(hash.replace(/^#/, ''));
  return (SECTION_IDS as readonly string[]).includes(id) ? (id as SectionId) : null;
}

/** "http://192.168.1.20:8096/" → "192.168.1.20:8096". */
export function hostOf(url: string): string {
  try {
    return new URL(url).host || url;
  } catch {
    return url.replace(/^https?:\/\//, '').replace(/\/+$/, '');
  }
}

/** schema-settings-20261030.json */
export function backupFileName(d: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `schema-settings-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}.json`;
}

/** Rough shape check of a backup file before it is sent to /settings/restore. */
export function looksLikeBackup(data: unknown): data is { schema_backup: number; created_at?: string } {
  return !!data && typeof data === 'object' && 'schema_backup' in data;
}

/* ------------------------------------------------------------------ connection status */

export type ConnTone = 'neutral' | 'success' | 'info' | 'warning' | 'danger';

export interface ProviderLike {
  name: string;
  requires_key: boolean;
  configured: boolean;
  healthy: boolean | null;
  last_error: string | null;
  requests_made: number;
}

/** Providers Schema works fine without: never shown as a warning when they aren't set up. */
export const OPTIONAL_PROVIDERS = new Set(['tvdb', 'anidb', 'trakt']);

/**
 * Dot + text for a provider row. Unconfigured providers are grey ("Not set up · optional"),
 * never amber; only a configured provider that actually failed is red.
 */
export function providerConn(p: ProviderLike, off: boolean): { tone: ConnTone; status: string; needsSetup: boolean } {
  if (p.requires_key && !p.configured) {
    return { tone: 'neutral', status: OPTIONAL_PROVIDERS.has(p.name) ? 'Not set up · optional' : 'Not set up', needsSetup: true };
  }
  if (off) return { tone: 'neutral', status: 'Turned off', needsSetup: false };
  if (p.healthy === false) return { tone: 'danger', status: 'Couldn’t connect', needsSetup: false };
  const lookups = p.requests_made > 0 ? ` · ${p.requests_made.toLocaleString()} lookups` : '';
  if (!p.requires_key) return { tone: 'success', status: `On · no key needed${lookups}`, needsSetup: false };
  if (p.healthy === true) return { tone: 'success', status: `Connected${lookups}`, needsSetup: false };
  return { tone: 'info', status: `Set up${lookups} · not tested yet`, needsSetup: false };
}

/* ------------------------------------------------------------------ lists & languages */

/** Move `from` to `to` (both in range), returning a new array. */
export function moveItem<T>(list: readonly T[], from: number, to: number): T[] {
  if (from === to || from < 0 || to < 0 || from >= list.length || to >= list.length) return [...list];
  const next = list.slice();
  const [x] = next.splice(from, 1);
  next.splice(to, 0, x);
  return next;
}

export const LANGUAGES: { code: string; name: string }[] = [
  { code: 'en', name: 'English' },
  { code: 'ja', name: 'Japanese' },
  { code: 'de', name: 'German' },
  { code: 'es', name: 'Spanish' },
  { code: 'fr', name: 'French' },
  { code: 'it', name: 'Italian' },
  { code: 'pt', name: 'Portuguese' },
  { code: 'nl', name: 'Dutch' },
  { code: 'sv', name: 'Swedish' },
  { code: 'da', name: 'Danish' },
  { code: 'no', name: 'Norwegian' },
  { code: 'fi', name: 'Finnish' },
  { code: 'pl', name: 'Polish' },
  { code: 'ru', name: 'Russian' },
  { code: 'ko', name: 'Korean' },
  { code: 'zh', name: 'Chinese' },
];

/** The kind's current default format (user formats win over presets; there is one per kind). */
export function defaultFormat(formats: readonly FormatOut[] | undefined, kind: EditKind): FormatOut | undefined {
  return formats?.find((f) => f.kind === kind && f.is_default);
}

/* ------------------------------------------------------------------ download clients */

export type DownloadClient = 'qbittorrent' | 'sabnzbd' | 'generic';

export const MASKED_KEY = '••••••••';

/** Where the download app keeps the key: a one-line header file curl reads with `-H @file`. */
export const KEY_FILE = '/config/schema-api-key';

export interface ClientSnippet {
  where: string;
  /** The command or settings to paste. Never holds the key when `keyFile` is set. */
  code: string;
  /** A file to create first; its content holds the key, so commands stay free of it. */
  keyFile?: { path: string; content: string; where: string };
}

/** The key as a header line, the format curl's `-H @file` and the hook scripts read. */
export function keyFileContent(key: string): string {
  return `X-Api-Key: ${key}`;
}

/**
 * Copy-ready hook for each download client. The key never goes into a command line (where `ps`,
 * the client's settings file and its execution log would show it): commands read it from
 * `keyFile`, and SABnzbd's script reads it from its environment.
 */
export function clientSnippet(client: DownloadClient, origin: string, key: string): ClientSnippet {
  const keyFile = (where: string) => ({ path: KEY_FILE, content: keyFileContent(key), where });
  switch (client) {
    case 'qbittorrent':
      return {
        where:
          'Paste the command in qBittorrent → Options → Downloads → "Run external program on torrent finished". It reads the key from the key file, so save that first. Failed downloads are ignored.',
        code: `curl -fsS -X POST -H @${KEY_FILE} --data-urlencode "content_path=%F" --data-urlencode "name=%N" --data-urlencode "category=%L" --data-urlencode "hash=%I" --data-urlencode "save_path=%D" "${origin}/api/webhooks/qbittorrent"`,
        keyFile: keyFile(
          `Key file: save this line as ${KEY_FILE} inside qBittorrent’s container (its config folder), readable only by qBittorrent.`,
        ),
      };
    case 'sabnzbd':
      return {
        where:
          'Copy deploy/hooks/sabnzbd/schema_notify.py into SABnzbd’s scripts folder, pick it as the post-processing script for your categories, and give SABnzbd’s container these environment variables. Failed downloads are ignored.',
        code: `SCHEMA_URL=${origin}\nSCHEMA_API_KEY=${key}`,
      };
    case 'generic':
      return {
        where:
          'Any tool that can send a web request when a download finishes (Sonarr/Radarr custom scripts, cron, n8n …) can call this, or use deploy/hooks/generic/schema-notify.sh.',
        code: `curl -fsS -X POST -H @${KEY_FILE} -H "Content-Type: application/json" \\\n  -d '{"path": "/media/downloads/complete/Some.Show.S01E01.mkv", "kind": "series"}' \\\n  "${origin}/api/webhooks/generic"`,
        keyFile: keyFile(`Key file: save this line as ${KEY_FILE}, or any private file (then change the path in the command).`),
      };
  }
}
