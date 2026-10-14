import { useEffect, useState } from 'react';

import { EmptyState } from './ui/Card';

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
  const [active, setActive] = useState<string>('');

  // The selected file may have gone away (the Developer rewrote the
  // layout), so fall back to the first one rather than rendering
  // nothing.
  useEffect(() => {
    const names = Object.keys(files).sort();
    if (names.length === 0) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (!names.includes(active)) setActive(names[0] ?? '');
  }, [files, active]);

  const names = Object.keys(files).sort();

  if (names.length === 0) return <EmptyState>{emptyHint}</EmptyState>;

  return (
    <div>
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
              onClick={() => setActive(name)}
              onKeyDown={e => {
                if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
                e.preventDefault();
                const i = names.indexOf(name);
                const next =
                  e.key === 'ArrowRight'
                    ? names[(i + 1) % names.length]
                    : names[(i - 1 + names.length) % names.length];
                setActive(next);
                document.getElementById(`file-${next}`)?.focus();
              }}
            >
              {name}
            </button>
          );
        })}
      </div>
      <pre
        className="code"
        id="file-panel"
        role="tabpanel"
        aria-labelledby={`file-${active}`}
        tabIndex={0}
      >
        {files[active]}
      </pre>
    </div>
  );
}
