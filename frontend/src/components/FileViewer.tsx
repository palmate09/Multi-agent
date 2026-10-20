import { useState } from 'react';

import { languageFor } from '../lib/highlight';
import { CodeBlock } from './ui/CodeBlock';
import { EmptyState } from './ui/Card';
import { Icon } from './ui/Icon';

interface Props {
  files: Record<string, string>;
  emptyHint?: string;
}

/**
 * Tabbed read-only viewer for generated files.
 *
 * The tab strip is a `tablist` for the same reason the run-detail
 * strip is: it is a set of mutually exclusive views, so it is one
 * tab stop with arrow-key movement rather than ten.
 */
export function FileViewer({ files, emptyHint = 'No files generated.' }: Props) {
  const [chosen, setChosen] = useState<string>('');

  const names = Object.keys(files).sort();
  if (names.length === 0) return <EmptyState>{emptyHint}</EmptyState>;

  // The chosen file may have gone away (the Developer rewrote the layout), so
  // fall back to the first one rather than rendering nothing. Derived during
  // render, so there is no frame where the panel is empty.
  const active = names.includes(chosen) ? chosen : names[0];

  return (
    <div className="fileviewer">
      <div className="file-list" role="tablist" aria-label="Generated files">
        {names.map(name => {
          const selected = name === active;
          return (
            <button
              key={name}
              id={`file-${name}`}
              role="tab"
              type="button"
              aria-selected={selected}
              aria-controls="file-panel"
              tabIndex={selected ? 0 : -1}
              className={selected ? 'active' : ''}
              onClick={() => setChosen(name)}
              onKeyDown={e => {
                if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
                e.preventDefault();
                const i = names.indexOf(name);
                const next =
                  e.key === 'ArrowRight'
                    ? names[(i + 1) % names.length]
                    : names[(i - 1 + names.length) % names.length];
                setChosen(next);
                document.getElementById(`file-${next}`)?.focus();
              }}
            >
              <Icon name="file" size={14} />
              {name}
            </button>
          );
        })}
      </div>
      <CodeBlock
        code={files[active]}
        language={languageFor(active)}
        preProps={{
          id: 'file-panel',
          role: 'tabpanel',
          'aria-labelledby': `file-${active}`,
          tabIndex: 0,
        }}
      />
    </div>
  );
}
