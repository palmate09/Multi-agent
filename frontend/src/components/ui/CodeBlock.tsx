import { Fragment, useEffect, useMemo, useRef, useState } from 'react';

import { cn } from '../../lib/cn';
import { highlight, LANGUAGE_LABEL } from '../../lib/highlight';
import type { Language } from '../../lib/highlight';
import { Button } from './Button';
import { Icon } from './Icon';

export interface CodeBlockProps {
  code: string;
  /** Picks the colouring. Omit for output that is not code (logs, pytest). */
  language?: Language;
  /** Shown in the toolbar. Falls back to the language name, then `title`. */
  fileName?: string;
  /** Toolbar text for output with no file name, e.g. "Server log". */
  title?: string;
  lineNumbers?: boolean;
  /** Passed to the <pre>, so a caller can make it a tabpanel. */
  preProps?: {
    id?: string;
    role?: string;
    'aria-labelledby'?: string;
    tabIndex?: number;
  };
  className?: string;
}

/** Copy to the clipboard, with a fallback for pages served over plain http
 *  where `navigator.clipboard` does not exist. */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the legacy path */
  }
  try {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

/**
 * Read-only code or log output.
 *
 * Exactly one `<pre class="code">` per block, holding the text itself, so the
 * content is selectable, searchable and testable as plain text. Line numbers
 * are CSS counters (generated content), which is why they are never copied
 * along with the code.
 */
export function CodeBlock({
  code,
  language = 'text',
  fileName,
  title,
  lineNumbers = true,
  preProps,
  className,
}: CodeBlockProps) {
  const lines = useMemo(() => highlight(code, language), [code, language]);
  const [copied, setCopied] = useState<'idle' | 'copied' | 'failed'>('idle');
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    []
  );

  const onCopy = async () => {
    const ok = await copyText(code);
    setCopied(ok ? 'copied' : 'failed');
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied('idle'), 1800);
  };

  const heading = fileName ?? title ?? LANGUAGE_LABEL[language];
  const showLanguage = language !== 'text' && heading !== LANGUAGE_LABEL[language];
  const meta = [showLanguage ? LANGUAGE_LABEL[language] : null, `${lines.length} lines`]
    .filter(Boolean)
    .join(' · ');

  return (
    <div className={cn('codeblock', className)}>
      <div className="codeblock-bar">
        <span className="codeblock-name">{heading}</span>
        <span className="codeblock-meta">{meta}</span>
        <Button
          variant="ghost"
          size="sm"
          className="codeblock-copy"
          onClick={() => void onCopy()}
          title="Copy to clipboard"
        >
          <Icon name={copied === 'copied' ? 'check' : 'copy'} size={14} />
          {copied === 'copied' ? 'Copied' : copied === 'failed' ? 'Copy failed' : 'Copy'}
        </Button>
      </div>
      <pre className={cn('code', lineNumbers && 'numbered')} {...preProps}>
        <code>
          {lines.map((line, i) => (
            <Fragment key={i}>
              <span className="ln">
                {line.map((t, j) =>
                  t.type ? (
                    <span key={j} className={`tok-${t.type}`}>
                      {t.text}
                    </span>
                  ) : (
                    t.text
                  )
                )}
              </span>
              {i < lines.length - 1 ? '\n' : null}
            </Fragment>
          ))}
        </code>
      </pre>
      <span className="visually-hidden" role="status">
        {copied === 'copied' ? 'Copied to clipboard' : ''}
      </span>
    </div>
  );
}
