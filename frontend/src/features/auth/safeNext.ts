/**
 * Where to go after signing in, from the `?next=` parameter: only a path inside this app.
 *
 * Anything that could leave the origin falls back to `/`: absolute and protocol-relative URLs,
 * backslashes (browsers read `/\evil.com` as `//evil.com`; react-router 6.30.6 advisory
 * GHSA-wrjc-x8rr-h8h6) and control characters (tabs and newlines are stripped by the URL parser,
 * so `/\t/evil.com` would become `//evil.com`).
 */
export function safeNext(raw: string | null, origin: string = globalThis.location?.origin ?? 'http://localhost'): string {
  if (!raw || !raw.startsWith('/') || raw.startsWith('//')) return '/';
  for (let i = 0; i < raw.length; i += 1) {
    const code = raw.charCodeAt(i);
    if (code < 0x20 || code === 0x7f || raw[i] === '\\') return '/';
  }
  let url: URL;
  try {
    url = new URL(raw, origin);
  } catch {
    return '/';
  }
  if (url.origin !== origin) return '/';
  const path = `${url.pathname}${url.search}${url.hash}`;
  if (!path.startsWith('/') || path.startsWith('//') || path.startsWith('/login') || path.startsWith('/setup')) {
    return '/';
  }
  return path;
}
