import { useCallback, useEffect, useState } from 'react';

import { api } from '../api';
import type { PreviewState, PreviewStatus, RunSummary } from '../types';
import { Alert } from './ui/Alert';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { Card } from './ui/Card';
import { CodeBlock } from './ui/CodeBlock';
import { EmptyState } from './ui/Card';
import { Icon } from './ui/Icon';
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
  running: 'The app is serving behind the gateway, in an isolated sandbox container.',
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

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.previewStatus(run.run_id));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to read preview status');
    }
  }, [run.run_id]);

  // Read the status once when the panel opens (or the run changes).
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh();
  }, [refresh]);

  // A boot is the only state here that changes on its own: a running app is
  // stable, and the idle TTL is enforced server-side. While it is `starting`,
  // each answer schedules the next poll. This depends on the status OBJECT, not
  // on `status.state`: while the app is still starting the state does not
  // change, so keying on it polled exactly once and then left the badge on
  // "starting" forever whenever a boot took longer than one interval.
  useEffect(() => {
    if (status?.state !== 'starting') return;
    const timer = setTimeout(() => void refresh(), 1500);
    return () => clearTimeout(timer);
  }, [status, refresh]);

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
  // Point the frame at a path the app actually declares. `/` is often not a
  // route, and a 404 inside the iframe reads as a broken playground when the
  // app is serving perfectly well.
  const entry = status?.entry_path ?? '/';
  const url = status?.url
    ? `${status.url.replace(/\/$/, '')}${entry === '/' ? '/' : entry}`
    : `/preview/${run.run_id}${entry === '/' ? '/' : entry}`;
  const running = state === 'running';
  const idleMinutes = status ? Math.round((status.idle_seconds ?? 0) / 60) : 0;

  return (
    <Card label="Playground">
      <div className="playground-head">
        <div className="playground-title">
          <div className="playground-name">
            <h2>Playground</h2>
            <Badge tone={TONE[state]} pulse={state === 'starting'}>
              {state}
            </Badge>
          </div>
          <div className="hint">{EXPLAIN[state]}</div>
          {run.kind === 'fullstack' && (
            <div className="hint">
              This is the generated application itself — the same process serves the page and the
              API.
            </div>
          )}
        </div>
        <div className="row">
          <Button
            variant="primary"
            disabled={busy || running}
            loading={busy && !running}
            loadingLabel="Booting…"
            onClick={() => void act(() => api.startPreview(run.run_id))}
            title="Boot the generated app in an isolated container and serve it at /preview"
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
        <div className="browser">
          <div className="browser-bar">
            <span className="browser-url" title={url}>
              {url}
            </span>
            <Button
              variant="ghost"
              size="sm"
              className="icon-btn"
              onClick={onReload}
              aria-label="Refresh preview"
              title="Re-fetch the page inside the frame"
            >
              <Icon name="refresh" size={15} />
            </Button>
            <a
              className="btn btn-sm ghost icon-btn preview-link"
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Open preview in a new tab"
              title="Open in a new tab"
            >
              <Icon name="external" size={15} />
            </a>
          </div>
          <iframe
            key={reloadKey}
            title={`Preview of ${run.run_id}`}
            src={url}
            className="preview-frame"
          />
          <div className="browser-foot hint">
            {status?.mode === 'container' ? 'Isolated container' : 'Local subprocess'} · idle{' '}
            {idleMinutes}m
          </div>
        </div>
      )}

      {!running && status !== null && (
        <EmptyState
          action={
            <Button variant="primary" onClick={() => void act(() => api.startPreview(run.run_id))}>
              Start the preview
            </Button>
          }
        >
          Start the preview to load the generated app here. It runs in a hardened container on an
          internal network with no route off the host, no platform credentials, and the code mounted
          read-only.
        </EmptyState>
      )}

      {status?.log_tail && (
        <div className="log-block">
          <CodeBlock code={status.log_tail} title="Server log" lineNumbers={false} />
        </div>
      )}
    </Card>
  );
}
