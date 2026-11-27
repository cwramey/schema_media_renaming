/** Plain-English labels for Home's footer status strip and column captions (pure, unit tested). */
import type { HomeOut, LibraryCard, MediaKind, NeedsYouCard, HomeAction } from '@/api/types';
import type { Tone } from '@/lib/labels';
import { formatBytes, formatNumber } from '@/lib/format';

export interface StatusChip {
  key: string;
  tone: Tone;
  label: string;
}

const PROVIDER_SHORT: Record<string, string> = {
  tmdb: 'TMDb',
  tvdb: 'TheTVDB',
  anidb: 'AniDB',
  trakt: 'Trakt',
  tvmaze: 'TVmaze',
};

/** Always shown; the others only appear when they are set up and failing (never "needs key" noise). */
const PRIMARY_PROVIDER = 'tmdb';

export function statusChips(status: HomeOut['status']): StatusChip[] {
  const chips: StatusChip[] = [];

  if (status.watch.length === 0) chips.push({ key: 'watch', tone: 'neutral', label: 'No watch folder' });
  for (const w of status.watch) {
    const key = `watch:${w.id}`;
    if (!w.enabled) chips.push({ key, tone: 'neutral', label: `Paused ${w.path}` });
    else if (w.status === 'watching') chips.push({ key, tone: 'success', label: `Watching ${w.path}` });
    else if (w.status === 'missing') chips.push({ key, tone: 'warning', label: `${w.path} is missing` });
    else if (w.status === 'error') chips.push({ key, tone: 'danger', label: `Can't watch ${w.path}` });
    else chips.push({ key, tone: 'neutral', label: `Not watching ${w.path}` });
  }

  for (const p of status.providers) {
    const name = PROVIDER_SHORT[p.name] ?? p.label;
    const key = `provider:${p.name}`;
    if (p.name === PRIMARY_PROVIDER) {
      if (!p.configured) chips.push({ key, tone: 'neutral', label: `${name} not set up` });
      else if (!p.enabled) chips.push({ key, tone: 'neutral', label: `${name} turned off` });
      else if (p.healthy === false) chips.push({ key, tone: 'danger', label: `${name} can't connect` });
      else chips.push({ key, tone: 'success', label: `${name} connected` });
    } else if (p.configured && p.enabled && p.healthy === false) {
      chips.push({ key, tone: 'danger', label: `${name} can't connect` });
    }
  }

  for (const m of status.media_servers) {
    chips.push({
      key: `media:${m.id}`,
      tone: m.enabled ? 'success' : 'neutral',
      label: m.enabled ? `${m.name} connected` : `${m.name} turned off`,
    });
  }
  return chips;
}

/** "Done · Jellyfin was told" (enabled media servers get a library refresh after each move). */
export function libraryCaption(servers: HomeOut['status']['media_servers']): string {
  const names = servers.filter((m) => m.enabled).map((m) => m.name);
  if (names.length === 0) return 'Done';
  const list = names.length === 1 ? names[0] : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
  return `Done · ${list} ${names.length === 1 ? 'was' : 'were'} told`;
}

/** "156 files · 2.7 TiB organized" */
export function totalsLabel(files: number, bytes: number): string {
  return `${formatNumber(files)} file${files === 1 ? '' : 's'} · ${formatBytes(bytes)} organized`;
}

/** "3 files need review" / "1 file needs review" / "All caught up" */
export function needsYouHeading(count: number): string {
  if (count <= 0) return 'All caught up';
  return count === 1 ? '1 file needs review' : `${formatNumber(count)} files need review`;
}

/** The card's one-click action; event cards always offer Dismiss. */
export function primaryAction(card: Pick<NeedsYouCard, 'kind' | 'primary'>): { label: string; action: HomeAction } | null {
  if (card.primary) return card.primary;
  return card.kind === 'event' ? { label: 'Dismiss', action: 'dismiss' } : null;
}

export const ACTION_DONE: Record<HomeAction, string> = {
  skip: 'Skipped',
  approve: 'Approved: renaming now',
  retry: 'Trying again',
  dismiss: 'Dismissed',
};

export const SHORT_KIND: Record<MediaKind, string> = {
  movie: 'Movie',
  series: 'TV',
  anime: 'Anime',
  unknown: 'File',
};

/* ------------------------------------------------------------------ library rows */

export type LibraryRow =
  | { type: 'single'; card: LibraryCard }
  | { type: 'group'; key: string; show: string; season: number; cards: LibraryCard[] };

const isEpisode = (c: LibraryCard): c is LibraryCard & { show: string; season: number; episode: number } =>
  !!c.show && typeof c.season === 'number' && typeof c.episode === 'number';

/** Folds each run of episodes of one show and season (cards newest first) into one row; at most `max` rows. */
export function libraryRows(cards: LibraryCard[], max = 6): LibraryRow[] {
  const rows: LibraryRow[] = [];
  for (const card of cards) {
    const last = rows[rows.length - 1];
    if (isEpisode(card) && last) {
      if (last.type === 'group' && last.show === card.show && last.season === card.season) {
        last.cards.push(card);
        continue;
      }
      if (last.type === 'single' && isEpisode(last.card) && last.card.show === card.show && last.card.season === card.season) {
        rows[rows.length - 1] = {
          type: 'group',
          key: `group:${last.card.operation_id}`,
          show: card.show,
          season: card.season,
          cards: [last.card, card],
        };
        continue;
      }
    }
    if (rows.length >= max) break;
    rows.push({ type: 'single', card });
  }
  return rows;
}

const pad = (n: number) => String(n).padStart(2, '0');

/** "S04E12" */
export function episodeCode(season: number, episode: number): string {
  return `S${pad(season)}E${pad(episode)}`;
}

/** "S04E10–E12" for a run without gaps, otherwise "Season 4". */
export function episodeRange(season: number, cards: LibraryCard[]): string {
  const eps = cards.map((c) => c.episode ?? 0).sort((a, b) => a - b);
  const first = eps[0];
  const last = eps[eps.length - 1];
  const unbroken = new Set(eps).size === eps.length && last - first === eps.length - 1;
  return unbroken ? `${episodeCode(season, first)}–E${pad(last)}` : `Season ${season}`;
}

/** The folder a file was moved into ("/media/tv/Show (2013)/Season 04"). */
export function parentFolder(path: string): string {
  const cut = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
  return cut > 0 ? path.slice(0, cut) : path;
}
