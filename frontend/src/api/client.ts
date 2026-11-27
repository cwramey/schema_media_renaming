/**
 * Typed fetch client for /api (SPEC §5).
 *
 * - Cookie session: `credentials: 'include'`.
 * - CSRF guard (SPEC §11): every request carries `X-Schema-Client: web`.
 * - Errors: FastAPI `{"detail": ...}` bodies become a typed `ApiError`.
 */

export const API_BASE = '/api';

export interface ValidationDetail {
  loc: (string | number)[];
  msg: string;
  type: string;
}

export class ApiError extends Error {
  readonly status: number;
  /** Raw `detail` from the response body (string, validation list, or anything else). */
  readonly detail: unknown;
  readonly path: string;

  constructor(status: number, message: string, detail: unknown, path: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.path = path;
  }

  get isUnauthorized() {
    return this.status === 401;
  }
  get isForbidden() {
    return this.status === 403;
  }
  get isNotFound() {
    return this.status === 404;
  }
  get isConflict() {
    return this.status === 409;
  }
  get isValidation() {
    return this.status === 422;
  }
  /** Network failures and 5xx are worth a retry; 4xx are not. */
  get isRetryable() {
    return this.status === 0 || this.status >= 500 || this.status === 429;
  }
  get validationErrors(): ValidationDetail[] {
    return Array.isArray(this.detail) ? (this.detail as ValidationDetail[]) : [];
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

/** Human-readable message for any thrown value. */
export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return String(e);
}

function messageFromDetail(status: number, detail: unknown, fallback: string): string {
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as Partial<ValidationDetail>;
    const where = Array.isArray(first.loc) ? first.loc.filter((p) => p !== 'body').join('.') : '';
    const msg = first.msg ?? 'Invalid input';
    const more = detail.length > 1 ? ` (+${detail.length - 1} more)` : '';
    return where ? `${where}: ${msg}${more}` : `${msg}${more}`;
  }
  if (detail && typeof detail === 'object' && 'message' in detail) {
    return String((detail as { message: unknown }).message);
  }
  if (status === 0) return 'Cannot reach the Schema server';
  return fallback || `Request failed (${status})`;
}

export type Query = Record<string, string | number | boolean | null | undefined>;

export function buildUrl(path: string, query?: Query): string {
  const url = `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`;
  if (!query) return url;
  const params = new URLSearchParams();
  for (const [k, val] of Object.entries(query)) {
    if (val === undefined || val === null || val === '') continue;
    params.set(k, String(val));
  }
  const qs = params.toString();
  return qs ? `${url}?${qs}` : url;
}

export interface RequestOptions {
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
}

type Listener = (error: ApiError) => void;
const unauthorizedListeners = new Set<Listener>();

/** Subscribe to 401 responses (the auth layer re-checks /auth/status). */
export function onUnauthorized(listener: Listener): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

/**
 * Optional request interceptor used only by the dev mock (`npm run dev:mock`).
 * Returning `undefined` falls through to the network.
 */
export type MockHandler = (method: string, path: string, opts: RequestOptions) => Promise<unknown> | undefined;
let mockHandler: MockHandler | null = null;
export function installMockHandler(handler: MockHandler | null) {
  mockHandler = handler;
}

export async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  if (mockHandler) {
    const mocked = mockHandler(method, path, opts);
    if (mocked !== undefined) return (await mocked) as T;
  }

  const headers: Record<string, string> = {
    Accept: 'application/json',
    'X-Schema-Client': 'web',
  };
  let body: BodyInit | undefined;
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  }

  const url = buildUrl(path, opts.query);
  let res: Response;
  try {
    res = await fetch(url, { method, headers, body, credentials: 'include', signal: opts.signal });
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    throw new ApiError(0, 'Cannot reach the Schema server', null, path);
  }

  if (res.status === 204) return undefined as T;

  const text = await res.text();
  let data: unknown = undefined;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!res.ok) {
    const detail = data && typeof data === 'object' && 'detail' in data ? (data as { detail: unknown }).detail : data;
    const err = new ApiError(res.status, messageFromDetail(res.status, detail, res.statusText), detail, path);
    if (res.status === 401 && !path.startsWith('/auth/')) {
      unauthorizedListeners.forEach((l) => l(err));
    }
    throw err;
  }
  return data as T;
}

export const api = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) => request<T>('GET', path, { query, signal }),
  post: <T>(path: string, body?: unknown, query?: Query) => request<T>('POST', path, { body: body ?? {}, query }),
  put: <T>(path: string, body: unknown) => request<T>('PUT', path, { body }),
  patch: <T>(path: string, body: unknown) => request<T>('PATCH', path, { body }),
  delete: <T = void>(path: string) => request<T>('DELETE', path),
};
