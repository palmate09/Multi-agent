import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';

import type { RunSummary } from '../types';
import { statusTone } from '../types';
import { formatDateTime } from '../lib/format';
import { cn } from '../lib/cn';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { ConfirmModal } from './ConfirmModal';
import { EmptyState } from './ui/Card';
import { Icon } from './ui/Icon';

type Filter = 'all' | 'active' | 'accepted' | 'failed';

const ACTIVE = new Set(['queued', 'running', 'stopping', 'tests_green']);

function matches(run: RunSummary, filter: Filter): boolean {
  switch (filter) {
    case 'active':
      return ACTIVE.has(run.status);
    case 'accepted':
      return run.status.startsWith('accepted');
    case 'failed':
      return statusTone(run.status) === 'bad';
    default:
      return true;
  }
}

interface Props {
  runs: RunSummary[];
  selected: string | null;
  onSelect: (_id: string) => void;
  onDelete: (_id: string) => void;
  onRefresh: () => void;
  /** Leaves the current run and opens the requirement form. */
  onNew: () => void;
  /** Whether the sidebar drawer is open (mobile). */
  open?: boolean;
  /** Called when the sidebar should close (e.g. after selecting a run). */
  onClose?: () => void;
  /** Pinned to the bottom of the sidebar: system status. */
  footer?: ReactNode;
}

/**
 * The run history.
 *
 * Each row is a plain container with two sibling controls — the row's own
 * button, and the delete button beside it — so neither nests inside the other
 * (a button inside a button announces twice and disagrees on key handling).
 *
 * On a phone the list is a drawer. While it is closed it is `visibility:
 * hidden`, so its links are not in the tab order; Escape closes it.
 */
export function RunList({
  runs,
  selected,
  onSelect,
  onDelete,
  onRefresh,
  onNew,
  open = false,
  onClose,
  footer,
}: Props) {
  const [pendingDelete, setPendingDelete] = useState<RunSummary | null>(null);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<Filter>('all');

  useEffect(() => {
    if (!open || !onClose) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const counts = useMemo(
    () => ({
      all: runs.length,
      active: runs.filter(r => matches(r, 'active')).length,
      accepted: runs.filter(r => matches(r, 'accepted')).length,
      failed: runs.filter(r => matches(r, 'failed')).length,
    }),
    [runs]
  );

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return runs.filter(
      r => matches(r, filter) && (!q || `${r.requirement} ${r.run_id}`.toLowerCase().includes(q))
    );
  }, [runs, query, filter]);

  // Close the mobile drawer when a run is selected
  const handleSelect = (id: string) => {
    onSelect(id);
    onClose?.();
  };

  const filtering = query.trim() !== '' || filter !== 'all';

  return (
    <nav id="sidebar" className={cn('sidebar', open && 'open')} aria-label="Pipeline runs">
      <div className="side-top">
        <Button
          variant="primary"
          className="side-new"
          onClick={() => {
            onNew();
            onClose?.();
          }}
        >
          <Icon name="plus" />
          New run
        </Button>
      </div>

      <div className="side-head">
        <h2 className="side-title">
          Runs <span className="side-count">{runs.length}</span>
        </h2>
        <Button
          variant="ghost"
          size="sm"
          className="icon-btn"
          onClick={onRefresh}
          aria-label="Refresh run list"
          title="Refresh"
        >
          <Icon name="refresh" size={15} />
        </Button>
      </div>

      {runs.length > 0 && (
        <div className="side-tools">
          <div className="search">
            <Icon name="search" size={15} className="search-icon" />
            <input
              type="search"
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="Search runs"
              aria-label="Search runs"
            />
          </div>
          <div className="chips" role="group" aria-label="Filter runs by status">
            {(
              [
                ['all', 'All'],
                ['active', 'Active'],
                ['accepted', 'Accepted'],
                ['failed', 'Failed'],
              ] as Array<[Filter, string]>
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                className="chip"
                aria-pressed={filter === id}
                onClick={() => setFilter(id)}
              >
                {label}
                <span className="chip-count">{counts[id]}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {runs.length === 0 && (
        <EmptyState>No runs yet. Describe an API to start the first one.</EmptyState>
      )}

      {runs.length > 0 && visible.length === 0 && (
        <EmptyState
          action={
            filtering ? (
              <Button
                size="sm"
                onClick={() => {
                  setQuery('');
                  setFilter('all');
                }}
              >
                Clear search and filter
              </Button>
            ) : undefined
          }
        >
          No runs match.
        </EmptyState>
      )}

      <ul className="run-list">
        {visible.map(run => {
          const active = selected === run.run_id;
          const tone = statusTone(run.status);
          const moving = run.status === 'running' || run.status === 'stopping';

          return (
            <li key={run.run_id} className={cn('run-item', active && 'active')}>
              <button
                type="button"
                className="run-open"
                onClick={() => handleSelect(run.run_id)}
                aria-current={active ? 'true' : undefined}
              >
                <span className="req">{run.requirement}</span>
                <span className="meta">
                  <Badge tone={tone} pulse={moving}>
                    {run.status}
                  </Badge>
                  <span className="meta-time">{formatDateTime(run.created_at)}</span>
                </span>
              </button>
              <Button
                variant="ghost"
                size="sm"
                className="run-delete icon-btn"
                onClick={() => setPendingDelete(run)}
                aria-label={`Delete run ${run.run_id}`}
                title="Delete run"
              >
                <Icon name="trash" size={15} />
              </Button>
            </li>
          );
        })}
      </ul>

      {footer && <div className="side-footer">{footer}</div>}

      {pendingDelete && (
        <ConfirmModal
          title={`Delete run ${pendingDelete.run_id}?`}
          confirmLabel="Delete run"
          busy={false}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => {
            onDelete(pendingDelete.run_id);
            setPendingDelete(null);
          }}
          body={
            <>
              <p>
                This permanently deletes everything this run produced (stories, spec, code, tests,
                reports). This cannot be undone.
              </p>
              <p className="hint quote">{pendingDelete.requirement}</p>
            </>
          }
        />
      )}
    </nav>
  );
}
