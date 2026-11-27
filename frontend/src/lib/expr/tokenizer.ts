/**
 * Tokenizer for Schema format templates (SPEC §8.1–8.2), used for syntax highlighting,
 * squiggles and cursor-aware insertion in the format builder. It never throws: malformed
 * input yields tokens with an `error` message so the editor can still render it.
 *
 * Template:  literal text with `{expr}` blocks; `{{` / `}}` are literal braces; `/` makes folders.
 * Expr:      strings, numbers, identifiers (bindings), `.method(...)`, `.property`, operators,
 *            `any{…}{…}` / `allOf{…}` closures (nested braces).
 */

export type TokenType =
  | 'literal' // plain template text
  | 'slash' // folder separator in literal text
  | 'escape' // {{ or }}
  | 'block-open' // { starting an expression block
  | 'block-close' // } ending it
  | 'closure' // { } of any{…}/allOf{…} inside an expression
  | 'ws'
  | 'string'
  | 'number'
  | 'binding' // identifier resolved as a binding (n, s00e00, plex …)
  | 'property' // .name after a dot, not called
  | 'method' // .name( … )
  | 'keyword' // true false null any allOf
  | 'operator'
  | 'punct' // . , ( ) [ ]
  | 'unknown';

export interface Token {
  type: TokenType;
  text: string;
  start: number;
  end: number;
  /** Set when the token is malformed (unterminated string, unclosed block, stray brace …). */
  error?: string;
}

const KEYWORDS = new Set(['true', 'false', 'null', 'any', 'allOf']);
const OPERATORS = ['?:', '==', '!=', '<=', '>=', '&&', '||', '?', ':', '+', '-', '*', '%', '/', '!', '<', '>', '='];
const PUNCT = new Set(['.', ',', '(', ')', '[', ']']);
const IDENT_START = /[A-Za-z_$À-￿]/;
const IDENT_PART = /[A-Za-z0-9_$À-￿]/;

export function tokenize(src: string): Token[] {
  const out: Token[] = [];
  let i = 0;
  const n = src.length;

  const push = (type: TokenType, start: number, end: number, error?: string) => {
    const tok: Token = { type, text: src.slice(start, end), start, end };
    if (error) tok.error = error;
    out.push(tok);
  };

  while (i < n) {
    const ch = src[i];

    // ---- literal context
    if (ch === '{' && src[i + 1] === '{') {
      push('escape', i, i + 2);
      i += 2;
      continue;
    }
    if (ch === '}' && src[i + 1] === '}') {
      push('escape', i, i + 2);
      i += 2;
      continue;
    }
    if (ch === '}') {
      push('unknown', i, i + 1, 'Unmatched "}" (use "}}" for a literal brace)');
      i += 1;
      continue;
    }
    if (ch === '/') {
      push('slash', i, i + 1);
      i += 1;
      continue;
    }
    if (ch !== '{') {
      const start = i;
      while (i < n && src[i] !== '{' && src[i] !== '}' && src[i] !== '/') i += 1;
      push('literal', start, i);
      continue;
    }

    // ---- expression block
    const openIndex = out.length;
    push('block-open', i, i + 1);
    i += 1;
    let depth = 0;
    let closed = false;
    let prevSignificant: Token | null = null;

    while (i < n) {
      const c = src[i];

      if (c === '}') {
        if (depth === 0) {
          push('block-close', i, i + 1);
          i += 1;
          closed = true;
          break;
        }
        depth -= 1;
        push('closure', i, i + 1);
        i += 1;
        prevSignificant = out[out.length - 1];
        continue;
      }
      if (c === '{') {
        depth += 1;
        push('closure', i, i + 1);
        i += 1;
        prevSignificant = out[out.length - 1];
        continue;
      }
      if (/\s/.test(c)) {
        const s = i;
        while (i < n && /\s/.test(src[i])) i += 1;
        push('ws', s, i);
        continue;
      }
      if (c === "'" || c === '"') {
        const s = i;
        i += 1;
        let terminated = false;
        while (i < n) {
          if (src[i] === '\\') {
            i += 2;
            continue;
          }
          if (src[i] === c) {
            i += 1;
            terminated = true;
            break;
          }
          i += 1;
        }
        i = Math.min(i, n);
        push('string', s, i, terminated ? undefined : 'Unterminated string');
        prevSignificant = out[out.length - 1];
        continue;
      }
      if (/[0-9]/.test(c)) {
        const s = i;
        while (i < n && /[0-9]/.test(src[i])) i += 1;
        if (src[i] === '.' && /[0-9]/.test(src[i + 1] ?? '')) {
          i += 1;
          while (i < n && /[0-9]/.test(src[i])) i += 1;
        }
        push('number', s, i);
        prevSignificant = out[out.length - 1];
        continue;
      }
      if (IDENT_START.test(c)) {
        const s = i;
        while (i < n && IDENT_PART.test(src[i])) i += 1;
        const word = src.slice(s, i);
        let type: TokenType;
        if (prevSignificant?.type === 'punct' && prevSignificant.text === '.') {
          let j = i;
          while (j < n && /\s/.test(src[j])) j += 1;
          type = src[j] === '(' ? 'method' : 'property';
        } else if (KEYWORDS.has(word)) {
          type = 'keyword';
        } else {
          type = 'binding';
        }
        push(type, s, i);
        prevSignificant = out[out.length - 1];
        continue;
      }
      const op = OPERATORS.find((o) => src.startsWith(o, i));
      if (op) {
        push('operator', i, i + op.length);
        i += op.length;
        prevSignificant = out[out.length - 1];
        continue;
      }
      if (PUNCT.has(c)) {
        push('punct', i, i + 1);
        i += 1;
        prevSignificant = out[out.length - 1];
        continue;
      }
      push('unknown', i, i + 1, `Unexpected character "${c}"`);
      i += 1;
      prevSignificant = out[out.length - 1];
    }

    if (!closed) {
      out[openIndex] = { ...out[openIndex], error: 'Unclosed "{" block' };
    }
  }
  return out;
}

/* ------------------------------------------------------------------ queries */

/** Binding names referenced by the template (first identifier of each dotted chain). */
export function usedBindings(tokens: readonly Token[]): Set<string> {
  const used = new Set<string>();
  for (const t of tokens) if (t.type === 'binding') used.add(t.text);
  return used;
}

/** True when `offset` (a caret position) sits inside an expression block. */
export function isInsideBlock(src: string, offset: number): boolean {
  const tokens = tokenize(src);
  let inside = false;
  for (const t of tokens) {
    if (t.start >= offset) break;
    if (t.type === 'block-open') inside = true;
    else if (t.type === 'block-close') inside = false;
  }
  return inside;
}

/** Problems the tokenizer itself can detect (shown before the server validates). */
export function lexicalIssues(tokens: readonly Token[]): { message: string; start: number; end: number }[] {
  return tokens.filter((t) => t.error).map((t) => ({ message: t.error as string, start: t.start, end: t.end }));
}

/* ------------------------------------------------------------------ highlighting with issues */

export interface IssueRange {
  start: number;
  end: number;
  severity: 'error' | 'warning';
  message: string;
}

export interface Segment {
  text: string;
  start: number;
  end: number;
  type: TokenType;
  /** Highest severity of the issues covering this segment. */
  severity?: 'error' | 'warning';
  messages: string[];
}

/** Clamp issue offsets to the text; zero-width issues widen to one character so they stay visible. */
export function normalizeIssues(textLength: number, issues: readonly IssueRange[]): IssueRange[] {
  if (textLength === 0) return [];
  return issues.map((iss) => {
    let start = Math.max(0, Math.min(iss.start, textLength));
    let end = Math.max(start, Math.min(iss.end, textLength));
    if (end === start) {
      if (start >= textLength) start = textLength - 1;
      end = start + 1;
    }
    return { ...iss, start, end };
  });
}

/**
 * Split tokens at issue boundaries so each segment has one token type and one issue state.
 * Adjacent pieces with identical state are merged.
 */
export function segmentTokens(tokens: readonly Token[], issues: readonly IssueRange[], textLength: number): Segment[] {
  const norm = normalizeIssues(textLength, issues);
  const cuts = new Set<number>();
  for (const t of tokens) {
    cuts.add(t.start);
    cuts.add(t.end);
  }
  for (const iss of norm) {
    cuts.add(iss.start);
    cuts.add(iss.end);
  }
  const points = [...cuts].filter((p) => p >= 0 && p <= textLength).sort((a, b) => a - b);

  const segments: Segment[] = [];
  let ti = 0;
  for (let k = 0; k < points.length - 1; k++) {
    const a = points[k];
    const b = points[k + 1];
    if (a === b) continue;
    while (ti < tokens.length && tokens[ti].end <= a) ti += 1;
    const tok = tokens[ti];
    if (!tok || tok.start > a) continue;
    const covering = norm.filter((iss) => iss.start < b && iss.end > a);
    const severity = covering.some((c) => c.severity === 'error')
      ? 'error'
      : covering.length > 0
        ? 'warning'
        : undefined;
    const messages = covering.map((c) => c.message);
    const text = tok.text.slice(a - tok.start, b - tok.start);
    const last = segments[segments.length - 1];
    if (
      last &&
      last.type === tok.type &&
      last.end === a &&
      last.severity === severity &&
      last.messages.join('\n') === messages.join('\n')
    ) {
      last.text += text;
      last.end = b;
    } else {
      segments.push({ text, start: a, end: b, type: tok.type, severity, messages });
    }
  }
  return segments;
}
