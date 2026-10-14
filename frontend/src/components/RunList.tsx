import type { RunSummary } from '../types';
import { statusTone } from '../types';
import { formatDateTime } from '../lib/format';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { ConfirmModal } from './ConfirmModal';
import { EmptyState } from './ui/Card';
import { useState } from 'react';

interface Props {
  runs: RunSummary[];
  selected: string | null;
  onSelect: (_id: string) => void;
  onDelete: (_id: string) => void;
  onRefresh: () => void;
  /** Whether the sidebar drawer is open (mobile). */
  open?: boolean;
  /** Called when the sidebar should close (e.g. after selecting a run). */
  onClose?: () => void;
}

/**
 * The run list.
 *
 * Previously each row was a `role="button"` div with a real delete `<button>`
 * inside it, which is the nested-interactive failure: the outer element claims
 * to be a button while containing one, so a screen reader announces a button
 * inside a button and the key handling disagrees between the two. A row is now
 * a plain container with two sibling controls — the row's own button, and the
 * delete button beside it — so there is exactly one interactive target per
 * action and neither nests inside the other.
 */
export function RunList({
  runs,
  selected,
  onSelect,
  onDelete,
  onRefresh,
  open = false,
  onClose,
}: Props) {
  const [pendingDelete, setPendingDelete] = useState<RunSummary | null>(null);

  // Close the mobile drawer when a run is selected
  const handleSelect = (id: string) => {
    onSelect(id);
    onClose?.();
  };

  return (
    <nav id="sidebar" className={`sidebar${open ? ' open' : ''}`} aria-label="Pipeline runs">
      <div className="side-head">
        <span className="side-title">Runs</span>
        <Button variant="ghost" size="sm" onClick={onRefresh} aria-label="Refresh run list">
          <span aria-hidden="true">↻</span>
        </Button>
      </div>

      {runs.length === 0 && <EmptyState>No runs yet.</EmptyState>}

      <ul className="run-list">
        {runs.map(run => {
          const active = selected === run.run_id;
          const tone = statusTone(run.status);
          const moving = run.status === 'running' || run.status === 'stopping';

          return (
            <li key={run.run_id} className={`run-item${active ? ' active' : ''}`}>
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
                className="run-delete"
                onClick={() => setPendingDelete(run)}
                aria-label={`Delete run ${run.run_id}`}
              >
                <span aria-hidden="true">✕</span>
              </Button>
            </li>
          );
        })}
      </ul>

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
              <p className="hint">{pendingDelete.requirement}</p>
            </>
          }
        />
      )}
    </nav>
  );
}
