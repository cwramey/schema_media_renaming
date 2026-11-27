/**
 * Mock Home board and History feed for `npm run dev:mock`, seeded with the design canvas samples
 * (Never After Dark settling, three bonus clips in "Needs you", Soulm8te & co. in the library).
 * A tiny simulation moves the settling file through Renaming into the library when its wait ends.
 * Dev-only: never imported by production code paths.
 */
import { ApiError } from '@/api/client';
import type {
  HistoryEntry,
  HistoryFilter,
  HistoryPage,
  HomeOut,
  IncomingFile,
  LibraryCard,
  MediaKind,
  NeedsYouCard,
  OperationOut,
  RenamingCard,
} from '@/api/types';
import { providers } from './data';

const GB = 1024 ** 3;
const DL = '/media/downloads';
const MOVIES = '/media/movies';
const TV = '/media/tv';

/** Local wall-clock time `daysAgo` days back, never later than a few minutes ago. */
function at(daysAgo: number, h: number, m: number): string {
  const now = new Date();
  const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - daysAgo, h, m);
  // Before that time of day: a few minutes ago instead, keeping later times newer.
  const latest = now.getTime() - (24 * 60 - (h * 60 + m)) * 1000;
  return new Date(Math.min(d.getTime(), latest)).toISOString();
}

/* ------------------------------------------------------------------ incoming (simulated) */

interface SimIncoming extends IncomingFile {
  /** When the simulated rename finishes (after the settle wait ends). */
  doneAt?: number;
}

const SETTLE = 120;
let incoming: SimIncoming[] = [];
let renaming: RenamingCard[] = [];

function seedIncoming() {
  const now = Date.now();
  incoming = [
    {
      folder_id: 1,
      path: `${DL}/Never.After.Dark.2026.1080p.WEBRip.DUAL.10Bit.DDP5.1.x265-NeoNoir.mkv`,
      name: 'Never.After.Dark.2026.1080p.WEBRip.DUAL.10Bit.DDP5.1.x265-NeoNoir.mkv',
      size: 1.4 * GB,
      state: 'settling',
      settle_seconds: SETTLE,
      stable_since: new Date(now - 72_000).toISOString(),
      settles_at: new Date(now + 48_000).toISOString(),
      title_guess: 'Never After Dark (2026)',
    },
    {
      folder_id: 1,
      path: `${DL}/The.Last.Lighthouse.2026.2160p.WEB-DL.DDP5.1.HEVC-FLUX.mkv.part`,
      name: 'The.Last.Lighthouse.2026.2160p.WEB-DL.DDP5.1.HEVC-FLUX.mkv.part',
      size: 3.1 * GB,
      state: 'partial',
      settle_seconds: SETTLE,
      stable_since: null,
      settles_at: null,
      title_guess: 'The Last Lighthouse (2026)',
    },
  ];
  renaming = [];
}

/* ------------------------------------------------------------------ needs you */

let needsYou: NeedsYouCard[] = [];

function seedNeedsYou() {
  const card = (n: number, title: string, reason: string, label: string, itemId: number, candidate: string): NeedsYouCard => ({
    key: `item:${itemId}`,
    kind: 'item',
    item_id: itemId,
    job_id: 1,
    event_id: null,
    title,
    reason,
    reason_code: 'extra',
    primary: { label, action: 'skip' },
    candidate_title: candidate,
    confidence: 0.62,
    ts: new Date(Date.now() - n * 60_000).toISOString(),
  });
  needsYou = [
    card(
      4,
      'Blade Runner 2049 Environments Reel by DNEG.mkv',
      'Looks like a bonus clip. Blade Runner (1982) is already in your library.',
      'Skip as extra',
      104,
      'Blade Runner (1982)',
    ),
    card(5, "The Shootout at Top Dollar's.mkv", 'Bonus clip · would overwrite Years When I Was a Child Outside (2008).', 'Skip', 109, 'Years When I Was a Child Outside (2008)'),
    card(6, 'The Funboy Fight.mkv', 'Bonus clip · same conflict as above.', 'Skip', 112, 'Years When I Was a Child Outside (2008)'),
    {
      key: 'group:8',
      kind: 'group',
      item_id: null,
      job_id: 8,
      event_id: null,
      count: 1019,
      title: "1,019 files from job 'tv' no longer exist at their source",
      reason: 'The file was moved or deleted before Schema could rename it',
      reason_code: 'source_missing',
      primary: { label: 'Dismiss all', action: 'dismiss' },
      candidate_title: null,
      confidence: null,
      ts: new Date(Date.now() - 9 * 60_000).toISOString(),
    },
  ];
}

/* ------------------------------------------------------------------ history + library */

let history: HistoryEntry[] = [];
let totalFiles = 156;
let totalBytes = 2.7 * 1024 ** 4;
let nextOpId = 300;

function moved(
  operation_id: number,
  title: string,
  from: string,
  to: string,
  bytes: number,
  ts: string,
  kind: MediaKind = 'movie',
): HistoryEntry {
  return {
    key: `op:${operation_id}`,
    type: 'moved',
    ts,
    title,
    kind,
    source: from,
    destination: to,
    detail: null,
    bytes,
    mode: 'move',
    operation_id,
    item_id: null,
    job_id: null,
    can_undo: true,
    can_fix: false,
  };
}

const FILLER = [
  'Paper Moons (2004)',
  'The Quiet Meridian (2019)',
  'Iron Orchard (1987)',
  'Glass Harbor (2011)',
  'Northbound (1999)',
  'Cinder & Salt (2015)',
  'A Year Without Winter (2021)',
  'Low Tide (2003)',
];

function seedHistory() {
  history = [
    {
      key: 'op:240',
      type: 'undone',
      ts: at(0, 11, 42),
      title: 'Iron Orchard (2023)',
      kind: 'movie',
      source: `${DL}/Iron Orchard 2023 2160p WEB-DL DDP5.1 Atmos HDR10 HEVC-CMRG.mkv`,
      destination: `${MOVIES}/Iron Orchard (2023)/Iron Orchard (2023).mkv`,
      detail: 'Undone: put back in downloads',
      bytes: 18.2 * GB,
      mode: 'move',
      operation_id: 240,
      item_id: null,
      job_id: null,
      can_undo: false,
      can_fix: false,
    },
    {
      key: 'item:150',
      type: 'problem',
      ts: at(0, 9, 15),
      title: 'Harbor.Lights.S01E01.mkv',
      kind: 'series',
      source: `${DL}/Harbor.Lights.S01E01.mkv`,
      destination: null,
      detail: 'Could not reach TheTVDB to look it up',
      bytes: 0,
      mode: null,
      operation_id: null,
      item_id: 109,
      job_id: 1,
      can_undo: false,
      can_fix: true,
    },
    moved(
      203,
      'Soulm8te (2026)',
      `${DL}/Soulm8te.2026.1080p.WEBRip.10Bit.DDP5.1.x265-NeoNoir.mkv`,
      `${MOVIES}/Soulm8te (2026)/Soulm8te (2026).mkv`,
      1.4 * GB,
      at(1, 18, 14),
    ),
    moved(
      202,
      'A Nightmare on Elm Street 3 – Dream Warriors (1987)',
      `${DL}/A Nightmare on Elm Street 3 Dream Warriors 1987 REPACK 1080p BluRay x265-YAWNTiC.mkv`,
      `${MOVIES}/A Nightmare on Elm Street 3 - Dream Warriors (1987)/A Nightmare on Elm Street 3 - Dream Warriors (1987).mkv`,
      4.1 * GB,
      at(1, 18, 14),
    ),
    moved(
      201,
      'Hallowarrior (2026)',
      `${DL}/Hallowarrior.2026.1080p.WEBRip.x264.AAC5.1-[YTS.GG - YTS.BZ].mp4`,
      `${MOVIES}/Hallowarrior (2026)/Hallowarrior (2026).mp4`,
      1.4 * GB,
      at(1, 18, 8),
    ),
    ...[3, 2, 1].map((ep) =>
      moved(
        204 + ep,
        `Harbor Lights (2024) · S01E0${ep}`,
        `${DL}/Harbor.Lights.2024.S01E0${ep}.1080p.WEB.h264.mkv`,
        `${TV}/Harbor Lights (2024)/Season 01/Harbor Lights - S01E0${ep}.mkv`,
        2.1 * GB,
        at(1, 12, 30 - ep),
        'series',
      ),
    ),
    {
      key: 'item:104',
      type: 'skipped',
      ts: at(2, 19, 4),
      title: 'Blade Runner 2049 Environments Reel by DNEG.mkv',
      kind: 'movie',
      source: `${DL}/Blade Runner 2049 Environments Reel by DNEG.mkv`,
      destination: null,
      detail: 'Matched Blade Runner (1982) · a file with that name already exists',
      bytes: 0,
      mode: null,
      operation_id: null,
      item_id: 104,
      job_id: 1,
      can_undo: false,
      can_fix: true,
    },
    moved(
      200,
      "Trick 'r Treat (2007)",
      `${MOVIES}/Trick'r.Treat.2007.mkv`,
      `${MOVIES}/Trick 'r Treat (2007)/Trick 'r Treat (2007).mkv`,
      56.8 * GB,
      at(2, 19, 4),
    ),
    moved(
      199,
      'True Romance (1993)',
      `${MOVIES}/True Romance 1993 Director's Cut.mkv`,
      `${MOVIES}/True Romance (1993)/True Romance (1993) - Director's Cut.mkv`,
      27.4 * GB,
      at(2, 19, 4),
    ),
  ];
  // Older filler so "Show older" has something to page through.
  for (let i = 0; i < 64; i++) {
    const title = FILLER[i % FILLER.length];
    const id = 198 - i;
    const name = title.replace(/[()]/g, '').replace(/ /g, '.');
    history.push(
      moved(id, title, `${DL}/${name}.1080p.BluRay.x264.mkv`, `${MOVIES}/${title}/${title}.mkv`, (2 + (i % 9)) * GB, at(3 + Math.floor(i / 3), 20 - (i % 3), 10 + i % 40)),
    );
  }
  totalFiles = 156;
  totalBytes = 2.7 * 1024 ** 4;
}

function libraryRecent(): LibraryCard[] {
  return history
    .filter((e) => e.type === 'moved' && e.operation_id !== null && e.destination)
    .slice(0, 12)
    .map((e) => {
      const ep = /^(.*) · S(\d+)E(\d+)$/.exec(e.title);
      return {
        operation_id: e.operation_id as number,
        title: e.title,
        kind: e.kind,
        destination: e.destination as string,
        ts: e.ts,
        bytes: e.bytes,
        can_undo: e.can_undo,
        show: ep ? ep[1] : null,
        season: ep ? Number(ep[2]) : null,
        episode: ep ? Number(ep[3]) : null,
      };
    });
}

export function resetHomeMock() {
  seedIncoming();
  seedNeedsYou();
  seedHistory();
}
resetHomeMock();

/* ------------------------------------------------------------------ simulation */

function tick() {
  const now = Date.now();
  for (const f of incoming) {
    if (f.state !== 'settling' || !f.settles_at || Date.parse(f.settles_at) > now || f.doneAt) continue;
    f.doneAt = now + 6_000;
    renaming = [
      ...renaming,
      { item_id: 900 + renaming.length, job_id: 1, title: f.title_guess ?? f.name, filename: f.name, status: 'executing', job_progress: 0.4 },
    ];
  }
  const finished = incoming.filter((f) => f.doneAt !== undefined && f.doneAt <= now);
  if (finished.length === 0) return;
  incoming = incoming.filter((f) => !finished.includes(f));
  for (const f of finished) {
    const title = f.title_guess ?? f.name;
    renaming = renaming.filter((r) => r.filename !== f.name);
    history = [
      moved(nextOpId++, title, f.path, `${MOVIES}/${title}/${title}.mkv`, f.size, new Date().toISOString()),
      ...history,
    ];
    totalFiles += 1;
    totalBytes += f.size;
  }
}

/* ------------------------------------------------------------------ handlers */

export function homeOut(): HomeOut {
  tick();
  return {
    needs_you_count: needsYou.reduce((n, c) => n + (c.count ?? 1), 0),
    incoming: incoming.map(({ doneAt: _d, ...f }) => f).filter((f) => !renaming.some((r) => r.filename === f.name)),
    needs_you: needsYou,
    renaming,
    library: { recent: libraryRecent(), total_files: totalFiles, total_bytes: totalBytes },
    status: {
      watch: [{ id: 1, path: DL, status: 'watching', enabled: true }],
      // The demo catalogue's AniDB ban is not Home's business: optional providers stay quiet.
      providers: providers.filter((p) => p.name === 'tmdb' || p.name === 'tvmaze'),
      media_servers: [{ id: 'jf1', kind: 'jellyfin', name: 'Jellyfin', url: 'http://192.168.1.20:8096', enabled: true }],
    },
    server_time: new Date().toISOString(),
  };
}

export function homeDismissAll(): { cleared: number } {
  const cleared = needsYou.length;
  needsYou = [];
  return { cleared };
}

export function homeAction(body: { key: string; action: string }): { ok: true } {
  const card = needsYou.find((c) => c.key === body.key);
  if (!card) throw new ApiError(404, 'That card is gone', 'That card is gone', '/home/actions');
  needsYou = needsYou.filter((c) => c.key !== body.key);
  if (body.action === 'skip' && card.kind === 'item') {
    history = [
      {
        key: card.key,
        type: 'skipped',
        ts: new Date().toISOString(),
        title: card.title,
        kind: 'movie',
        source: `${DL}/${card.title}`,
        destination: null,
        detail: card.reason,
        bytes: 0,
        mode: null,
        operation_id: null,
        item_id: card.item_id,
        job_id: card.job_id,
        can_undo: false,
        can_fix: true,
      },
      ...history,
    ];
  }
  return { ok: true };
}

export function startNow(folderId: number, path?: string): { started: number } {
  let started = 0;
  const nowIso = new Date().toISOString();
  for (const f of incoming) {
    if (f.folder_id !== folderId || f.state !== 'settling' || (path && f.path !== path)) continue;
    f.settles_at = nowIso;
    started += 1;
  }
  return { started };
}

export function historyPage(query: Record<string, unknown> | undefined): HistoryPage {
  tick();
  const filter = (query?.filter as HistoryFilter | undefined) ?? 'all';
  const q = String(query?.q ?? '').toLowerCase();
  const kind = query?.kind as MediaKind | undefined;
  const limit = typeof query?.limit === 'number' ? query.limit : 50;
  const offset = query?.before ? Number(query.before) : 0;
  const type = { all: null, moved: 'moved', skipped: 'skipped', problems: 'problem' }[filter];
  const matching = history.filter(
    (e) =>
      (!type || e.type === type) &&
      (!kind || e.kind === kind) &&
      (!q || e.title.toLowerCase().includes(q) || e.source.toLowerCase().includes(q) || (e.destination ?? '').toLowerCase().includes(q)),
  );
  const entries = matching.slice(offset, offset + limit);
  return { entries, next_before: offset + limit < matching.length ? String(offset + limit) : null };
}

/** Undo for operations that live in the Home/History mock (null when the id is not one of them). */
export function undoOperation(id: number): OperationOut | null {
  const e = history.find((x) => x.operation_id === id);
  if (!e) return null;
  if (e.type !== 'moved') throw new ApiError(409, 'Already undone', 'Already undone', `/operations/${id}/undo`);
  Object.assign(e, { type: 'undone', can_undo: false, detail: 'Undone: put back where it was' });
  totalFiles -= 1;
  totalBytes -= e.bytes;
  return {
    id,
    job_id: e.job_id,
    item_id: e.item_id,
    mode: 'move',
    source: e.source,
    destination: e.destination ?? '',
    status: 'undone',
    bytes: e.bytes,
    sidecars: [],
    error: null,
    created_at: e.ts,
    undone_at: new Date().toISOString(),
  };
}
