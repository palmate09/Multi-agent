/**
 * A small, dependency-free syntax highlighter for the generated files.
 *
 * Scope is deliberate. The console shows Python, YAML, JSON and a little
 * HTML/CSS/JS written by the pipeline; it does not need a grammar-accurate
 * highlighter, and a real one (Prism, Shiki) is a dependency the lockfile and
 * the bundle would both have to carry. This does the one job that matters for
 * reading code quickly — make comments, strings, keywords and numbers
 * distinguishable — in about a hundred lines.
 *
 * Invariant (covered by tests): joining every token's `text` gives back the
 * input exactly. Highlighting can therefore never change what a person copies
 * or what a test finds in the page; it only decides how a span is coloured.
 */

export type TokenType = 'keyword' | 'string' | 'comment' | 'number' | 'function' | 'key';

export interface Token {
  text: string;
  type?: TokenType;
}

export type Language = 'python' | 'javascript' | 'json' | 'yaml' | 'html' | 'css' | 'text';

export const LANGUAGE_LABEL: Record<Language, string> = {
  python: 'Python',
  javascript: 'JavaScript',
  json: 'JSON',
  yaml: 'YAML',
  html: 'HTML',
  css: 'CSS',
  text: 'Text',
};

/** Past this, highlighting costs more than it is worth; render plain. */
const MAX_HIGHLIGHT_CHARS = 150_000;

export function languageFor(fileName: string): Language {
  const ext = fileName.toLowerCase().split('.').pop() ?? '';
  switch (ext) {
    case 'py':
      return 'python';
    case 'js':
    case 'jsx':
    case 'mjs':
    case 'cjs':
    case 'ts':
    case 'tsx':
      return 'javascript';
    case 'json':
      return 'json';
    case 'yaml':
    case 'yml':
      return 'yaml';
    case 'html':
    case 'htm':
      return 'html';
    case 'css':
      return 'css';
    default:
      return 'text';
  }
}

/* ---------- rule-based tokenizer ----------------------------------------- */

/** `plain` consumes text without colouring it — used so that, say, the `1` in
 *  a CSS identifier `h1` is never mistaken for a number. */
type RuleType = TokenType | 'plain';
type Rule = readonly [RuleType, RegExp];

/**
 * Every rule is tried at every position and the earliest, then the first-listed,
 * wins. Rules must be written with non-capturing groups only: each rule is
 * wrapped in one capturing group so the matched alternative can be identified.
 * Use `(?![\s\S])` for "end of input" — `$` would match at every line end
 * under the `m` flag.
 */
function compile(rules: readonly Rule[]) {
  return {
    rules,
    regex: new RegExp(rules.map(([, re]) => `(${re.source})`).join('|'), 'gm'),
  };
}

const PY = compile([
  ['comment', /#[^\n]*/],
  [
    'string',
    /(?:\b[rRbBuUfF]{1,2})?(?:"""[\s\S]*?(?:"""|(?![\s\S]))|'''[\s\S]*?(?:'''|(?![\s\S])))/,
  ],
  ['string', /(?:\b[rRbBuUfF]{1,2})?(?:"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?)/],
  ['function', /@[A-Za-z_][\w.]*/],
  [
    'keyword',
    /\b(?:and|as|assert|async|await|break|case|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|match|nonlocal|not|or|pass|raise|return|try|while|with|yield|True|False|None)\b/,
  ],
  ['number', /\b(?:0[xX][\da-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b/],
  ['function', /[A-Za-z_]\w*(?=\()/],
  ['plain', /[A-Za-z_]\w*/],
]);

const JS = compile([
  ['comment', /\/\/[^\n]*|\/\*[\s\S]*?(?:\*\/|(?![\s\S]))/],
  ['string', /"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?|`(?:\\[\s\S]|[^`\\])*`?/],
  [
    'keyword',
    /\b(?:as|async|await|break|case|catch|class|const|continue|default|delete|do|else|enum|export|extends|finally|for|from|function|if|implements|import|in|instanceof|interface|let|new|of|private|protected|public|readonly|return|static|super|switch|this|throw|try|type|typeof|var|void|while|with|yield|true|false|null|undefined)\b/,
  ],
  ['number', /\b(?:0[xX][\da-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b/],
  ['function', /[A-Za-z_$][\w$]*(?=\()/],
  ['plain', /[A-Za-z_$][\w$]*/],
]);

const JSON_ = compile([
  ['key', /"(?:\\.|[^"\\\n])*"(?=[ \t]*:)/],
  ['string', /"(?:\\.|[^"\\\n])*"?/],
  ['keyword', /\b(?:true|false|null)\b/],
  ['number', /-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/],
]);

const YAML = compile([
  // A `#` only starts a comment at a line start or after whitespace.
  ['comment', /(?:^|[ \t])#[^\n]*/],
  ['key', /^[ \t]*(?:-[ \t]+)?[^\s#:-][^:#\n]*(?=:(?:[ \t]|$))/],
  ['string', /"(?:\\.|[^"\\\n])*"?|'[^'\n]*'?/],
  ['keyword', /\b(?:true|false|null)\b/],
  ['number', /\b\d+(?:\.\d+)?\b/],
]);

const CSS = compile([
  ['comment', /\/\*[\s\S]*?(?:\*\/|(?![\s\S]))/],
  ['string', /"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?/],
  ['keyword', /@[\w-]+/],
  // A property is a name followed by `:` and a value that ends in `;` or `}`
  // before any `{` — which is what tells `color: red;` from `a:hover {`.
  ['key', /[A-Za-z-][\w-]*(?=[ \t]*:[^;{}]*[;}])/],
  ['plain', /[A-Za-z_-][\w-]*/],
  [
    'number',
    /#[\da-fA-F]{3,8}\b|-?(?:\d+\.?\d*|\.\d+)(?:px|rem|em|vh|vw|vmin|vmax|ch|ms|s|deg|fr|%)?/,
  ],
]);

function runRules(code: string, lang: { rules: readonly Rule[]; regex: RegExp }): Token[] {
  const tokens: Token[] = [];
  const { rules, regex } = lang;
  regex.lastIndex = 0;
  let last = 0;
  let m: RegExpExecArray | null;

  while ((m = regex.exec(code)) !== null) {
    if (m[0].length === 0) {
      // No rule should match nothing, but an infinite loop is not worth the risk.
      regex.lastIndex++;
      continue;
    }
    if (m.index > last) tokens.push({ text: code.slice(last, m.index) });

    let type: RuleType = 'plain';
    for (let i = 0; i < rules.length; i++) {
      if (m[i + 1] !== undefined) {
        type = rules[i][0];
        break;
      }
    }
    tokens.push(type === 'plain' ? { text: m[0] } : { text: m[0], type });
    last = m.index + m[0].length;
  }
  if (last < code.length) tokens.push({ text: code.slice(last) });
  return tokens;
}

/* ---------- HTML: tags need their insides coloured separately ------------- */

const HTML_OUTER = /<!--[\s\S]*?(?:-->|(?![\s\S]))|<\/?[A-Za-z][^>]*>?/g;
const HTML_INNER = /(<\/?)([A-Za-z][\w:-]*)|([\w:@.-]+)(?=\s*=)|("[^"]*"|'[^']*')/g;

function tokenizeHtml(code: string): Token[] {
  const tokens: Token[] = [];
  let last = 0;
  HTML_OUTER.lastIndex = 0;
  let m: RegExpExecArray | null;

  while ((m = HTML_OUTER.exec(code)) !== null) {
    if (m.index > last) tokens.push({ text: code.slice(last, m.index) });
    const piece = m[0];

    if (piece.startsWith('<!--')) {
      tokens.push({ text: piece, type: 'comment' });
    } else {
      let at = 0;
      HTML_INNER.lastIndex = 0;
      let t: RegExpExecArray | null;
      while ((t = HTML_INNER.exec(piece)) !== null) {
        if (t.index > at) tokens.push({ text: piece.slice(at, t.index) });
        if (t[2] !== undefined) {
          tokens.push({ text: t[1] });
          tokens.push({ text: t[2], type: 'keyword' });
        } else if (t[3] !== undefined) {
          tokens.push({ text: t[0], type: 'key' });
        } else {
          tokens.push({ text: t[0], type: 'string' });
        }
        at = t.index + t[0].length;
      }
      if (at < piece.length) tokens.push({ text: piece.slice(at) });
    }
    last = m.index + piece.length;
  }
  if (last < code.length) tokens.push({ text: code.slice(last) });
  return tokens;
}

/* ---------- public API ---------------------------------------------------- */

function tokenize(code: string, language: Language): Token[] {
  if (code.length > MAX_HIGHLIGHT_CHARS) return [{ text: code }];
  switch (language) {
    case 'python':
      return runRules(code, PY);
    case 'javascript':
      return runRules(code, JS);
    case 'json':
      return runRules(code, JSON_);
    case 'yaml':
      return runRules(code, YAML);
    case 'css':
      return runRules(code, CSS);
    case 'html':
      return tokenizeHtml(code);
    default:
      return [{ text: code }];
  }
}

/**
 * Tokens grouped into lines, so a line-numbered view can give each line its
 * own element. A token that spans lines (a docstring, a block comment) is
 * split at each newline and keeps its type on every piece.
 *
 * A single trailing newline does not produce a phantom empty last line.
 */
export function highlight(code: string, language: Language): Token[][] {
  const text = code.replace(/\r\n?/g, '\n');
  const body = text.endsWith('\n') ? text.slice(0, -1) : text;

  const lines: Token[][] = [[]];
  for (const token of tokenize(body, language)) {
    const pieces = token.text.split('\n');
    pieces.forEach((piece, i) => {
      if (i > 0) lines.push([]);
      if (piece === '') return;
      lines[lines.length - 1].push(
        token.type ? { text: piece, type: token.type } : { text: piece }
      );
    });
  }
  return lines;
}
