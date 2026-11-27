import { describe, expect, it } from 'vitest';
import {
  isInsideBlock,
  lexicalIssues,
  normalizeIssues,
  segmentTokens,
  tokenize,
  usedBindings,
  type Token,
} from './tokenizer';

const kinds = (src: string) => tokenize(src).map((t) => [t.type, t.text]);
const significant = (src: string) => tokenize(src).filter((t) => t.type !== 'ws').map((t) => `${t.type}:${t.text}`);

describe('tokenize', () => {
  it('splits literal text, folder slashes and blocks', () => {
    expect(kinds('{n} - {s00e00} - {t}')).toEqual([
      ['block-open', '{'],
      ['binding', 'n'],
      ['block-close', '}'],
      ['literal', ' - '],
      ['block-open', '{'],
      ['binding', 's00e00'],
      ['block-close', '}'],
      ['literal', ' - '],
      ['block-open', '{'],
      ['binding', 't'],
      ['block-close', '}'],
    ]);
  });

  it('marks "/" in literal text as a folder separator', () => {
    const toks = tokenize('{ny}/Season {s}/x');
    expect(toks.filter((t) => t.type === 'slash')).toHaveLength(2);
  });

  it('classifies strings, operators, methods and properties', () => {
    expect(significant("{'Season ' + s.pad(2)}")).toEqual([
      'block-open:{',
      "string:'Season '",
      'operator:+',
      'binding:s',
      'punct:.',
      'method:pad',
      'punct:(',
      'number:2',
      'punct:)',
      'block-close:}',
    ]);
    expect(significant('{episode.title}')).toContain('property:title');
    expect(significant('{plex.id}')).toEqual(['block-open:{', 'binding:plex', 'punct:.', 'property:id', 'block-close:}']);
  });

  it('treats the method name before "(" as a method even with whitespace', () => {
    expect(significant('{n.lower ()}')).toContain('method:lower');
  });

  it('recognizes keywords, ternaries and the elvis operator', () => {
    const src = "{special ? 'Specials' : 'Season ' + s.pad(2)}{x ?: null}{true}";
    const sig = significant(src);
    expect(sig).toContain('operator:?');
    expect(sig).toContain('operator::');
    expect(sig).toContain('operator:?:');
    expect(sig).toContain('keyword:null');
    expect(sig).toContain('keyword:true');
  });

  it('keeps any{…}{…} closures inside one block', () => {
    const toks = tokenize("{any{vf}{'SD'}}");
    expect(toks[0].type).toBe('block-open');
    expect(toks.filter((t) => t.type === 'closure').map((t) => t.text).join('')).toBe('{}{}');
    expect(toks[toks.length - 1].type).toBe('block-close');
    expect(toks.find((t) => t.text === 'any')?.type).toBe('keyword');
    expect(lexicalIssues(toks)).toEqual([]);
  });

  it('does not end a block at a brace inside a string', () => {
    const toks = tokenize("{'a}b'}tail");
    expect(toks.map((t) => t.type)).toEqual(['block-open', 'string', 'block-close', 'literal']);
  });

  it('handles escaped quotes inside strings', () => {
    const toks = tokenize("{'it\\'s'}");
    expect(toks[1]).toMatchObject({ type: 'string', text: "'it\\'s'" });
    expect(toks[2].type).toBe('block-close');
  });

  it('turns {{ and }} into escape tokens', () => {
    expect(kinds('a{{b}}c')).toEqual([
      ['literal', 'a'],
      ['escape', '{{'],
      ['literal', 'b'],
      ['escape', '}}'],
      ['literal', 'c'],
    ]);
  });

  it('reports unclosed blocks, unterminated strings and stray braces', () => {
    const unclosed = tokenize('{n - {t}');
    // "{n - {t}" : the inner { opens a closure, the } closes it, the outer block never closes.
    expect(unclosed[0].error).toMatch(/Unclosed/);

    const str = tokenize("{'abc}");
    expect(str.find((t) => t.type === 'string')?.error).toMatch(/Unterminated/);

    const stray = tokenize('abc}def');
    expect(lexicalIssues(stray)).toEqual([{ message: expect.stringMatching(/Unmatched/), start: 3, end: 4 }]);

    const bad = tokenize('{n # t}');
    expect(lexicalIssues(bad)[0]).toMatchObject({ start: 3, end: 4 });
  });

  it('covers the whole input with contiguous, non-overlapping tokens', () => {
    const inputs = [
      "{n} ({y})/{special ? 'Specials' : 'Season ' + s.pad(2)}/{n} - {s00e00}{' - ' + t}",
      "{[vf, vc, ac]}{any{x}{y}}{{literal}}",
      "{'unterminated",
      '}}}{{{',
      '',
      '日本語/{n}',
    ];
    for (const src of inputs) {
      const toks = tokenize(src);
      let pos = 0;
      for (const t of toks) {
        expect(t.start).toBe(pos);
        expect(src.slice(t.start, t.end)).toBe(t.text);
        pos = t.end;
      }
      expect(pos).toBe(src.length);
    }
  });
});

describe('queries', () => {
  it('collects used bindings', () => {
    const used = usedBindings(tokenize("{n} - {s00e00}{' - ' + t}{plex.id}{n.lower()}"));
    expect([...used].sort()).toEqual(['n', 'plex', 's00e00', 't']);
  });

  it('knows whether the caret is inside a block', () => {
    const src = 'ab{n}cd';
    expect(isInsideBlock(src, 0)).toBe(false);
    expect(isInsideBlock(src, 2)).toBe(false); // before "{"
    expect(isInsideBlock(src, 3)).toBe(true); // after "{"
    expect(isInsideBlock(src, 4)).toBe(true); // before "}"
    expect(isInsideBlock(src, 5)).toBe(false); // after "}"
    expect(isInsideBlock('{n} {t', 6)).toBe(true); // unclosed block
  });
});

describe('segmentTokens (highlighting with issue squiggles)', () => {
  const join = (segs: { text: string }[]) => segs.map((s) => s.text).join('');

  it('returns one segment per token when there are no issues', () => {
    const src = '{n} - {t}';
    const toks = tokenize(src);
    const segs = segmentTokens(toks, [], src.length);
    expect(join(segs)).toBe(src);
    expect(segs).toHaveLength(toks.length);
    expect(segs.every((s) => s.severity === undefined && s.messages.length === 0)).toBe(true);
  });

  it('splits a token at issue boundaries and tags the covered part', () => {
    const src = '{seriesName}';
    const toks = tokenize(src);
    // warning on "Name" only (offsets 7..11)
    const segs = segmentTokens(toks, [{ start: 7, end: 11, severity: 'warning', message: 'unsupported' }], src.length);
    expect(join(segs)).toBe(src);
    const binding = segs.filter((s) => s.type === 'binding');
    expect(binding.map((s) => [s.text, s.severity])).toEqual([
      ['series', undefined],
      ['Name', 'warning'],
    ]);
    expect(binding[1].messages).toEqual(['unsupported']);
  });

  it('error wins over warning where issues overlap', () => {
    const src = '{abc}';
    const segs = segmentTokens(
      tokenize(src),
      [
        { start: 1, end: 4, severity: 'warning', message: 'w' },
        { start: 2, end: 3, severity: 'error', message: 'e' },
      ],
      src.length,
    );
    const b = segs.find((s) => s.text === 'b');
    expect(b?.severity).toBe('error');
    expect(b?.messages).toEqual(['w', 'e']);
    expect(segs.find((s) => s.text === 'a')?.severity).toBe('warning');
  });

  it('widens zero-width and out-of-range issues so they stay visible', () => {
    expect(normalizeIssues(5, [{ start: 2, end: 2, severity: 'error', message: 'x' }])[0]).toMatchObject({ start: 2, end: 3 });
    expect(normalizeIssues(5, [{ start: 9, end: 12, severity: 'error', message: 'x' }])[0]).toMatchObject({ start: 4, end: 5 });
    expect(normalizeIssues(0, [{ start: 0, end: 0, severity: 'error', message: 'x' }])).toEqual([]);

    const src = '{n';
    const segs = segmentTokens(tokenize(src), [{ start: 2, end: 2, severity: 'error', message: 'eof' }], src.length);
    expect(segs[segs.length - 1]).toMatchObject({ text: 'n', severity: 'error' });
  });

  it('merges adjacent pieces with identical state', () => {
    const tokens: Token[] = [
      { type: 'literal', text: 'ab', start: 0, end: 2 },
      { type: 'literal', text: 'cd', start: 2, end: 4 },
    ];
    expect(segmentTokens(tokens, [], 4)).toEqual([{ text: 'abcd', start: 0, end: 4, type: 'literal', severity: undefined, messages: [] }]);
  });
});
