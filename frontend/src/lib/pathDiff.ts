/**
 * Path splitting and "what changed" highlighting for proposed destinations (SPEC §9.3):
 * folders are dimmed, and inside the new filename the words that differ from the
 * original filename are highlighted.
 */

export interface PathParts {
  /** Folder segments (without separators). */
  folders: string[];
  /** Separator used in the input ("/" or "\\"). */
  sep: '/' | '\\';
  /** Leading root such as "/", "C:\\", "\\\\server\\" (empty for relative paths). */
  root: string;
  /** Filename without extension. */
  stem: string;
  /** Extension without the dot ("" when none). Multi-part sidecar suffixes stay in the stem. */
  ext: string;
}

export function splitPath(path: string): PathParts {
  const sep: '/' | '\\' = path.includes('/') || !path.includes('\\') ? '/' : '\\';
  let rest = path;
  let root = '';
  const drive = /^[A-Za-z]:[\\/]/.exec(rest);
  if (drive) {
    root = drive[0];
    rest = rest.slice(root.length);
  } else if (rest.startsWith('\\\\') || rest.startsWith('//')) {
    root = rest.slice(0, 2);
    rest = rest.slice(2);
  } else if (rest.startsWith('/') || rest.startsWith('\\')) {
    root = rest[0];
    rest = rest.slice(1);
  }
  const parts = rest.split(/[\\/]/).filter((p) => p.length > 0);
  const filename = parts.pop() ?? '';
  const dot = filename.lastIndexOf('.');
  const hasExt = dot > 0 && dot < filename.length - 1 && /^[A-Za-z0-9]{1,5}$/.test(filename.slice(dot + 1));
  return {
    folders: parts,
    sep,
    root,
    stem: hasExt ? filename.slice(0, dot) : filename,
    ext: hasExt ? filename.slice(dot + 1) : '',
  };
}

export function basename(path: string): string {
  const p = splitPath(path);
  return p.ext ? `${p.stem}.${p.ext}` : p.stem;
}

export function dirname(path: string): string {
  const p = splitPath(path);
  return p.root + p.folders.join(p.sep);
}

/** Strip `root` from `path` when `path` lives under it (separator- and case-tolerant). */
export function relativeTo(path: string, root: string | null | undefined): string {
  if (!root) return path;
  const norm = (s: string) => s.replace(/\\/g, '/').replace(/\/+$/, '');
  const p = norm(path);
  const r = norm(root);
  if (p.toLowerCase().startsWith(`${r.toLowerCase()}/`)) return path.slice(r.length + 1);
  return path;
}

/* ------------------------------------------------------------------ word diff */

export type WordTokenKind = 'word' | 'sep';

export interface WordToken {
  text: string;
  kind: WordTokenKind;
}

const WORD_RE = /[\p{L}\p{N}]+|[^\p{L}\p{N}]+/gu;

export function wordTokens(s: string): WordToken[] {
  return (s.match(WORD_RE) ?? []).map((text) => ({
    text,
    kind: /[\p{L}\p{N}]/u.test(text) ? 'word' : 'sep',
  }));
}

const normWord = (w: string) => w.toLowerCase().replace(/^0+(?=\d)/, '');

/** Indices of `b` words that belong to a longest common subsequence with `a` (case-insensitive). */
export function lcsKeep(a: readonly string[], b: readonly string[]): Set<number> {
  const A = a.map(normWord);
  const B = b.map(normWord);
  const m = A.length;
  const n = B.length;
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array<number>(n + 1).fill(0));
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const keep = new Set<number>();
  let i = 0;
  let j = 0;
  while (i < m && j < n) {
    if (A[i] === B[j]) {
      keep.add(j);
      i += 1;
      j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      i += 1;
    } else {
      j += 1;
    }
  }
  return keep;
}

/* ------------------------------------------------------------------ path diff */

export type DiffKind = 'root' | 'folder' | 'sep' | 'same' | 'changed' | 'ext';

export interface DiffSegment {
  text: string;
  kind: DiffKind;
}

/**
 * Segments of `proposed` for display. Folders (and the root) are their own kinds so they can
 * be dimmed; the filename stem is split into words marked `same` (present in the original
 * filename) or `changed`; separators inside the stem are `sep` unless they sit between
 * changed words. The extension is `ext`, or `changed` when it differs.
 */
export function diffPath(originalFilename: string, proposed: string): DiffSegment[] {
  const orig = splitPath(originalFilename);
  const prop = splitPath(proposed);
  const out: DiffSegment[] = [];

  if (prop.root) out.push({ text: prop.root, kind: 'root' });
  for (const f of prop.folders) {
    out.push({ text: f, kind: 'folder' });
    out.push({ text: prop.sep, kind: 'sep' });
  }

  const origWords = wordTokens(orig.stem)
    .filter((t) => t.kind === 'word')
    .map((t) => t.text);
  const tokens = wordTokens(prop.stem);
  const words = tokens.filter((t) => t.kind === 'word').map((t) => t.text);
  const keep = lcsKeep(origWords, words);

  // word index -> changed?
  const changed: boolean[] = words.map((_, idx) => !keep.has(idx));
  let w = 0;
  tokens.forEach((t) => {
    if (t.kind === 'word') {
      out.push({ text: t.text, kind: changed[w] ? 'changed' : 'same' });
      w += 1;
      return;
    }
    // separator: changed only when flanked by changed words (or a changed word and an edge)
    const prevChanged = w > 0 ? changed[w - 1] : null;
    const nextChanged = w < words.length ? changed[w] : null;
    const flanked =
      (prevChanged === true && nextChanged === true) ||
      (prevChanged === null && nextChanged === true) ||
      (prevChanged === true && nextChanged === null);
    out.push({ text: t.text, kind: flanked ? 'changed' : 'sep' });
  });

  const merged = mergeSegments(out);
  if (prop.ext) {
    // Kept as its own segment (never merged into the stem).
    const same = prop.ext.toLowerCase() === orig.ext.toLowerCase();
    merged.push({ text: `.${prop.ext}`, kind: same ? 'ext' : 'changed' });
  }
  return merged;
}

/** Merge adjacent segments of the same kind (keeps folder segments separate for styling). */
export function mergeSegments(segs: readonly DiffSegment[]): DiffSegment[] {
  const out: DiffSegment[] = [];
  for (const s of segs) {
    const last = out[out.length - 1];
    if (last && last.kind === s.kind && s.kind !== 'folder' && s.kind !== 'sep') {
      last.text += s.text;
    } else {
      out.push({ ...s });
    }
  }
  return out;
}

/** Fraction of proposed filename words that were kept from the original (0..1). */
export function similarity(originalFilename: string, proposed: string): number {
  const segs = diffPath(originalFilename, proposed).filter((s) => s.kind === 'same' || s.kind === 'changed');
  const total = segs.reduce((n, s) => n + s.text.length, 0);
  if (total === 0) return 1;
  const same = segs.filter((s) => s.kind === 'same').reduce((n, s) => n + s.text.length, 0);
  return same / total;
}
