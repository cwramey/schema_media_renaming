import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, buildUrl, onUnauthorized } from './client';

function mockFetch(status: number, body: unknown, statusText = '') {
  const fn = vi.fn(async () => new Response(body === undefined ? null : typeof body === 'string' ? body : JSON.stringify(body), { status, statusText }));
  vi.stubGlobal('fetch', fn);
  return fn;
}

afterEach(() => vi.unstubAllGlobals());

describe('buildUrl', () => {
  it('prefixes /api and drops empty query values', () => {
    expect(buildUrl('/jobs', { status: 'review', limit: 50, offset: undefined, q: '' })).toBe('/api/jobs?status=review&limit=50');
    expect(buildUrl('fs/list', { path: '/storage/TV Shows' })).toBe('/api/fs/list?path=%2Fstorage%2FTV+Shows');
  });
});

describe('request', () => {
  it('sends credentials, the CSRF header and JSON bodies', async () => {
    const fetchFn = mockFetch(200, { ok: true });
    await api.patch('/items/3', { status: 'approved' });
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/items/3');
    expect(init.method).toBe('PATCH');
    expect(init.credentials).toBe('include');
    expect((init.headers as Record<string, string>)['X-Schema-Client']).toBe('web');
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json');
    expect(init.body).toBe('{"status":"approved"}');
  });

  it('returns undefined for 204', async () => {
    mockFetch(204, undefined);
    await expect(api.delete('/jobs/1')).resolves.toBeUndefined();
  });

  it('maps a string detail to ApiError', async () => {
    mockFetch(409, { detail: 'Job is executing' });
    const err = await api.delete('/jobs/1').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 409, message: 'Job is executing', isConflict: true, isRetryable: false });
  });

  it('summarizes FastAPI validation errors', async () => {
    mockFetch(422, { detail: [{ loc: ['body', 'paths'], msg: 'List should have at least 1 item', type: 'too_short' }, { loc: ['body', 'x'], msg: 'bad', type: 't' }] });
    const err = (await api.post('/jobs', { paths: [] }).catch((e: unknown) => e)) as ApiError;
    expect(err.message).toBe('paths: List should have at least 1 item (+1 more)');
    expect(err.validationErrors).toHaveLength(2);
  });

  it('turns network failures into a retryable status-0 error', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new TypeError('Failed to fetch'))));
    const err = (await api.get('/dashboard').catch((e: unknown) => e)) as ApiError;
    expect(err.status).toBe(0);
    expect(err.isRetryable).toBe(true);
  });

  it('notifies unauthorized listeners on 401 (except for auth routes)', async () => {
    const seen: number[] = [];
    const off = onUnauthorized((e) => seen.push(e.status));
    mockFetch(401, { detail: 'Not authenticated' });
    await api.get('/dashboard').catch(() => undefined);
    await api.get('/auth/status').catch(() => undefined);
    off();
    expect(seen).toEqual([401]);
  });
});
