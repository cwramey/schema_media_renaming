/**
 * Wire types mirroring backend/schema_app/domain.py and backend/schema_app/api_models.py.
 *
 * Field names are snake_case exactly as on the wire. Python `datetime` / `date`
 * become ISO strings. Optional-with-default Pydantic fields are always present in
 * responses, so they are typed as required here; `X | None` stays `X | null`.
 * Keep this file in lockstep with the contract files.
 */

/* ------------------------------------------------------------------ domain.py: enums */

export type MediaKind = 'movie' | 'series' | 'anime' | 'unknown';
export const MEDIA_KINDS: readonly MediaKind[] = ['movie', 'series', 'anime', 'unknown'];

export type EpisodeOrder = 'aired' | 'dvd' | 'absolute';
export const EPISODE_ORDERS: readonly EpisodeOrder[] = ['aired', 'dvd', 'absolute'];

export type RenameMode = 'move' | 'copy' | 'hardlink' | 'symlink' | 'keeplink' | 'test';
export const RENAME_MODES: readonly RenameMode[] = ['move', 'copy', 'hardlink', 'symlink', 'keeplink', 'test'];

export type ConflictPolicy = 'skip' | 'overwrite' | 'upgrade' | 'index' | 'fail';
export const CONFLICT_POLICIES: readonly ConflictPolicy[] = ['skip', 'overwrite', 'upgrade', 'index', 'fail'];

/** ISO-8601 datetime string (Python `datetime`). */
export type DateTimeString = string;
/** ISO-8601 date string `YYYY-MM-DD` (Python `date`). */
export type DateString = string;

/* ------------------------------------------------------------------ domain.py: parsing */

export interface QualityInfo {
  resolution: string | null;
  source: string | null;
  remux: boolean;
  video_codec: string | null;
  bit_depth: number | null;
  hdr: string[];
  audio_codec: string | null;
  audio_channels: string | null;
  atmos: boolean;
  streaming_service: string | null;
  proper: boolean;
  repack: boolean;
  s3d: string | null;
}

export interface ParsedMedia {
  path: string;
  filename: string;
  extension: string;
  kind: MediaKind;
  title: string | null;
  alt_titles: string[];
  year: number | null;
  season: number | null;
  episodes: number[];
  absolute: number[];
  air_date: DateString | null;
  special: boolean;
  special_title: string | null;
  /** Text after the numbering ("S01E02 - Dia de los Dangerous"); checks the episode number. */
  episode_title?: string | null;
  part: number | null;
  edition: string | null;
  release_group: string | null;
  crc32: string | null;
  version: number | null;
  quality: QualityInfo;
  languages: string[];
  is_sample: boolean;
  is_extra: boolean;
  folder_title: string | null;
  confidence: number;
  tokens: string[];
}

export interface ProbeInfo {
  container: string | null;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  video_codec: string | null;
  bit_depth: number | null;
  frame_rate: number | null;
  hdr: string[];
  audio_codec: string | null;
  audio_channels: string | null;
  audio_languages: string[];
  subtitle_languages: string[];
  bitrate_kbps: number | null;
}

/* ------------------------------------------------------------------ domain.py: metadata */

export interface ProviderRef {
  provider: string;
  id: string;
}

export interface MovieInfo {
  ref: ProviderRef;
  title: string;
  original_title: string | null;
  year: number | null;
  release_date: DateString | null;
  aliases: string[];
  overview: string | null;
  poster_url: string | null;
  ids: Record<string, string>;
  popularity: number | null;
  runtime_minutes: number | null;
  genres: string[];
  collection: string | null;
  certification: string | null;
  rating: number | null;
  original_language: string | null;
}

export interface SeriesInfo {
  ref: ProviderRef;
  title: string;
  original_title: string | null;
  year: number | null;
  first_aired: DateString | null;
  aliases: string[];
  overview: string | null;
  poster_url: string | null;
  ids: Record<string, string>;
  popularity: number | null;
  status: string | null;
  network: string | null;
  genres: string[];
  is_anime: boolean;
  season_count: number | null;
  certification: string | null;
  rating: number | null;
  original_language: string | null;
}

export interface EpisodeInfo {
  series_ref: ProviderRef;
  season: number | null;
  episode: number | null;
  absolute: number | null;
  title: string | null;
  air_date: DateString | null;
  ids: Record<string, string>;
}

export interface Candidate {
  /** Stable: `${provider}:${kind}:${provider_id}`, e.g. "tmdb:movie:603". */
  id: string;
  kind: MediaKind;
  provider: string;
  title: string;
  year: number | null;
  score: number;
  movie: MovieInfo | null;
  series: SeriesInfo | null;
  episodes: EpisodeInfo[];
  episode_order: EpisodeOrder | null;
  /** "Part 2" of a release that splits this one provider episode (named "- part2"). */
  part?: number | null;
  reasons: string[];
}

export interface MatchHint {
  kind?: MediaKind | null;
  provider?: string | null;
  query?: string | null;
  /** Alias of `query`. */
  title?: string | null;
  year?: number | null;
  season?: number | null;
  episodes?: number[] | null;
  absolute?: number[] | null;
  ref?: ProviderRef | null;
  episode_order?: EpisodeOrder | null;
}

export interface MatchResult {
  candidates: Candidate[];
  best: Candidate | null;
  confidence: number;
  warnings: string[];
  providers_tried: string[];
  provider_errors: Record<string, string>;
}

/* ------------------------------------------------------------------ domain.py: configuration */

export interface ProviderCredentials {
  tmdb_api_key: string | null;
  tvdb_api_key: string | null;
  tvdb_pin: string | null;
  anidb_client: string | null;
  anidb_client_version: number | null;
  trakt_client_id: string | null;
}

export interface ProviderSettings {
  credentials: ProviderCredentials;
  language: string;
  priority: Partial<Record<MediaKind, string[]>>;
  disabled: string[];
}

export interface ProviderStatus {
  name: string;
  label: string;
  kinds: MediaKind[];
  requires_key: boolean;
  configured: boolean;
  enabled: boolean;
  healthy: boolean | null;
  last_error: string | null;
  last_checked: DateTimeString | null;
  rate_limit: string | null;
  requests_made: number;
  cache_hits: number;
}

export interface PathMapping {
  source: string;
  target: string;
}

export type MediaServerKind = 'plex' | 'jellyfin' | 'emby';

export interface MediaServerConfig {
  id: string;
  /** "plex" | "jellyfin" | "emby" (typed `str` in the contract). */
  kind: string;
  name: string;
  url: string;
  token: string;
  enabled: boolean;
  path_mappings: PathMapping[];
}

export interface WatchFolderSpec {
  id: number;
  path: string;
  recursive: boolean;
  enabled: boolean;
  settle_seconds: number;
  kind_hint: MediaKind | null;
}

/* ------------------------------------------------------------------ domain.py: file operations */

export interface SidecarMove {
  source: string;
  destination: string;
}

/** A movie's own folder renamed in place ("Alien.1979" -> "Alien (1979)") instead of left behind. */
export interface FolderRename {
  source: string;
  destination: string;
}

export interface FileOpPlan {
  source: string;
  destination: string;
  mode: RenameMode;
  policy: ConflictPolicy;
  sidecars: SidecarMove[];
  conflict: 'none' | 'exists' | 'same_file' | 'duplicate_in_batch' | string;
  incoming_rank: number | null;
  existing_rank: number | null;
}

export interface FileOpResult {
  ok: boolean;
  mode: RenameMode;
  source: string;
  destination: string;
  sidecars: SidecarMove[];
  bytes: number;
  verified: boolean;
  skipped: boolean;
  replaced: string | null;
  error: string | null;
  duration_ms: number;
}

/* ================================================================== api_models.py */

/** GET responses mask secrets with this; PUT with this value keeps the stored secret. */
export const SECRET_MASK = '********';

/* ------------------------------------------------------------------ auth */

export interface AuthStatus {
  setup_required: boolean;
  authenticated: boolean;
  username: string | null;
  auth_enabled: boolean;
  demo: boolean;
  version: string;
  /** First-run setup needs the one-time code printed in the container log. */
  setup_token_required?: boolean;
}

export interface Credentials {
  username: string;
  password: string;
  /** Only for /auth/setup. */
  setup_token?: string;
}

/** POST /auth/password: signs out every other session and re-issues this browser's cookie. */
export interface PasswordChange {
  current_password: string;
  /** At least 8 characters. */
  new_password: string;
}

/* ------------------------------------------------------------------ jobs & items */

export type JobStatus =
  | 'queued'
  | 'matching'
  | 'review'
  | 'ready'
  | 'executing'
  | 'completed'
  | 'failed'
  | 'cancelled';
export const JOB_STATUSES: readonly JobStatus[] = [
  'queued',
  'matching',
  'review',
  'ready',
  'executing',
  'completed',
  'failed',
  'cancelled',
];

export type ItemStatus = 'pending' | 'matched' | 'review' | 'approved' | 'skipped' | 'executing' | 'done' | 'error';
export const ITEM_STATUSES: readonly ItemStatus[] = [
  'pending',
  'matched',
  'review',
  'approved',
  'skipped',
  'executing',
  'done',
  'error',
];

export type JobSource = 'manual' | 'watch' | 'webhook';

export interface JobCreate {
  paths: string[];
  kind?: MediaKind | null;
  mode?: RenameMode | null;
  format_id?: number | null;
  dest_root?: string | null;
  auto_execute?: boolean;
  label?: string | null;
}

export interface JobItemOut {
  id: number;
  job_id: number;
  status: ItemStatus;
  source_path: string;
  filename: string;
  parsed: ParsedMedia;
  candidates: Candidate[];
  selected_candidate_id: string | null;
  confidence: number;
  proposed_path: string | null;
  proposed_relative: string | null;
  path_overridden: boolean;
  final_path: string | null;
  /** Move: the item's folder is (or was) renamed in place rather than left behind. */
  folder_rename?: FolderRename | null;
  sidecars: string[];
  /** null = no CRC in the name or not checked yet; false = mismatch. */
  crc_ok: boolean | null;
  /** mediainfo/ffprobe facts when probing ran. */
  probe: ProbeInfo | null;
  warnings: string[];
  error: string | null;
  updated_at: DateTimeString;
  /** Candidates stored for the item (the slim form only carries the selected one). */
  candidate_count?: number;
  /**
   * Slim form (list pages, `item.updated` pushes): `candidates` holds at most the selected one,
   * without overview/aliases/ids/reasons. GET /items/{id} returns the full item.
   */
  slim?: boolean;
}

export interface JobOut {
  id: number;
  label: string | null;
  source: JobSource;
  status: JobStatus;
  kind: MediaKind | null;
  mode: RenameMode;
  format_id: number | null;
  dest_root: string | null;
  auto_execute: boolean;
  created_at: DateTimeString;
  updated_at: DateTimeString;
  finished_at: DateTimeString | null;
  /** ItemStatus value -> count */
  counts: Partial<Record<ItemStatus, number>>;
  total: number;
  progress: number;
  error: string | null;
}

/** GET /jobs/{id}: the job plus one page of items (`limit=0` for the header only). */
export interface JobDetail extends JobOut {
  items: JobItemOut[];
  /** Items matching the item filter; `items.length < items_total` means the page is partial. */
  items_total?: number;
  next_after?: number | null;
}

/** GET /jobs/{id}/items: one page of slim items ordered by id. */
export interface JobItemPage {
  job_id: number;
  items: JobItemOut[];
  /** Items matching the filter. */
  total: number;
  offset: number;
  limit: number;
  /** Cursor (`after`) for the next page; null = last page. */
  next_after: number | null;
  /** ItemStatus value -> count for the whole job. */
  counts: Partial<Record<ItemStatus, number>>;
}

export interface JobItemFilter {
  /** null/empty = every status. */
  statuses?: ItemStatus[] | null;
  q?: string | null;
}

export interface ItemUpdate {
  selected_candidate_id?: string | null;
  status?: 'approved' | 'skipped' | 'review' | null;
  proposed_path?: string | null;
  reset_path?: boolean;
}

export interface ItemRematch {
  hint: MatchHint;
}

export interface ItemSearch {
  query: string;
  kind: MediaKind;
  year?: number | null;
  provider?: string | null;
}

export type BulkAction = 'approve' | 'skip' | 'review' | 'rematch';

/** Either explicit `item_ids`, or every item of `job_id` matching `statuses`/`q` (server-side). */
export interface BulkItemAction {
  item_ids?: number[] | null;
  action: BulkAction;
  job_id?: number | null;
  statuses?: ItemStatus[] | null;
  q?: string | null;
}

export interface ExecuteRequest {
  item_ids?: number[] | null;
  mode?: RenameMode | null;
  /** Without item_ids: only matched/approved items whose name, path or title contains this. */
  q?: string | null;
}

export interface PreviewRequest {
  names: string[];
  kind?: MediaKind | null;
  format_id?: number | null;
  expression?: string | null;
}

export interface PreviewItem {
  name: string;
  parsed: ParsedMedia;
  best: Candidate | null;
  confidence: number;
  rendered: string | null;
  error: string | null;
}

/* ------------------------------------------------------------------ formats */

export interface FormatIn {
  name: string;
  kind: MediaKind;
  expression: string;
  is_default?: boolean;
}

export interface FormatOut {
  name: string;
  kind: MediaKind;
  expression: string;
  is_default: boolean;
  id: number;
  builtin: boolean;
  created_at: DateTimeString;
  updated_at: DateTimeString;
}

export type IssueSeverity = 'error' | 'warning';

export interface FormatIssue {
  message: string;
  start: number;
  end: number;
  severity: IssueSeverity;
}

export interface FormatSample {
  source: string;
  output: string | null;
  error: string | null;
}

export interface FormatValidateRequest {
  expression: string;
  kind: MediaKind;
  samples?: string[] | null;
}

export interface FormatValidateResponse {
  ok: boolean;
  issues: FormatIssue[];
  samples: FormatSample[];
}

export interface BindingSpec {
  name: string;
  description: string;
  kinds: MediaKind[];
  group: string;
  example: string;
  /** str | int | date | list | object | bool */
  type: string;
}

export interface FilterSpec {
  name: string;
  signature: string;
  description: string;
  example: string;
}

export interface FormatPreset {
  id: string;
  name: string;
  kind: MediaKind;
  expression: string;
  description: string;
}

export interface FormatCatalog {
  bindings: BindingSpec[];
  filters: FilterSpec[];
  presets: FormatPreset[];
}

/* ------------------------------------------------------------------ operations (history) */

export type OperationStatus = 'done' | 'failed' | 'undone' | 'undo_failed';

export interface OperationOut {
  id: number;
  job_id: number | null;
  item_id: number | null;
  mode: RenameMode;
  source: string;
  destination: string;
  status: OperationStatus;
  bytes: number;
  sidecars: SidecarMove[];
  /** The source folder was renamed in place. */
  folder_rename?: FolderRename | null;
  /** Empty source folders removed after the move. */
  removed_dirs?: string[];
  error: string | null;
  created_at: DateTimeString;
  undone_at: DateTimeString | null;
}

/* ------------------------------------------------------------------ watch folders */

export interface WatchFolderIn {
  path: string;
  enabled?: boolean;
  recursive?: boolean;
  kind?: MediaKind | null;
  auto_execute?: boolean;
  settle_seconds?: number;
  format_id?: number | null;
  dest_root?: string | null;
  mode?: RenameMode | null;
}

export type WatchStatus = 'watching' | 'stopped' | 'error' | 'missing';

export interface WatchFolderOut {
  path: string;
  enabled: boolean;
  recursive: boolean;
  kind: MediaKind | null;
  auto_execute: boolean;
  settle_seconds: number;
  format_id: number | null;
  dest_root: string | null;
  mode: RenameMode | null;
  id: number;
  status: WatchStatus;
  last_event_at: DateTimeString | null;
  last_error: string | null;
  files_seen: number;
}

/* ------------------------------------------------------------------ settings & providers */

export interface AppSettings {
  library_roots: Partial<Record<MediaKind, string>>;
  default_mode: RenameMode;
  conflict_policy: ConflictPolicy;
  min_confidence: number;
  verify_crc: boolean;
  probe_media: boolean;
  move_sidecars: boolean;
  /** Move: rename the movie's existing folder instead of creating a new one. */
  rename_folders_in_place: boolean;
  /** Move: delete source folders left empty (never a library, watch or allowed root). */
  remove_empty_source_folders: boolean;
  /** Move: once a download folder holds nothing Schema still needs, move it (with its .nfo, .jpg,
   * samples and extras) to a holding folder; it is deleted a day later. */
  clean_release_folders: boolean;
  /** Watch folders: wait until a whole download folder has been unchanged this many seconds (0 = off). */
  release_quiet_seconds: number;
  sidecar_extensions: string[];
  video_extensions: string[];
  ignore_patterns: string[];
  skip_samples: boolean;
  skip_extras: boolean;
  allowed_roots: string[];
  language: string;
  media_servers: MediaServerConfig[];
  category_kinds: Record<string, MediaKind>;
  path_mappings: PathMapping[];
  webhook_api_key: string;
  /** "Clear solved problems from Home": 0 = right away, 24 = 1 day, 168 = 7 days, null = never. */
  clear_solved_after_hours: number | null;
}

export interface ProvidersOut {
  statuses: ProviderStatus[];
  priority: Partial<Record<MediaKind, string[]>>;
  disabled: string[];
  language: string;
  /** ProviderCredentials field -> masked value (or null if unset) */
  credentials: Record<string, string | null>;
}

export interface ProvidersIn {
  priority?: Partial<Record<MediaKind, string[]>> | null;
  disabled?: string[] | null;
  language?: string | null;
  credentials?: Record<string, string | null> | null;
}

export interface MediaServerTest {
  ok: boolean;
  message: string;
}

/* ------------------------------------------------------------------ dashboard, events, fs */

export type EventLevel = 'debug' | 'info' | 'warning' | 'error';
export const EVENT_LEVELS: readonly EventLevel[] = ['debug', 'info', 'warning', 'error'];
export const EVENT_CATEGORIES = ['job', 'watcher', 'provider', 'fileops', 'webhook', 'mediaserver', 'system'] as const;

export interface EventOut {
  id: number;
  ts: DateTimeString;
  level: EventLevel;
  /** "job" "watcher" "provider" "fileops" "webhook" "mediaserver" "system" */
  category: string;
  message: string;
  job_id: number | null;
  data: Record<string, unknown>;
}

export interface DashboardTotals {
  files_renamed_today: number;
  files_renamed_total: number;
  bytes_moved_total: number;
  items_pending_review: number;
}

export interface DashboardOut {
  /** JobStatus value -> count */
  queue: Partial<Record<JobStatus, number>>;
  totals: DashboardTotals;
  active_jobs: JobOut[];
  recent_operations: OperationOut[];
  recent_errors: EventOut[];
  watchers: WatchFolderOut[];
  providers: ProviderStatus[];
}

export interface FsEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number | null;
  modified: DateTimeString | null;
  is_video: boolean;
}

export interface FsListing {
  path: string;
  parent: string | null;
  entries: FsEntry[];
  roots: string[];
}

export interface WebhookAccepted {
  job_id: number;
  paths: string[];
}

/* ------------------------------------------------------------------ untyped route bodies (SPEC §5) */

export interface HealthOut {
  status: string;
  version: string;
  /** Git commit the running image was built from; null for local / development builds. */
  commit?: string | null;
}

export interface ApiKeyOut {
  api_key: string;
}

export interface ParseRequest {
  names: string[];
}

/* ------------------------------------------------------------------ home (flow board) */

/** partial = still downloading (.part/.!qB …); settling = waiting out the watch folder's settle_seconds. */
export type IncomingState = 'settling' | 'partial';

export interface IncomingFile {
  folder_id: number;
  path: string;
  name: string;
  size: number;
  state: IncomingState;
  settle_seconds: number;
  /** When size/mtime last changed (null for partial files). */
  stable_since: DateTimeString | null;
  /** stable_since + settle_seconds (null for partial files). */
  settles_at: DateTimeString | null;
  /** Parsed title + year, e.g. "Never After Dark (2026)". */
  title_guess: string | null;
}

export type HomeAction = 'skip' | 'approve' | 'retry' | 'dismiss';

export type NeedsYouReason =
  | 'low_confidence'
  | 'conflict'
  | 'extra'
  | 'sample'
  | 'no_match'
  | 'crc_mismatch'
  | 'error'
  | 'interrupted'
  | 'event'
  | 'source_missing'
  | 'episode_mismatch';

export interface NeedsYouCard {
  /** "item:123" | "event:45" | "group:7" */
  key: string;
  /** `group`: one card for many files of one job (their sources vanished before the rename). */
  kind: 'item' | 'event' | 'group';
  item_id: number | null;
  job_id: number | null;
  event_id: number | null;
  /** Group cards: how many files the card stands for. */
  count?: number | null;
  /** File name for items; a short message for events; a summary for groups. */
  title: string;
  /** Plain English, e.g. "Looks like a bonus clip. Blade Runner (1982) is already in your library." */
  reason: string;
  reason_code: NeedsYouReason;
  primary: { label: string; action: HomeAction } | null;
  candidate_title: string | null;
  confidence: number | null;
  ts: DateTimeString;
}

export interface RenamingCard {
  item_id: number;
  job_id: number;
  title: string;
  filename: string;
  status: 'matching' | 'matched' | 'approved' | 'executing';
  job_progress: number | null;
}

export interface LibraryCard {
  operation_id: number;
  title: string;
  kind: MediaKind;
  destination: string;
  ts: DateTimeString;
  bytes: number;
  can_undo: boolean;
  /** Series episodes: the matched show ("Starfall Station (2016)") and its numbers; null otherwise. */
  show?: string | null;
  season?: number | null;
  episode?: number | null;
}

export interface HomeWatchStatus {
  id: number;
  path: string;
  status: WatchStatus;
  enabled: boolean;
}

export interface HomeMediaServer {
  id: string;
  kind: MediaServerKind;
  name: string;
  url: string;
  enabled: boolean;
}

/** GET /home */
export interface HomeOut {
  needs_you_count: number;
  incoming: IncomingFile[];
  /** Newest first, at most 50. */
  needs_you: NeedsYouCard[];
  renaming: RenamingCard[];
  library: { recent: LibraryCard[]; total_files: number; total_bytes: number };
  status: {
    watch: HomeWatchStatus[];
    providers: ProviderStatus[];
    media_servers: HomeMediaServer[];
  };
  /** Lets the client correct clock skew for countdowns. */
  server_time: DateTimeString;
}

/** POST /home/actions (404 unknown key, 409 when the action does not apply). */
export interface HomeActionRequest {
  key: string;
  action: HomeAction;
}

/** POST /home/dismiss-all: every review card is dismissed. */
export interface HomeClearedResult {
  cleared: number;
}

/**
 * POST /home/refresh: re-checks the disk; review cards whose source file is gone are dismissed, and
 * finished files waiting in the watch (download) folders start now (`started`).
 */
export interface HomeRefreshResult extends HomeClearedResult {
  started: number;
}

/** POST /watch-folders/{id}/start-now: skips the settle wait (every settling file without `path`). */
export interface StartNowRequest {
  path?: string;
}

export interface StartNowResult {
  started: number;
}

/* ------------------------------------------------------------------ history */

export type HistoryFilter = 'all' | 'moved' | 'skipped' | 'problems';
export type HistoryEntryType = 'moved' | 'skipped' | 'problem' | 'undone';

export interface HistoryEntry {
  key: string;
  type: HistoryEntryType;
  ts: DateTimeString;
  title: string;
  kind: MediaKind;
  source: string;
  destination: string | null;
  /** e.g. "Matched Blade Runner (1982)" / "A file with that name already exists" */
  detail: string | null;
  bytes: number;
  /** Rename mode for moved entries. */
  mode: string | null;
  operation_id: number | null;
  item_id: number | null;
  job_id: number | null;
  can_undo: boolean;
  can_fix: boolean;
}

/** GET /history: newest first; pass `next_before` back as `before` for older entries. */
export interface HistoryPage {
  entries: HistoryEntry[];
  next_before: string | null;
}

/* ------------------------------------------------------------------ WebSocket */

export type WsMessage =
  | { type: 'hello'; data: { version: string } }
  | { type: 'job.updated'; data: JobOut }
  | { type: 'job.deleted'; data: { id: number } }
  | { type: 'item.updated'; data: JobItemOut }
  // One message per save / bulk action / rematch instead of one `item.updated` per item (newer
  // servers). `item_ids` is left out for large batches: refetch the job's items.
  | { type: 'job.items_changed'; data: JobItemsChanged }
  | { type: 'operation.created'; data: OperationOut }
  | { type: 'operation.updated'; data: OperationOut }
  | { type: 'event'; data: EventOut }
  | { type: 'watcher.updated'; data: WatchFolderOut }
  | { type: 'provider.updated'; data: ProviderStatus }
  // The server dropped messages for this socket (burst or slow client): refetch what is on screen.
  | { type: 'resync'; data: Record<string, never> };

export type WsMessageType = WsMessage['type'];

/** Data of `job.items_changed`. */
export interface JobItemsChanged {
  job_id: number;
  /** The items that changed; omitted when the batch is large ("refetch this job's items"). */
  item_ids?: number[] | null;
}
