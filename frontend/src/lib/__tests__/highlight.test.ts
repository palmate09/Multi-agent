import { describe, it, expect } from 'vitest';
import { highlight, languageFor } from '../highlight';
import type { Language } from '../highlight';

const text = (lines: ReturnType<typeof highlight>) =>
  lines.map(line => line.map(t => t.text).join('')).join('\n');

const typed = (code: string, lang: Language) =>
  highlight(code, lang)
    .flat()
    .filter(t => t.type)
    .map(t => `${t.type}:${t.text}`);

describe('languageFor', () => {
  it('maps file extensions, case-insensitively, and falls back to text', () => {
    expect(languageFor('tests/test_app.py')).toBe('python');
    expect(languageFor('openapi.YML')).toBe('yaml');
    expect(languageFor('app.tsx')).toBe('javascript');
    expect(languageFor('Makefile')).toBe('text');
  });
});

describe('highlight', () => {
  // The invariant everything else depends on: colouring never changes content.
  const SAMPLES: Array<[Language, string]> = [
    ['python', 'def f(a, b=1.5e3):\n    """doc\n    more"""\n    return a  # tail\n'],
    ['python', "s = 'it\\'s'\nx = 'unterminated\n"],
    ['python', '@app.get("/x")\r\nasync def h(): ...\r\n'],
    ['yaml', '# c\nkey: "v" # t\n- item\nurl: http://x/#anchor\n\'200\':\n  description: ok\n'],
    ['json', '{"a": [1, 2.5, -3e2, true, null], "b": "unterminated'],
    ['javascript', 'const a = `x ${y}\nz`; // c\n/* b\nlock */ fetch("/x");'],
    ['html', "<!doctype html>\n<main id=\"app\" class='a'></main>\n<!-- c -->\n<p>Don't</p>"],
    ['css', '/* c */\n.a:hover { color: #fff; margin: 0 -1.5rem; }\nh1 { font: 12px/1 "x"; }'],
    ['text', 'plain\ntext'],
    ['python', ''],
  ];

  it.each(SAMPLES)('round-trips %s source exactly', (lang, code) => {
    const normalised = code.replace(/\r\n?/g, '\n');
    const expected = normalised.endsWith('\n') ? normalised.slice(0, -1) : normalised;
    expect(text(highlight(code, lang))).toBe(expected);
  });

  it('classifies python keywords, calls, strings and comments', () => {
    expect(typed('def f(x): return None  # hi', 'python')).toEqual([
      'keyword:def',
      'function:f',
      'keyword:return',
      'keyword:None',
      'comment:# hi',
    ]);
  });

  it('does not treat # inside a python string as a comment', () => {
    expect(typed('x = "a#b"', 'python')).toContain('string:"a#b"');
  });

  it('keeps a multi-line string typed on every line', () => {
    const lines = highlight('"""doc\nmore"""', 'python');
    expect(lines).toHaveLength(2);
    expect(lines[0][0].type).toBe('string');
    expect(lines[1][0].type).toBe('string');
  });

  it('tells json keys from values', () => {
    expect(typed('{"k": "v", "n": 1}', 'json')).toEqual([
      'key:"k"',
      'string:"v"',
      'key:"n"',
      'number:1',
    ]);
  });

  it('only starts a yaml comment at a line start or after whitespace', () => {
    expect(typed('url: http://x/#frag', 'yaml').some(t => t.startsWith('comment'))).toBe(false);
    expect(typed('a: 1 # note', 'yaml').some(t => t.startsWith('comment'))).toBe(true);
  });

  it('never reads digits inside a css identifier as a number', () => {
    expect(typed('h1 { color: red; }', 'css').filter(t => t.startsWith('number'))).toEqual([]);
    expect(typed('a:hover { color: red; }', 'css')).toContain('key:color');
  });

  it('colours html tag names, attributes and values separately', () => {
    expect(typed('<a href="x">', 'html')).toEqual(['keyword:a', 'key:href', 'string:"x"']);
  });

  it('does not invent a trailing empty line for a final newline', () => {
    expect(highlight('a\nb\n', 'text')).toHaveLength(2);
    expect(highlight('a\r\nb', 'python')).toHaveLength(2);
  });

  it('survives pathological input quickly', () => {
    const start = performance.now();
    for (const lang of ['python', 'javascript', 'json', 'css', 'yaml'] as const) {
      for (const s of ['"'.repeat(30000), '/*'.repeat(15000), '"""'.repeat(10000)]) {
        highlight(s, lang);
      }
    }
    expect(performance.now() - start).toBeLessThan(5000);
  });

  it('renders very large input plain rather than spending time on it', () => {
    const big = 'x = 1\n'.repeat(40000);
    const lines = highlight(big, 'python');
    expect(lines.flat().every(t => t.type === undefined)).toBe(true);
  });
});
