import { useCallback, useEffect, useRef, useState } from 'react';

import { api } from '../api';
import type { PreviewState, PreviewStatus, RunSummary } from '../types';
import { Alert } from './ui/Alert';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { Card } from './ui/Card';
import { EmptyState } from './ui/Card';
import { SkeletonGroup } from './ui/Skeleton';

interface Props {
  run: RunSummary;
  /** Resets the iframe so a manual refresh actually re-fetches. */
  reloadKey: number;
  onReload: () => void;
}

const TONE: Record<PreviewState, 'good' | 'warn' | 'bad' | 'idle'> = {
  running: 'good',
  starting: 'warn',
  crashed: 'bad',
  stopped: 'idle',
};

const EXPLAIN: Record<PreviewState, string> = {
  stopped: 'Not running.',
  starting: 'Booting the generated app…',
  running: 'The app is serving on a loopback port behind the gateway.',
  crashed: 'The app did not come up. The log below is what it printed.',
};

/**
 * Live view of a generated app: state, controls, log tail, iframe.
 *
 * The iframe points at the gateway path (`/preview/<run_id>/`) rather
 * than a port, so the browser never learns the ephemeral port and the
 * session cookie rides along as it would for any same-origin request.
 *
 * Polling happens only while a boot is in flight. `stopped` and
 * `crashed` are terminal, so polling them re-fetched the same answer
 * every 1.5s for as long as the tab stayed open, for no reason.
 */
export function PlaygroundPanel({ run, reloadKey, onReload }: Props) {
  const [status, setStatus] = useState<PreviewStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.previewStatus(run.run_id));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to read preview status');
    }
  }, [run.run_id]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh();
    // A boot is the only state here that changes on its own. A running
    // app is stable, and the idle TTL is enforced server-side.
    if (status?.state !== 'starting') return;
    pollRef.current = setTimeout(() => void refresh(), 1500);
    return () => {
      if (pollRef.current) clearTimeout(pollRef.current);
    };
  }, [refresh, status?.state]);

  const act = async (fn: () => Promise<PreviewStatus | void>) => {
    setBusy(true);
    setError(null);
    try {
      const next = await fn();
      if (next) setStatus(next);
      else await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Preview action failed');
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const state: PreviewState = status?.state ?? 'stopped';
  const url = status?.url ?? `/preview/${run.run_id}/`;
  const running = state === 'running';
  const idleMinutes = status ? Math.round((status.idle_seconds ?? 0) / 60) : 0;

  return (
    <Card label="Playground">
      <div className="row row-between">
        <div>
          <h2>Playground</h2>
          <Badge tone={TONE[state]} pulse={state === 'starting'}>
            {state}
          </Badge>
          <div className="hint" style={{ marginTop: 6 }}>
            {EXPLAIN[state]}
          </div>
        </div>
        <div className="row">
          <Button
            variant="primary"
            disabled={busy || running}
            loading={busy && !running}
            loadingLabel="Booting…"
            onClick={() => void act(() => api.startPreview(run.run_id))}
            title="Boot the generated app on a loopback port and serve it at /preview"
          >
            {state === 'stopped' ? 'Start' : 'Restart'}
          </Button>
          <Button
            variant="ghost"
            disabled={busy || state === 'stopped'}
            onClick={() => void act(() => api.stopPreview(run.run_id).then(() => undefined))}
            title="Stop the app and discard its working directory"
          >
            Stop
          </Button>
        </div>
      </div>

      {error && <Alert tone="error">{error}</Alert>}
      {status?.error && (
        <Alert tone="error" className="alert-pre">
          {status.error}
        </Alert>
      )}

      {status === null && !error && <SkeletonGroup label="Checking playground status" rows={2} />}

      {running && (
        <>
          <iframe
            key={reloadKey}
            title={`Preview of ${run.run_id}`}
            src={url}
            className="preview-frame"
          />
          <div className="row" style={{ marginTop: 10 }}>
            <a className="ghost preview-link" href={url} target="_blank" rel="noopener noreferrer">
              Open in a new tab
            </a>
            <Button variant="ghost" onClick={onReload} title="Re-fetch the page inside the iframe">
              Refresh
            </Button>
            <span className="hint">
              port {status?.port} on loopback · idle {idleMinutes}m
            </span>
          </div>
        </>
      )}

      {!running && status !== null && (
        <EmptyState
          action={
            <Button variant="primary" onClick={() => void act(() => api.startPreview(run.run_id))}>
              Start the preview
            </Button>
          }
        >
          Start the preview to load the generated app here. It runs as a real subprocess on this
          host with the platform&apos;s API keys stripped from its environment — that is a
          mitigation, not isolation.
        </EmptyState>
      )}

      {status?.log_tail && (
        <>
          <h3 className="log-heading">Server log</h3>
          <pre className="code">{status.log_tail}</pre>
        </>
      )}
    </Card>
  );
}
