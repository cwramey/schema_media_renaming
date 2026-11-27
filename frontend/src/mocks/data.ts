/**
 * Fictional demo data (mirrors backend/schema_app/demo/demo_files.txt) for `npm run dev:mock`.
 * Dev-only: never imported by production code paths.
 */
import type {
  AppSettings,
  BindingSpec,
  Candidate,
  EventOut,
  FilterSpec,
  FormatOut,
  FormatPreset,
  ItemStatus,
  JobDetail,
  JobItemOut,
  MediaKind,
  OperationOut,
  ParsedMedia,
  ProviderStatus,
  QualityInfo,
  WatchFolderOut,
} from '@/api/types';

const NOW = Date.now();
export const iso = (minutesAgo: number) => new Date(NOW - minutesAgo * 60_000).toISOString();

const Q = (q: Partial<QualityInfo> = {}): QualityInfo => ({
  resolution: null,
  source: null,
  remux: false,
  video_codec: null,
  bit_depth: null,
  hdr: [],
  audio_codec: null,
  audio_channels: null,
  atmos: false,
  streaming_service: null,
  proper: false,
  repack: false,
  s3d: null,
  ...q,
});

function parsed(path: string, p: Partial<ParsedMedia>): ParsedMedia {
  const filename = path.split('/').pop() ?? path;
  return {
    path,
    filename,
    extension: filename.split('.').pop()?.toLowerCase() ?? '',
    kind: 'unknown',
    title: null,
    alt_titles: [],
    year: null,
    season: null,
    episodes: [],
    absolute: [],
    air_date: null,
    special: false,
    special_title: null,
    part: null,
    edition: null,
    release_group: null,
    crc32: null,
    version: null,
    quality: Q(),
    languages: [],
    is_sample: false,
    is_extra: false,
    folder_title: null,
    confidence: 0.9,
    tokens: [],
    ...p,
  };
}

function series(
  provider: string,
  id: string,
  title: string,
  year: number,
  score: number,
  eps: { s: number; e: number; t: string; abs?: number }[],
  kind: MediaKind = 'series',
  reasons: string[] = [],
): Candidate {
  const ref = { provider, id };
  return {
    id: `${provider}:${kind}:${id}`,
    kind,
    provider,
    title,
    year,
    score,
    movie: null,
    series: {
      ref,
      title,
      original_title: null,
      year,
      first_aired: `${year}-03-01`,
      aliases: [],
      overview: null,
      poster_url: null,
      ids: { tvdb: id },
      popularity: 12,
      status: 'Continuing',
      network: kind === 'anime' ? 'Tokyo MX' : 'Northlight',
      genres: [],
      is_anime: kind === 'anime',
      season_count: 3,
      certification: null,
      rating: 8.1,
      original_language: 'en',
    },
    episodes: eps.map((x) => ({
      series_ref: ref,
      season: x.s,
      episode: x.e,
      absolute: x.abs ?? null,
      title: x.t,
      air_date: null,
      ids: {},
    })),
    episode_order: 'aired',
    reasons: reasons.length ? reasons : [`title similarity ${Math.round(score * 100)}%`, 'episode exists +0.15'],
  };
}

function movie(id: string, title: string, year: number, score: number, reasons: string[] = []): Candidate {
  const ref = { provider: 'tmdb', id };
  return {
    id: `tmdb:movie:${id}`,
    kind: 'movie',
    provider: 'tmdb',
    title,
    year,
    score,
    movie: {
      ref,
      title,
      original_title: null,
      year,
      release_date: `${year}-05-01`,
      aliases: [],
      overview: null,
      poster_url: null,
      ids: { tmdb: id },
      popularity: 8,
      runtime_minutes: 112,
      genres: [],
      collection: null,
      certification: null,
      rating: 7.2,
      original_language: 'en',
    },
    series: null,
    episodes: [],
    episode_order: null,
    reasons: reasons.length ? reasons : ['title similarity 100%', 'year exact +0.20'],
  };
}

const IN = '/storage/demo/incoming/';
const TV = '/storage/demo/library/TV';
const MOV = '/storage/demo/library/Movies';
const ANI = '/storage/demo/library/Anime';

let nextItemId = 100;
function item(
  rel: string,
  p: Partial<ParsedMedia>,
  opts: {
    status: ItemStatus;
    candidates?: Candidate[];
    relOut?: string | null;
    root?: string;
    confidence?: number;
    warnings?: string[];
    sidecars?: string[];
    error?: string | null;
  },
): JobItemOut {
  const path = IN + rel;
  const cands = opts.candidates ?? [];
  return {
    id: nextItemId++,
    job_id: 1,
    status: opts.status,
    source_path: path,
    filename: path.split('/').pop() ?? path,
    parsed: parsed(path, p),
    candidates: cands,
    selected_candidate_id: cands[0]?.id ?? null,
    confidence: opts.confidence ?? cands[0]?.score ?? 0,
    proposed_relative: opts.relOut ?? null,
    proposed_path: opts.relOut ? `${opts.root ?? TV}/${opts.relOut}` : null,
    path_overridden: false,
    final_path: null,
    sidecars: opts.sidecars ?? [],
    crc_ok: (opts.warnings ?? []).includes('crc_mismatch') ? false : p.crc32 ? true : null,
    probe: null,
    warnings: opts.warnings ?? [],
    error: opts.error ?? null,
    updated_at: iso(3),
  };
}

const starfall = (eps: { s: number; e: number; t: string }[], score = 0.97) =>
  series('fixtures', '900001', 'Starfall Station', 2016, score, eps);

export function buildItems(): JobItemOut[] {
  nextItemId = 100;
  const items: JobItemOut[] = [
    item('Starfall.Station.S01E01.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv', {
      kind: 'series', title: 'Starfall Station', season: 1, episodes: [1], release_group: 'NTb',
      quality: Q({ resolution: '1080p', source: 'WEB-DL', video_codec: 'x264', audio_codec: 'DDP', audio_channels: '5.1' }),
    }, {
      status: 'matched',
      candidates: [starfall([{ s: 1, e: 1, t: 'Arrival' }]), series('tvmaze', '4410', 'Starfall', 1998, 0.61, [{ s: 1, e: 1, t: 'Pilot' }])],
      relOut: 'Starfall Station (2016)/Season 01/Starfall Station - S01E01 - Arrival.mkv',
      sidecars: [IN + 'Starfall.Station.S01E01.1080p.WEB-DL.DDP5.1.H.264-NTb.en.srt'],
    }),
    item('Starfall.Station.S01E02E03.720p.HDTV.x264-KILLERS.mkv', {
      kind: 'series', title: 'Starfall Station', season: 1, episodes: [2, 3], release_group: 'KILLERS',
      quality: Q({ resolution: '720p', source: 'HDTV', video_codec: 'x264' }),
    }, {
      status: 'matched',
      candidates: [starfall([{ s: 1, e: 2, t: 'Dead Reckoning' }, { s: 1, e: 3, t: 'Signal Fire' }])],
      relOut: 'Starfall Station (2016)/Season 01/Starfall Station - S01E02-E03 - Dead Reckoning & Signal Fire.mkv',
    }),
    item('starfall_station_1x04_hdtv.avi', {
      kind: 'series', title: 'starfall station', season: 1, episodes: [4], quality: Q({ source: 'HDTV' }),
    }, {
      status: 'approved',
      candidates: [starfall([{ s: 1, e: 4, t: 'The Long Orbit' }], 0.93)],
      relOut: 'Starfall Station (2016)/Season 01/Starfall Station - S01E04 - The Long Orbit.avi',
    }),
    item('Starfall Station - S00E02 - Christmas Special.mkv', {
      kind: 'series', title: 'Starfall Station', season: 0, episodes: [2], special: true,
    }, {
      status: 'matched',
      candidates: [starfall([{ s: 0, e: 2, t: 'Christmas Special' }])],
      relOut: 'Starfall Station (2016)/Specials/Starfall Station - S00E02 - Christmas Special.mkv',
    }),
    item('Starfall Station - The Making of Starfall.mkv', {
      kind: 'series', title: 'Starfall Station', special: true, special_title: 'The Making of Starfall', confidence: 0.6,
    }, {
      status: 'review',
      confidence: 0.81,
      candidates: [starfall([{ s: 0, e: 1, t: 'The Making of Starfall' }], 0.81)],
      relOut: 'Starfall Station (2016)/Specials/Starfall Station - S00E01 - The Making of Starfall.mkv',
      warnings: ['low_confidence'],
    }),
    item('Starfall.Station.S02E10.PROPER.1080p.BluRay.x265.10bit-GROUP.mkv', {
      kind: 'series', title: 'Starfall Station', season: 2, episodes: [10], release_group: 'GROUP',
      quality: Q({ resolution: '1080p', source: 'BluRay', video_codec: 'x265', bit_depth: 10, proper: true }),
    }, {
      status: 'matched',
      candidates: [starfall([{ s: 2, e: 10, t: 'Event Horizon' }])],
      relOut: 'Starfall Station (2016)/Season 02/Starfall Station - S02E10 - Event Horizon.mkv',
    }),
    item('Starfall.Station.S03E01.Sample.mkv', {
      kind: 'series', title: 'Starfall Station', season: 3, episodes: [1], is_sample: true,
    }, { status: 'skipped', warnings: ['sample'] }),
    item('Starfall Station/Season 3/03 - Ghosts in the Array.mkv', {
      kind: 'series', title: 'Starfall Station', season: 3, episodes: [3], folder_title: 'Starfall Station',
    }, {
      status: 'matched',
      candidates: [starfall([{ s: 3, e: 3, t: 'Ghosts in the Array' }])],
      relOut: 'Starfall Station (2016)/Season 03/Starfall Station - S03E03 - Ghosts in the Array.mkv',
    }),
    item('Harbor.Lights.2021.S01E03.2160p.AMZN.WEB-DL.DV.HDR.H.265-FLUX.mkv', {
      kind: 'series', title: 'Harbor Lights', year: 2021, season: 1, episodes: [3], release_group: 'FLUX',
      quality: Q({ resolution: '2160p', source: 'WEB-DL', video_codec: 'x265', hdr: ['DV', 'HDR10'], streaming_service: 'AMZN' }),
    }, {
      status: 'matched',
      candidates: [
        series('fixtures', '900021', 'Harbor Lights', 2021, 0.98, [{ s: 1, e: 3, t: 'Low Tide' }]),
        series('fixtures', '900020', 'Harbor Lights', 2008, 0.71, [{ s: 1, e: 3, t: 'The Keeper' }]),
      ],
      relOut: 'Harbor Lights (2021)/Season 01/Harbor Lights - S01E03 - Low Tide.mkv',
    }),
    item('Harbor Lights S02E05.mkv', { kind: 'series', title: 'Harbor Lights', season: 2, episodes: [5] }, {
      status: 'review',
      confidence: 0.78,
      candidates: [
        series('fixtures', '900020', 'Harbor Lights', 2008, 0.83, [{ s: 2, e: 5, t: 'Undertow' }], 'series', [
          'title similarity 100%', 'only this series has a season 2', 'ambiguity −0.05',
        ]),
        series('fixtures', '900021', 'Harbor Lights', 2021, 0.79, [], 'series', ['title similarity 100%', 'season 2 does not exist −0.20']),
      ],
      relOut: 'Harbor Lights (2008)/Season 02/Harbor Lights - S02E05 - Undertow.mkv',
      warnings: ['ambiguous'],
    }),
    item('The.Late.Hour.with.Sam.Reyes.2024.03.04.720p.WEB.h264-SPAMnEGGS.mkv', {
      kind: 'series', title: 'The Late Hour with Sam Reyes', air_date: '2024-03-04', release_group: 'SPAMnEGGS',
      quality: Q({ resolution: '720p', source: 'WEB-DL', video_codec: 'x264' }),
    }, {
      status: 'matched',
      candidates: [series('fixtures', '900030', 'The Late Hour with Sam Reyes', 2019, 0.95, [{ s: 2024, e: 4, t: 'Guest: Mara Lind' }])],
      relOut: 'The Late Hour with Sam Reyes (2019)/Season 2024/The Late Hour with Sam Reyes - S2024E04 - Guest - Mara Lind.mkv',
    }),
    item('[SubsPlease] Kaze no Tsubasa - 15 (1080p) [5B1C2D3E].mkv', {
      kind: 'anime', title: 'Kaze no Tsubasa', absolute: [15], release_group: 'SubsPlease', crc32: '5B1C2D3E',
      quality: Q({ resolution: '1080p' }),
    }, {
      status: 'matched',
      root: ANI,
      candidates: [series('anidb', '90001', 'Kaze no Tsubasa', 2018, 0.96, [{ s: 2, e: 3, t: 'Crosswind', abs: 15 }], 'anime')],
      relOut: 'Kaze no Tsubasa/Season 02/Kaze no Tsubasa - S02E03 - Crosswind.mkv',
    }),
    item('[Erai-raws] Wings of the Wind - 26 [1080p][Multiple Subtitle].mkv', {
      kind: 'anime', title: 'Wings of the Wind', absolute: [26], release_group: 'Erai-raws', quality: Q({ resolution: '1080p' }),
    }, {
      status: 'matched',
      root: ANI,
      candidates: [series('anidb', '90001', 'Kaze no Tsubasa', 2018, 0.9, [{ s: 3, e: 1, t: 'New Currents', abs: 26 }], 'anime', [
        'alias “Wings of the Wind” 100%', 'absolute 26 → S03E01 via Anime-Lists',
      ])],
      relOut: 'Kaze no Tsubasa/Season 03/Kaze no Tsubasa - S03E01 - New Currents.mkv',
    }),
    item('[Hatsuyuki] Kaze no Tsubasa - 07v2 [720p][DEADBEEF].mkv', {
      kind: 'anime', title: 'Kaze no Tsubasa', absolute: [7], version: 2, crc32: 'DEADBEEF', release_group: 'Hatsuyuki',
      quality: Q({ resolution: '720p' }),
    }, {
      status: 'review',
      root: ANI,
      candidates: [series('anidb', '90001', 'Kaze no Tsubasa', 2018, 0.95, [{ s: 1, e: 7, t: 'Paper Kites', abs: 7 }], 'anime')],
      relOut: 'Kaze no Tsubasa/Season 01/Kaze no Tsubasa - S01E07 - Paper Kites.mkv',
      warnings: ['crc_mismatch', 'format: block {edition} empty (missing binding edition)'],
    }),
    item('[Judas] Tensei Robo Gaiden - S01E07 [1080p][HEVC x265 10bit].mkv', {
      kind: 'anime', title: 'Tensei Robo Gaiden', season: 1, episodes: [7], release_group: 'Judas',
      quality: Q({ resolution: '1080p', video_codec: 'x265', bit_depth: 10 }),
    }, {
      status: 'done',
      root: ANI,
      candidates: [series('anidb', '90002', 'Tensei Robo Gaiden', 2020, 0.97, [{ s: 1, e: 7, t: 'Iron Saint' }], 'anime')],
      relOut: 'Tensei Robo Gaiden/Season 01/Tensei Robo Gaiden - S01E07 - Iron Saint.mkv',
    }),
    item('The.Quiet.Meridian.2019.Directors.Cut.1080p.BluRay.x264-SPARKS.mkv', {
      kind: 'movie', title: 'The Quiet Meridian', year: 2019, edition: "Director's Cut", release_group: 'SPARKS',
      quality: Q({ resolution: '1080p', source: 'BluRay', video_codec: 'x264' }),
    }, {
      status: 'matched',
      root: MOV,
      candidates: [movie('700001', 'The Quiet Meridian', 2019, 0.99), movie('700002', 'The Quiet Meridian Returns', 2022, 0.58)],
      relOut: "The Quiet Meridian (2019)/The Quiet Meridian (2019) - Director's Cut.mkv",
    }),
    item('Iron.Orchard.1987.REMASTERED.720p.BluRay.x264-AMIABLE.mkv', {
      kind: 'movie', title: 'Iron Orchard', year: 1987, edition: 'Remastered', release_group: 'AMIABLE',
      quality: Q({ resolution: '720p', source: 'BluRay', video_codec: 'x264' }),
    }, {
      status: 'matched',
      root: MOV,
      candidates: [movie('700003', 'Iron Orchard', 1987, 0.98), movie('700004', 'Iron Orchard', 2023, 0.52)],
      relOut: 'Iron Orchard (1987)/Iron Orchard (1987) - Remastered.mkv',
    }),
    item('Paper.Moons.2004.CD1.DVDRip.XviD-FiCO.avi', {
      kind: 'movie', title: 'Paper Moons', year: 2004, part: 1, release_group: 'FiCO', quality: Q({ source: 'DVD', video_codec: 'XviD' }),
    }, {
      status: 'error',
      root: MOV,
      candidates: [movie('700005', 'Paper Moons', 2004, 0.97)],
      relOut: 'Paper Moons (2004)/Paper Moons (2004) - pt1.avi',
      error: 'Destination is outside the allowed roots',
    }),
    item('Neon.Requiem.2021.2160p.UHD.BluRay.REMUX.HDR.HEVC.TrueHD.7.1.Atmos-FGT.mkv', {
      kind: 'movie', title: 'Neon Requiem', year: 2021, release_group: 'FGT',
      quality: Q({ resolution: '2160p', source: 'BluRay', remux: true, video_codec: 'x265', hdr: ['HDR10'], audio_codec: 'TrueHD', audio_channels: '7.1', atmos: true }),
    }, {
      status: 'matched',
      root: MOV,
      candidates: [movie('700006', 'Neon Requiem', 2021, 0.99)],
      relOut: 'Neon Requiem (2021)/Neon Requiem (2021) - 2160p Remux HDR.mkv',
      warnings: ['duplicate_in_batch'],
    }),
    item('completely.unknown.title.2011.1080p.mkv', {
      kind: 'movie', title: 'completely unknown title', year: 2011, quality: Q({ resolution: '1080p' }), confidence: 0.4,
    }, { status: 'review', warnings: ['no_match'] }),
  ];
  // Pad with synthetic episodes so virtualization is exercised.
  for (let e = 1; e <= 40; e++) {
    const ep = String(e).padStart(2, '0');
    items.push(
      item(`Starfall.Station.S02E${ep}.1080p.WEB-DL.x264-NTb.mkv`, {
        kind: 'series', title: 'Starfall Station', season: 2, episodes: [e], release_group: 'NTb',
        quality: Q({ resolution: '1080p', source: 'WEB-DL', video_codec: 'x264' }),
      }, {
        status: e % 9 === 0 ? 'review' : 'matched',
        confidence: e % 9 === 0 ? 0.72 : 0.96,
        candidates: [starfall([{ s: 2, e, t: `Episode ${e}` }], e % 9 === 0 ? 0.72 : 0.96)],
        relOut: `Starfall Station (2016)/Season 02/Starfall Station - S02E${ep} - Episode ${e}.mkv`,
      }),
    );
  }
  return items;
}

export function countsOf(items: JobItemOut[]) {
  const counts: Partial<Record<ItemStatus, number>> = {};
  for (const i of items) counts[i.status] = (counts[i.status] ?? 0) + 1;
  return counts;
}

export function buildJob(items: JobItemOut[]): JobDetail {
  const counts = countsOf(items);
  return {
    id: 1,
    label: 'Demo import',
    source: 'manual',
    status: (counts.review ?? 0) > 0 ? 'review' : 'ready',
    kind: null,
    mode: 'copy',
    format_id: null,
    dest_root: null,
    auto_execute: false,
    created_at: iso(12),
    updated_at: iso(2),
    finished_at: null,
    counts,
    total: items.length,
    progress: 1,
    error: null,
    items,
  };
}

export const providers: ProviderStatus[] = [
  { name: 'fixtures', label: 'Demo catalogue', kinds: ['movie', 'series', 'anime'], requires_key: false, configured: true, enabled: true, healthy: true, last_error: null, last_checked: iso(5), rate_limit: null, requests_made: 214, cache_hits: 180 },
  { name: 'tmdb', label: 'The Movie Database', kinds: ['movie', 'series', 'anime'], requires_key: true, configured: true, enabled: true, healthy: true, last_error: null, last_checked: iso(40), rate_limit: '20 requests / s', requests_made: 52, cache_hits: 31 },
  { name: 'tvdb', label: 'TheTVDB', kinds: ['series', 'anime', 'movie'], requires_key: true, configured: false, enabled: true, healthy: null, last_error: null, last_checked: null, rate_limit: '10 requests / s', requests_made: 0, cache_hits: 0 },
  { name: 'anidb', label: 'AniDB', kinds: ['anime'], requires_key: true, configured: true, enabled: true, healthy: false, last_error: 'Banned for 30 min (too many requests)', last_checked: iso(18), rate_limit: '1 request / 2.5 s', requests_made: 9, cache_hits: 44 },
  { name: 'trakt', label: 'Trakt', kinds: ['movie', 'series'], requires_key: true, configured: false, enabled: false, healthy: null, last_error: null, last_checked: null, rate_limit: '3 requests / s', requests_made: 0, cache_hits: 0 },
  { name: 'tvmaze', label: 'TVmaze', kinds: ['series'], requires_key: false, configured: true, enabled: true, healthy: true, last_error: null, last_checked: iso(90), rate_limit: '20 requests / 10 s', requests_made: 17, cache_hits: 3 },
];

export const watchers: WatchFolderOut[] = [
  { id: 1, path: '/storage/downloads/complete/tv', enabled: true, recursive: true, kind: 'series', auto_execute: true, settle_seconds: 30, format_id: null, dest_root: null, mode: null, status: 'watching', last_event_at: iso(25), last_error: null, files_seen: 128 },
  { id: 2, path: '/storage/downloads/complete/anime', enabled: true, recursive: true, kind: 'anime', auto_execute: false, settle_seconds: 60, format_id: null, dest_root: null, mode: 'keeplink', status: 'missing', last_event_at: null, last_error: 'Folder does not exist (retrying every 60 s)', files_seen: 0 },
];

export const settings: AppSettings = {
  library_roots: { movie: MOV, series: TV, anime: ANI },
  default_mode: 'move',
  conflict_policy: 'skip',
  min_confidence: 0.85,
  verify_crc: true,
  probe_media: true,
  move_sidecars: true,
  rename_folders_in_place: true,
  remove_empty_source_folders: true,
  clean_release_folders: true,
  release_quiet_seconds: 120,
  clear_solved_after_hours: 24,
  sidecar_extensions: ['srt', 'ass', 'ssa', 'sub', 'idx', 'sup', 'vtt', 'nfo'],
  video_extensions: ['mkv', 'mp4', 'm4v', 'avi', 'mov', 'wmv', 'ts', 'm2ts', 'webm'],
  ignore_patterns: ['*.part', '*.!qB', '*.tmp', '*.crdownload'],
  skip_samples: true,
  skip_extras: true,
  allowed_roots: ['/storage'],
  language: 'en',
  media_servers: [
    { id: 'a1b2c3', kind: 'plex', name: 'Plex', url: 'http://192.168.1.20:32400', token: '********', enabled: true, path_mappings: [] },
  ],
  category_kinds: { tv: 'series', sonarr: 'series', movies: 'movie', radarr: 'movie', anime: 'anime' },
  path_mappings: [{ source: '/downloads', target: '/storage/downloads' }],
  webhook_api_key: '********',
};

const B = (name: string, group: string, description: string, example: string, kinds: MediaKind[] = ['movie', 'series', 'anime'], type = 'str'): BindingSpec => ({ name, group, description, example, kinds, type });
const EPI: MediaKind[] = ['series', 'anime'];

export const bindings: BindingSpec[] = [
  B('n', 'Naming', 'Series or movie name', 'Starfall Station'),
  B('y', 'Naming', 'Year', '2016', undefined, 'int'),
  B('ny', 'Naming', 'Name (Year)', 'Starfall Station (2016)'),
  B('primaryTitle', 'Naming', 'Original title', 'Starfall Station'),
  B('az', 'Naming', 'Sort initial', 'S'),
  B('s', 'Episode', 'Season number', '1', EPI, 'int'),
  B('e', 'Episode', 'Episode number', '2', EPI, 'int'),
  B('s00e00', 'Episode', 'S01E02 style (multi-episode aware)', 'S01E02-E03', EPI),
  B('sxe', 'Episode', '1x02 style', '1x02', EPI),
  B('t', 'Episode', 'Episode title', 'Dead Reckoning', EPI),
  B('absolute', 'Episode', 'Absolute episode number', '15', EPI, 'int'),
  B('special', 'Episode', 'True for season 0', 'false', EPI, 'bool'),
  B('d', 'Episode', 'Air date', '2024-03-04', EPI, 'date'),
  B('episode', 'Episode', 'Episode object (.season .episode .title …)', 'S01E02', EPI, 'object'),
  B('collection', 'Movie', 'Movie collection', 'Meridian Collection', ['movie']),
  B('pi', 'Movie', 'Part index', '1', ['movie'], 'int'),
  B('pc', 'Movie', 'Part count', '2', ['movie'], 'int'),
  B('genres', 'Info', 'Genres', '[Drama, Sci-Fi]', undefined, 'list'),
  B('info', 'Info', 'Info object (.network .status .overview)', 'Northlight', undefined, 'object'),
  B('tmdbid', 'IDs', 'TMDb id', '800001'),
  B('tvdbid', 'IDs', 'TheTVDB id', '900001', EPI),
  B('imdbid', 'IDs', 'IMDb id', 'tt9000001'),
  B('vf', 'Quality', 'Video format', '1080p'),
  B('vc', 'Quality', 'Video codec', 'x265'),
  B('ac', 'Quality', 'Audio codec', 'DDP'),
  B('hdr', 'Quality', 'HDR format', 'HDR10'),
  B('source', 'Quality', 'Release source', 'BluRay'),
  B('edition', 'Quality', 'Edition', "Director's Cut"),
  B('group', 'Quality', 'Release group', 'NTb'),
  B('fn', 'File', 'Original filename without extension', 'Starfall.Station.S01E01'),
  B('ext', 'File', 'Extension', 'mkv'),
  B('crc32', 'File', 'CRC32 checksum', '5B1C2D3E'),
  B('plex', 'Presets', 'Plex standard path', 'Starfall Station (2016)/Season 01/…'),
  B('jellyfin', 'Presets', 'Jellyfin standard path', 'Starfall Station (2016)/Season 01/…'),
];

export const filters: FilterSpec[] = [
  { name: 'pad', signature: 'pad(width)', description: 'Zero-pad a number', example: '{e.pad(3)} → 007' },
  { name: 'lower', signature: 'lower()', description: 'Lower case', example: '{n.lower()}' },
  { name: 'upper', signature: 'upper()', description: 'Upper case', example: '{n.upper()}' },
  { name: 'upperInitial', signature: 'upperInitial()', description: 'Capitalize each word', example: '{t.upperInitial()}' },
  { name: 'colon', signature: 'colon(r)', description: 'Replace colons', example: "{n.colon(' - ')}" },
  { name: 'replace', signature: 'replace(a, b)', description: 'Literal replace', example: "{n.replace('&', 'and')}" },
  { name: 'ascii', signature: 'ascii()', description: 'Transliterate to ASCII', example: '{n.ascii()}' },
  { name: 'sortName', signature: 'sortName()', description: '"Office, The"', example: '{n.sortName()}' },
  { name: 'join', signature: 'join(sep)', description: 'Join a list', example: "{genres.join(', ')}" },
  { name: 'take', signature: 'take(n)', description: 'First n items or chars', example: '{genres.take(2)}' },
];

export const presets: FormatPreset[] = [
  { id: 'plex-series', name: 'Plex', kind: 'series', expression: '{plex}', description: 'Plex TV naming with Specials folder' },
  { id: 'plex-id-series', name: 'Plex + IDs', kind: 'series', expression: '{plex.id}', description: 'Adds {tvdb-ID} to the show folder' },
  { id: 'jellyfin-series', name: 'Jellyfin', kind: 'series', expression: '{jellyfin}', description: 'Season 00 for specials, [tvdbid-ID]' },
  { id: 'kodi-series', name: 'Kodi', kind: 'series', expression: '{kodi}', description: 'Season {s} folders' },
  { id: 'plex-movie', name: 'Plex', kind: 'movie', expression: '{plex}', description: '{ny}/{ny}' },
  { id: 'emby-movie', name: 'Emby', kind: 'movie', expression: '{emby}', description: '[tmdbid=ID]' },
  { id: 'anime-absolute', name: 'Anime absolute', kind: 'anime', expression: "{n}/{n} - {any{absolute.pad(3)}{s00e00}}{' - ' + t}", description: 'Absolute numbering, no seasons' },
];

export const formats: FormatOut[] = [
  { id: 1, name: 'Series (default)', kind: 'series', expression: "{n} ({y})/{special ? 'Specials' : 'Season ' + s.pad(2)}/{n} - {s00e00}{' - ' + t}", is_default: true, builtin: false, created_at: iso(5000), updated_at: iso(5000) },
  { id: 2, name: 'Movies (default)', kind: 'movie', expression: "{ny}/{ny}{' - ' + edition}{' - pt' + pi}", is_default: true, builtin: false, created_at: iso(5000), updated_at: iso(5000) },
  { id: 3, name: 'Anime (default)', kind: 'anime', expression: "{n}/{special ? 'Specials' : 'Season ' + s.pad(2)}/{n} - {s00e00}{' - ' + t}", is_default: true, builtin: false, created_at: iso(5000), updated_at: iso(5000) },
  { id: 4, name: 'Plex', kind: 'series', expression: '{plex}', is_default: false, builtin: true, created_at: iso(5000), updated_at: iso(5000) },
];

export function buildEvents(): EventOut[] {
  const rows: [number, EventOut['level'], string, string, number | null][] = [
    [1, 'info', 'job', 'Demo import: matched 54 of 60 files (6 need review)', 1],
    [2, 'warning', 'job', 'CRC mismatch for [Hatsuyuki] Kaze no Tsubasa - 07v2 [720p][DEADBEEF].mkv', 1],
    [4, 'error', 'provider', 'AniDB: banned for 30 min (too many requests); falling back to TheTVDB', null],
    [6, 'info', 'fileops', 'Copied Tensei Robo Gaiden - S01E07 - Iron Saint.mkv (512 KiB)', 1],
    [9, 'warning', 'watcher', '/storage/downloads/complete/anime is missing; retrying every 60 s', null],
    [15, 'info', 'webhook', 'qBittorrent: Harbor.Lights.2021.S01E03 accepted (category tv)', null],
    [22, 'info', 'mediaserver', 'Plex: refreshed 2 folders in “TV Shows”', null],
    [40, 'error', 'fileops', 'Paper Moons (2004) - pt1.avi: destination is outside the allowed roots', 1],
    [65, 'info', 'system', 'Schema 0.1.0 started (demo mode)', null],
  ];
  return rows.map(([m, level, category, message, job_id], i) => ({ id: 500 - i, ts: iso(m), level, category, message, job_id, data: {} }));
}

export function buildOperations(): OperationOut[] {
  return [
    {
      id: 41, job_id: 1, item_id: 115, mode: 'copy', status: 'done', bytes: 524288,
      source: IN + '[Judas] Tensei Robo Gaiden - S01E07 [1080p][HEVC x265 10bit].mkv',
      destination: `${ANI}/Tensei Robo Gaiden/Season 01/Tensei Robo Gaiden - S01E07 - Iron Saint.mkv`,
      sidecars: [], error: null, created_at: iso(6), undone_at: null,
    },
    {
      id: 40, job_id: null, item_id: null, mode: 'move', status: 'undone', bytes: 786432,
      source: '/storage/downloads/complete/movies/Iron Orchard 2023 2160p WEB-DL DDP5.1 Atmos HDR10 HEVC-CMRG.mkv',
      destination: `${MOV}/Iron Orchard (2023)/Iron Orchard (2023).mkv`,
      sidecars: [{ source: '/storage/downloads/complete/movies/Iron Orchard 2023.en.srt', destination: `${MOV}/Iron Orchard (2023)/Iron Orchard (2023).en.srt` }],
      error: null, created_at: iso(300), undone_at: iso(280),
    },
    {
      id: 39, job_id: null, item_id: null, mode: 'move', status: 'failed', bytes: 0,
      source: '/storage/downloads/complete/tv/Harbor.Lights.S01E01.mkv',
      destination: `${TV}/Harbor Lights (2008)/Season 01/Harbor Lights - S01E01 - The Keeper.mkv`,
      sidecars: [], error: 'Destination exists (policy: skip)', created_at: iso(900), undone_at: null,
    },
  ];
}
