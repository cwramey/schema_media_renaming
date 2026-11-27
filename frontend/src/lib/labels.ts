/** Human labels, tones and descriptions for contract enums and warning codes. */
import type { ConflictPolicy, ItemStatus, JobSource, JobStatus, MediaKind, RenameMode, WatchStatus } from '@/api/types';

export type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'danger' | 'brand';

export const KIND_LABEL: Record<MediaKind, string> = {
  movie: 'Movie',
  series: 'Series',
  anime: 'Anime',
  unknown: 'Unknown',
};

export const JOB_STATUS: Record<JobStatus, { label: string; tone: Tone }> = {
  queued: { label: 'Queued', tone: 'neutral' },
  matching: { label: 'Matching', tone: 'info' },
  review: { label: 'Needs review', tone: 'warning' },
  ready: { label: 'Ready', tone: 'brand' },
  executing: { label: 'Renaming', tone: 'info' },
  completed: { label: 'Completed', tone: 'success' },
  failed: { label: 'Failed', tone: 'danger' },
  cancelled: { label: 'Cancelled', tone: 'neutral' },
};

export const ITEM_STATUS: Record<ItemStatus, { label: string; tone: Tone }> = {
  pending: { label: 'Pending', tone: 'neutral' },
  matched: { label: 'Matched', tone: 'success' },
  review: { label: 'Review', tone: 'warning' },
  approved: { label: 'Approved', tone: 'brand' },
  skipped: { label: 'Skipped', tone: 'neutral' },
  executing: { label: 'Renaming', tone: 'info' },
  done: { label: 'Done', tone: 'success' },
  error: { label: 'Error', tone: 'danger' },
};

export const WATCH_STATUS: Record<WatchStatus, { label: string; tone: Tone }> = {
  watching: { label: 'Watching', tone: 'success' },
  stopped: { label: 'Stopped', tone: 'neutral' },
  error: { label: 'Error', tone: 'danger' },
  missing: { label: 'Missing', tone: 'warning' },
};

export const SOURCE_LABEL: Record<JobSource, string> = {
  manual: 'Manual',
  watch: 'Watch folder',
  webhook: 'Webhook',
};

export const MODE_INFO: Record<RenameMode, { label: string; verb: string; description: string }> = {
  move: { label: 'Move', verb: 'Rename', description: 'Move and rename the file into the library.' },
  copy: { label: 'Copy', verb: 'Copy', description: 'Copy into the library; the original stays put.' },
  hardlink: { label: 'Hardlink', verb: 'Link', description: 'Hardlink into the library (same filesystem only).' },
  symlink: { label: 'Symlink', verb: 'Link', description: 'Create a symlink in the library pointing at the original.' },
  keeplink: {
    label: 'Keeplink',
    verb: 'Rename',
    description: 'Move into the library and leave a symlink behind (keeps torrents seeding).',
  },
  test: { label: 'Test (dry run)', verb: 'Test', description: 'Plan and validate only. Nothing on disk changes.' },
};

/** What a move does with an item's folder (job table, history). */
export const ITEM_ACTION = {
  folder_renamed: { label: 'Folder renamed', tone: 'brand' },
  moved: { label: 'Moved', tone: 'neutral' },
} as const satisfies Record<string, { label: string; tone: Tone }>;

export type ItemAction = keyof typeof ITEM_ACTION;

export const CONFLICT_INFO: Record<ConflictPolicy, { label: string; description: string }> = {
  skip: { label: 'Skip', description: 'Leave the source untouched and flag the item for review.' },
  overwrite: { label: 'Overwrite', description: 'Replace the existing file (it is kept in the trash folder).' },
  upgrade: { label: 'Upgrade', description: 'Replace only when the incoming file is better quality.' },
  index: { label: 'Index', description: 'Keep both, adding " (1)", " (2)" … to the new name.' },
  fail: { label: 'Fail', description: 'Stop the item with an error.' },
};

/** Known warning codes (SPEC §3.3) → readable text. Unknown warnings are shown verbatim. */
const WARNING_TEXT: Record<string, string> = {
  crc_mismatch: 'CRC in the filename does not match the file contents',
  crc_ok: 'CRC verified',
  destination_exists: 'A file already exists at the destination',
  replaces_existing: 'A file already exists at the destination; it will be moved to the trash and replaced',
  upgrade_existing: 'A file already exists at the destination; it is replaced only if this one is better quality',
  indexed_existing: 'A file already exists at the destination; this one will get a numbered name',
  duplicate_in_batch: 'Another file in this job renders to the same destination',
  interrupted: 'Renaming was interrupted; check History to see whether the file moved',
  low_confidence: 'Low match confidence',
  no_match: 'No provider found a match',
  sample: 'Sample file',
  extra: 'Extra (trailer, featurette …)',
  ambiguous: 'Several candidates scored almost the same',
  episode_not_found: 'Episode not found at the provider',
  dry_run_ok: 'Dry run passed; a real run would write',
  dry_run_note: 'Dry run note',
  dry_run_failed: 'Dry run found a problem; a real run would fail',
};

export function warningText(code: string): string {
  if (WARNING_TEXT[code]) return WARNING_TEXT[code];
  const [head, ...rest] = code.split(':');
  if (rest.length > 0 && WARNING_TEXT[head]) return `${WARNING_TEXT[head]}:${rest.join(':')}`;
  return code;
}

export function warningTone(code: string): Tone {
  if (
    code.startsWith('crc_mismatch') ||
    code.startsWith('destination_exists') ||
    code.startsWith('interrupted') ||
    code.startsWith('dry_run_failed')
  ) {
    return 'danger';
  }
  return 'warning';
}

export const PROVIDER_LABEL: Record<string, string> = {
  tmdb: 'TMDb',
  tvdb: 'TheTVDB',
  anidb: 'AniDB',
  trakt: 'Trakt',
  tvmaze: 'TVmaze',
  fixtures: 'Demo',
  animelists: 'Anime-Lists',
};

export const providerLabel = (name: string) => PROVIDER_LABEL[name] ?? name;

/** Credential fields (ProviderCredentials) grouped by provider for the settings form. */
export const PROVIDER_CREDENTIAL_FIELDS: Record<string, { field: string; label: string; secret: boolean }[]> = {
  tmdb: [{ field: 'tmdb_api_key', label: 'API key or v4 read token', secret: true }],
  tvdb: [
    { field: 'tvdb_api_key', label: 'API key', secret: true },
    { field: 'tvdb_pin', label: 'Subscriber PIN (optional)', secret: true },
  ],
  anidb: [
    { field: 'anidb_client', label: 'Client name', secret: false },
    { field: 'anidb_client_version', label: 'Client version', secret: false },
  ],
  trakt: [{ field: 'trakt_client_id', label: 'Client ID', secret: true }],
};
