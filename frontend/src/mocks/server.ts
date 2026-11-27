/**
 * In-browser mock of the Schema API for `npm run dev:mock` (visual checks without the backend).
 * Loaded only when Vite runs with `--mode mock`; production builds never include it.
 */
import { ApiError, installMockHandler, type RequestOptions } from '@/api/client';
import type {
  AuthStatus,
  BulkItemAction,
  Candidate,
  FormatIn,
  FormatOut,
  FormatValidateRequest,
  FsListing,
  ItemUpdate,
  JobItemOut,
  PreviewRequest,
} from '@/api/types';
import { tokenize } from '@/lib/expr/tokenizer';
import { historyPage, homeAction, homeDismissAll, homeOut, resetHomeMock, startNow, undoOperation } from './home';
import {
  bindings,
  buildEvents,
  buildItems,
  buildJob,
  buildOperations,
  filters,
  formats as seedFormats,
  iso,
  presets,
  providers,
  settings as seedSettings,
  watchers,
} from './data';

const auth: AuthStatus = { setup_required: false, authenticated: true, username: 'admin', auth_enabled: true, demo: true, version: '0.1.0' };
let items = buildItems();
let settings = structuredClone(seedSettings);
let formats = structuredClone(seedFormats);
const events = buildEvents();
const operations = buildOperations();

const delay = <T>(v: T, ms = 160) => new Promise<T>((r) => setTimeout(() => r(structuredClone(v)), ms));
const job = () => buildJob(items);

/** The slim list form: only the selected (or best) candidate, like the real API. */
function slim(it: JobItemOut): JobItemOut {
  const chosen = it.candidates.find((c) => c.id === it.selected_candidate_id) ?? it.candidates[0];
  return { ...it, candidates: chosen ? [{ ...chosen, reasons: [] }] : [], candidate_count: it.candidates.length, slim: true };
}

function setItem(id: number, patch: Partial<JobItemOut>): JobItemOut {
  const it = items.find((i) => i.id === id);
  if (!it) throw new ApiError(404, 'Item not found', 'Item not found', `/items/${id}`);
  Object.assign(it, patch, { updated_at: new Date().toISOString() });
  return it;
}

function fsList(path?: string): FsListing {
  const p = path || '/storage';
  const dirs: Record<string, string[]> = {
    '/storage': ['demo', 'downloads', 'media'],
    '/storage/demo': ['incoming', 'library'],
    '/storage/downloads': ['complete', 'incomplete'],
    '/storage/downloads/complete': ['tv', 'movies'],
  };
  const files: Record<string, string[]> = {
    '/storage/demo/incoming': items.slice(0, 14).map((i) => i.filename).concat(['notes.txt']),
    '/storage/downloads/complete/tv': ['Harbor.Lights.2021.S01E04.2160p.AMZN.WEB-DL.mkv', 'Harbor.Lights.2021.S01E04.en.srt'],
  };
  if (!p.startsWith('/storage')) throw new ApiError(403, 'Path is outside the allowed roots', null, '/fs/list');
  return {
    path: p,
    parent: p === '/storage' ? null : p.split('/').slice(0, -1).join('/') || '/storage',
    roots: ['/storage'],
    entries: [
      ...(dirs[p] ?? []).map((name) => ({ name, path: `${p}/${name}`, is_dir: true, size: null, modified: iso(60), is_video: false })),
      ...(files[p] ?? []).map((name, i) => ({
        name,
        path: `${p}/${name}`,
        is_dir: false,
        size: 512 * 1024 * (i + 1),
        modified: iso(30 + i),
        is_video: /\.(mkv|mp4|avi)$/i.test(name),
      })),
    ],
  };
}

function validate(req: FormatValidateRequest) {
  const toks = tokenize(req.expression);
  const known = new Set(bindings.map((b) => b.name.split('.')[0]).concat(['plex', 'jellyfin', 'emby', 'kodi', 'id']));
  const issues = toks
    .filter((t) => t.error || (t.type === 'binding' && !known.has(t.text)))
    .map((t) =>
      t.error
        ? { message: t.error, start: t.start, end: t.end, severity: 'error' as const }
        : { message: `Unsupported binding "${t.text}" (renders empty)`, start: t.start, end: t.end, severity: 'warning' as const },
    );
  const samples =
    req.kind === 'movie'
      ? ['The.Quiet.Meridian.2019.Directors.Cut.1080p.BluRay.x264-SPARKS.mkv', 'Paper.Moons.2004.CD1.DVDRip.XviD-FiCO.avi']
      : req.kind === 'anime'
        ? ['[SubsPlease] Kaze no Tsubasa - 15 (1080p).mkv', 'Kaze no Tsubasa - OVA [BD 1080p].mkv']
        : ['Starfall.Station.S01E02E03.720p.HDTV.x264-KILLERS.mkv', 'Starfall Station - S00E02 - Christmas Special.mkv', 'Harbor Lights S02E05.mkv'];
  const out: Record<string, string> = {
    'The.Quiet.Meridian.2019.Directors.Cut.1080p.BluRay.x264-SPARKS.mkv': "The Quiet Meridian (2019)/The Quiet Meridian (2019) - Director's Cut.mkv",
    'Paper.Moons.2004.CD1.DVDRip.XviD-FiCO.avi': 'Paper Moons (2004)/Paper Moons (2004) - pt1.avi',
    '[SubsPlease] Kaze no Tsubasa - 15 (1080p).mkv': 'Kaze no Tsubasa/Season 02/Kaze no Tsubasa - S02E03 - Crosswind.mkv',
    'Kaze no Tsubasa - OVA [BD 1080p].mkv': 'Kaze no Tsubasa/Specials/Kaze no Tsubasa - S00E01 - Beyond the Clouds.mkv',
    'Starfall.Station.S01E02E03.720p.HDTV.x264-KILLERS.mkv': 'Starfall Station (2016)/Season 01/Starfall Station - S01E02-E03 - Dead Reckoning & Signal Fire.mkv',
    'Starfall Station - S00E02 - Christmas Special.mkv': 'Starfall Station (2016)/Specials/Starfall Station - S00E02 - Christmas Special.mkv',
    'Harbor Lights S02E05.mkv': 'Harbor Lights (2008)/Season 02/Harbor Lights - S02E05 - Undertow.mkv',
  };
  const hasError = issues.some((i) => i.severity === 'error');
  return {
    ok: !hasError,
    issues,
    samples: samples.map((s) => (hasError ? { source: s, output: null, error: 'Syntax error' } : { source: s, output: out[s], error: null })),
  };
}

type Handler = (m: RegExpMatchArray, opts: RequestOptions) => unknown;
const routes: [string, RegExp, Handler][] = [
  ['GET', /^\/health$/, () => ({ status: 'ok', version: '0.1.0', commit: null })],
  ['GET', /^\/auth\/status$/, () => auth],
  ['POST', /^\/auth\/(login|setup)$/, () => ({ ...auth, authenticated: true, setup_required: false })],
  ['POST', /^\/auth\/logout$/, () => ({ ...auth, authenticated: false })],
  ['GET', /^\/home$/, () => homeOut()],
  ['POST', /^\/home\/actions$/, (_m, o) => homeAction(o.body as { key: string; action: string })],
  ['POST', /^\/home\/refresh$/, () => ({ cleared: 0 })],
  ['POST', /^\/home\/dismiss-all$/, () => homeDismissAll()],
  ['POST', /^\/watch-folders\/(\d+)\/start-now$/, (m, o) => startNow(Number(m[1]), (o.body as { path?: string } | undefined)?.path)],
  ['GET', /^\/history$/, (_m, o) => historyPage(o.query)],
  ['GET', /^\/jobs$/, (_m, o) => {
    const { items: _i, ...j } = job();
    const extra = [
      { ...j, id: 2, label: 'qBittorrent: Harbor.Lights.2021.S01E03', source: 'webhook' as const, status: 'completed' as const, counts: { done: 1 }, total: 1, finished_at: iso(15) },
      { ...j, id: 3, label: null, source: 'watch' as const, status: 'failed' as const, counts: { error: 2 }, total: 2, error: 'Watch folder disappeared during scan' },
    ];
    const all = [j, ...extra];
    const status = o.query?.status as string | undefined;
    return status ? all.filter((x) => x.status === status) : all;
  }],
  ['GET', /^\/jobs\/1$/, (_m, o) => (o.query?.limit === 0 ? { ...job(), items: [], items_total: items.length, next_after: null } : job())],
  ['GET', /^\/jobs\/1\/items$/, (_m, o) => {
    const statuses = o.query?.status ? String(o.query.status).split(',') : null;
    const q = o.query?.q ? String(o.query.q).toLowerCase() : '';
    const after = typeof o.query?.after === 'number' ? o.query.after : -1;
    const limit = typeof o.query?.limit === 'number' ? o.query.limit : 200;
    const matching = items.filter(
      (i) => (!statuses || statuses.includes(i.status)) && (!q || i.source_path.toLowerCase().includes(q) || (i.parsed.title ?? '').toLowerCase().includes(q)),
    );
    const rest = matching.filter((i) => i.id > after);
    const page = rest.slice(0, limit).map(slim);
    return { job_id: 1, items: page, total: matching.length, offset: 0, limit, next_after: rest.length > limit ? page[page.length - 1].id : null, counts: job().counts };
  }],
  ['GET', /^\/items\/(\d+)$/, (m) => {
    const it = items.find((i) => i.id === Number(m[1]));
    if (!it) throw new ApiError(404, 'item not found', 'item not found', m[0]);
    return { ...it, candidate_count: it.candidates.length, slim: false };
  }],
  ['POST', /^\/jobs\/1\/execute$/, (_m, o) => {
    const body = (o.body ?? {}) as { item_ids?: number[] | null };
    const ids = new Set(body.item_ids ?? items.filter((i) => i.status === 'matched' || i.status === 'approved').map((i) => i.id));
    items = items.map((i) => (ids.has(i.id) ? { ...i, status: 'done' as const, final_path: i.proposed_path } : i));
    const { items: _i, ...rest } = job();
    return rest;
  }],
  ['POST', /^\/jobs\/1\/(cancel|rematch)$/, () => {
    const { items: _i, ...rest } = job();
    return rest;
  }],
  ['DELETE', /^\/jobs\/\d+$/, () => undefined],
  ['PATCH', /^\/items\/(\d+)$/, (m, o) => {
    const patch = o.body as ItemUpdate;
    const id = Number(m[1]);
    const it = items.find((i) => i.id === id);
    const next: Partial<JobItemOut> = {};
    if (patch.status) next.status = patch.status;
    if (patch.selected_candidate_id) {
      next.selected_candidate_id = patch.selected_candidate_id;
      const c = it?.candidates.find((x) => x.id === patch.selected_candidate_id);
      if (c) next.confidence = c.score;
    }
    if (patch.proposed_path) Object.assign(next, { proposed_path: patch.proposed_path, path_overridden: true });
    if (patch.reset_path) next.path_overridden = false;
    return setItem(id, next);
  }],
  ['POST', /^\/items\/(\d+)\/rematch$/, (m) => setItem(Number(m[1]), { status: 'matched', warnings: [] })],
  ['POST', /^\/items\/(\d+)\/search$/, (_m, o) => {
    const q = String((o.body as { query: string }).query);
    const c: Candidate = { ...items[0].candidates[0], id: `tvmaze:series:77${q.length}`, provider: 'tvmaze', title: q.replace(/\b\w/g, (x) => x.toUpperCase()), score: 0.74, reasons: ['manual search'] };
    return [c];
  }],
  ['POST', /^\/items\/bulk$/, (_m, o) => {
    const body = o.body as BulkItemAction;
    const status = body.action === 'approve' ? 'approved' : body.action === 'skip' ? 'skipped' : body.action === 'review' ? 'review' : 'matched';
    const q = (body.q ?? '').toLowerCase();
    const ids =
      body.item_ids ??
      items.filter((i) => (!body.statuses?.length || body.statuses.includes(i.status)) && (!q || i.source_path.toLowerCase().includes(q))).map((i) => i.id);
    return ids.map((id) => setItem(id, { status }));
  }],
  ['POST', /^\/preview$/, (_m, o) => (o.body as PreviewRequest).names.map((name) => {
    const it = items.find((i) => i.filename === name) ?? items[1];
    return { name, parsed: it.parsed, best: it.candidates[0] ?? null, confidence: it.confidence, rendered: it.proposed_relative, error: null };
  })],
  ['GET', /^\/formats$/, () => formats],
  ['GET', /^\/formats\/catalog$/, () => ({ bindings, filters, presets })],
  ['POST', /^\/formats\/validate$/, (_m, o) => validate(o.body as FormatValidateRequest)],
  ['POST', /^\/formats$/, (_m, o) => {
    const f: FormatOut = { ...(o.body as FormatIn), is_default: !!(o.body as FormatIn).is_default, id: Math.max(...formats.map((x) => x.id)) + 1, builtin: false, created_at: iso(0), updated_at: iso(0) };
    formats = [...formats.map((x) => (f.is_default && x.kind === f.kind ? { ...x, is_default: false } : x)), f];
    return f;
  }],
  ['PUT', /^\/formats\/(\d+)$/, (m, o) => {
    const id = Number(m[1]);
    const body = o.body as FormatIn;
    formats = formats.map((x) => (x.id === id ? { ...x, ...body, is_default: !!body.is_default, updated_at: iso(0) } : body.is_default && x.kind === body.kind ? { ...x, is_default: false } : x));
    return formats.find((x) => x.id === id);
  }],
  ['DELETE', /^\/formats\/(\d+)$/, (m) => {
    formats = formats.filter((x) => x.id !== Number(m[1]));
    return undefined;
  }],
  ['GET', /^\/operations$/, () => operations],
  ['POST', /^\/operations\/(\d+)\/undo$/, (m) => {
    const fromHome = undoOperation(Number(m[1]));
    if (fromHome) return fromHome;
    const op = operations.find((x) => x.id === Number(m[1]));
    if (op) Object.assign(op, { status: 'undone', undone_at: iso(0) });
    return op;
  }],
  ['GET', /^\/watch-folders$/, () => watchers],
  ['POST', /^\/watch-folders\/(\d+)\/scan$/, () => {
    const { items: _i, ...rest } = job();
    return rest;
  }],
  ['GET', /^\/settings$/, () => settings],
  ['PUT', /^\/settings$/, (_m, o) => {
    settings = structuredClone(o.body as typeof settings);
    return settings;
  }],
  ['GET', /^\/settings\/api-key$/, () => ({ api_key: 'sk_demo_3f9a1c7e2b8d4e6f' })],
  ['POST', /^\/settings\/api-key$/, () => ({ api_key: `sk_demo_${Math.random().toString(16).slice(2, 18)}` })],
  ['GET', /^\/providers$/, () => ({
    statuses: providers,
    priority: { movie: ['tmdb', 'trakt'], series: ['tvdb', 'tmdb', 'tvmaze', 'trakt'], anime: ['anidb', 'tvdb', 'tmdb', 'tvmaze'] },
    disabled: ['trakt'],
    language: 'en',
    credentials: { tmdb_api_key: '********', tvdb_api_key: null, tvdb_pin: null, anidb_client: 'schemademo', anidb_client_version: '1', trakt_client_id: null },
  })],
  ['POST', /^\/providers\/(\w+)\/test$/, (m) => ({ ...providers.find((p) => p.name === m[1])!, healthy: true, last_checked: iso(0), last_error: null })],
  ['POST', /^\/mediaservers\/test$/, () => ({ ok: true, message: 'Connected to Plex 1.40 (3 libraries)' })],
  ['GET', /^\/events$/, (_m, o) => {
    const level = o.query?.level;
    const category = o.query?.category;
    return events.filter((e) => (!level || e.level === level) && (!category || e.category === category));
  }],
  ['GET', /^\/fs\/list$/, (_m, o) => fsList(o.query?.path as string | undefined)],
  ['POST', /^\/demo\/reset$/, () => {
    items = buildItems();
    resetHomeMock();
    return { ok: true };
  }],
];

export function installMocks() {
  installMockHandler((method, path, opts) => {
    for (const [m, re, handler] of routes) {
      if (m !== method) continue;
      const match = path.match(re);
      if (!match) continue;
      try {
        return delay(handler(match, opts));
      } catch (e) {
        return new Promise((_, reject) => setTimeout(() => reject(e), 120));
      }
    }
    return new Promise((_, reject) =>
      setTimeout(() => reject(new ApiError(404, `Mock: no handler for ${method} ${path}`, null, path)), 80),
    );
  });
  console.info('[schema] API mocks installed (npm run dev:mock)');
}

