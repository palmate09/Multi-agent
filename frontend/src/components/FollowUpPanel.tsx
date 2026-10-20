import { useState } from 'react';

import { api } from '../api';
import type { RunDetail } from '../types';
import { Alert } from './ui/Alert';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { Card, EmptyState } from './ui/Card';
import { Textarea } from './ui/Field';

interface Props {
  detail: RunDetail;
  /** Called with the id of the run a change produced, so it can be opened. */
  onOpenRun: (_runId: string) => void;
}

const TONE: Record<string, 'good' | 'warn' | 'bad' | 'idle'> = {
  accepted: 'good',
  accepted_no_review: 'good',
  running: 'good',
  queued: 'idle',
  starting: 'idle',
  stopped: 'idle',
  cancelled: 'idle',
  unresolved: 'warn',
  unresolved_review: 'warn',
  blocked: 'bad',
  failed: 'bad',
};

/**
 * Change an app that already exists.
 *
 * A follow-up is not a new run of the same requirement: it starts from the
 * run's artifacts and applies one instruction, so what comes back is the same
 * product with one thing different. The changes queue up and run one at a time,
 * in the order they were given — two changes editing the same files at once
 * would fight, and the second would win.
 */
export function FollowUpPanel({ detail, onOpenRun }: Props) {
  const [prompt, setPrompt] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const followups = detail.followups ?? [];
  const working = detail.status === 'queued' || detail.status === 'running';

  const apply = async () => {
    const text = prompt.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    try {
      const next = await api.addFollowup(detail.run_id, text);
      setPrompt('');
      onOpenRun(next.run_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not queue the change');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel-stack">
      <Card label="Change this app">
        <p className="hint">
          Describe one change in plain English. The team applies it to the app that already
          exists — the stories, the contract, the code, the tests and the interface are all
          updated together, and nothing that already works is dropped.
        </p>
        <Textarea
          id="followup"
          label="What should change?"
          rows={4}
          value={prompt}
          onChange={e => setPrompt(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              void apply();
            }
          }}
          placeholder="e.g. let me mark a habit as skipped for a day, and show the skip in the list"
          hint="Ctrl+Enter to apply"
        />
        {error && <Alert tone="error">{error}</Alert>}
        <div className="row">
          <Button
            variant="primary"
            disabled={busy || working || !prompt.trim()}
            loading={busy}
            loadingLabel="Queueing…"
            onClick={() => void apply()}
            title="Apply this change to the app"
          >
            Apply the change
          </Button>
          {working && (
            <span className="hint">This run is still working — wait for it to settle.</span>
          )}
        </div>
      </Card>

      <Card label="Changes so far">
        {followups.length === 0 ? (
          <EmptyState>No changes yet. The app is exactly as it was generated.</EmptyState>
        ) : (
          <ol className="followup-list">
            {followups.map(f => (
              <li key={f.run_id} className="followup-item">
                <div className="followup-head">
                  <Badge tone={TONE[f.status] ?? 'idle'}>{f.status}</Badge>
                  <span className="followup-when">{new Date(f.created_at).toLocaleString()}</span>
                </div>
                <p className="followup-prompt">{f.prompt}</p>
                <div className="row followup-foot">
                  <span className="hint">
                    {f.tests_passed} passed · {f.tests_failed} failed
                  </span>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => onOpenRun(f.run_id)}
                    title="Open the run this change produced"
                  >
                    Open
                  </Button>
                </div>
                {f.error && <Alert tone="error">{f.error}</Alert>}
              </li>
            ))}
          </ol>
        )}
      </Card>
    </div>
  );
}
