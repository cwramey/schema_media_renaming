import type { EventLevel, HistoryFilter, JobItemFilter, JobStatus, MediaKind } from './types';

export interface JobListFilter {
  status?: JobStatus;
  limit?: number;
  offset?: number;
}

export interface EventFilter {
  level?: EventLevel;
  category?: string;
  job_id?: number;
  limit?: number;
  offset?: number;
}

export interface OperationFilter {
  job_id?: number;
  limit?: number;
  offset?: number;
}

export interface HistoryQuery {
  filter: HistoryFilter;
  q?: string;
  kind?: Exclude<MediaKind, 'unknown'>;
}

export interface SearchParams {
  q: string;
  kind?: MediaKind;
  year?: number;
  provider?: string;
}

/** Query-key factory. Every key starts with a stable root so the WS reducer can match prefixes. */
export const qk = {
  health: ['health'] as const,
  authStatus: ['auth', 'status'] as const,
  home: ['home'] as const,
  history: {
    all: ['history'] as const,
    list: (h: HistoryQuery) => ['history', { filter: h.filter, q: h.q || null, kind: h.kind ?? null }] as const,
  },
  jobs: {
    all: ['jobs'] as const,
    lists: ['jobs', 'list'] as const,
    list: (f: JobListFilter = {}) => ['jobs', 'list', f] as const,
    detail: (id: number) => ['jobs', 'detail', id] as const,
    /** Every paged item list of a job (any filter). */
    itemLists: (jobId: number) => ['jobs', 'items', jobId] as const,
    /** Infinite query of slim item pages for one filter. */
    items: (jobId: number, f: JobItemFilter = {}) =>
      ['jobs', 'items', jobId, { statuses: f.statuses?.length ? f.statuses : null, q: f.q || null }] as const,
  },
  /** Full items (all candidates), fetched on demand for the inspector. */
  items: {
    all: ['items'] as const,
    detail: (id: number) => ['items', id] as const,
  },
  formats: {
    all: ['formats'] as const,
    list: ['formats', 'list'] as const,
    catalog: ['formats', 'catalog'] as const,
    validate: (expression: string, kind: MediaKind, samples?: string[] | null) =>
      ['formats', 'validate', { expression, kind, samples: samples ?? null }] as const,
  },
  operations: {
    all: ['operations'] as const,
    list: (f: OperationFilter = {}) => ['operations', f] as const,
  },
  watchFolders: ['watch-folders'] as const,
  settings: ['settings'] as const,
  apiKey: ['settings', 'api-key'] as const,
  providers: ['providers'] as const,
  events: {
    all: ['events'] as const,
    list: (f: EventFilter = {}) => ['events', f] as const,
  },
  fs: (path?: string) => ['fs', path ?? ''] as const,
  search: (p: SearchParams) => ['search', p] as const,
};
