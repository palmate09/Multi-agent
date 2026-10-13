import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api";
import type { PreviewState, PreviewStatus, RunSummary } from "../types";

interface Props {
  run: RunSummary;
  /** Resets the iframe so a manual refresh actually re-fetches. */
  reloadKey: number;
  onReload: () => void;
}

const TONE: Record<PreviewState, string> = {
  running: "good",
  starting: "warn",
  crashed: "bad",
  stopped: "idle",
};

const EXPLAIN: Record<PreviewState, string> = {
  stopped: "Not running.",
  starting: "Booting the generated app…",
  running: "The app is serving on a loopback port behind the gateway.",
  crashed: "The app did not come up. The log below is what it printed.",
};

/**
 * Live view of a generated app: state, controls, log tail, iframe.
 *
 * The iframe points at the gateway path (`/preview/<run_id>/`) rather than a
 * port, so the browser never learns the ephemeral port and the session cookie
 * rides along as it would for any same-origin request.
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
      setError(e instanceof Error ? e.message : "Failed to read preview status");
    }
  }, [run.run_id]);

  // Poll only while a boot is in flight; a running app does not change state
  // on its own, and the idle TTL is enforced server-side.
  useEffect(() => {
    void refresh();
    if (status?.state === "running" || status === null) return;
    pollRef.current = setTimeout(() => void refresh(), 1500);
    return () => {
      if (pollRef.current) clearTimeout(pollRef.current);
    };
  }, [refresh, status?.state, status === null]);

  const act = async (fn: () => Promise<PreviewStatus | void>) => {
    setBusy(true);
    setError(null);
    try {
      const next = await fn();
      if (next) setStatus(next);
      else await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Preview action failed");
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const state: PreviewState = status?.state ?? "stopped";
  const url = status?.url ?? `/preview/${run.run_id}/`;
  const running = state === "running";

  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h2 style={{ marginBottom: 4 }}>Playground</h2>
          <span className={`badge ${TONE[state]}`}>
            <span className={`dot${state === "starting" ? " pulse" : ""}`} />
            {state}
          </span>
          <div className="hint" style={{ marginTop: 6 }}>
            {EXPLAIN[state]}
          </div>
        </div>
        <div className="row">
          <button
            className="primary"
            disabled={busy || running}
            onClick={() => void act(() => api.startPreview(run.run_id))}
            title="Boot the generated app on a loopback port and serve it at /preview"
          >
            {state === "stopped" ? "Start" : "Restart"}
          </button>
          <button
            className="ghost"
            disabled={busy || state === "stopped"}
            onClick={() => void act(() => api.stopPreview(run.run_id).then(() => undefined))}
            title="Stop the app and discard its working directory"
          >
            Stop
          </button>
        </div>
      </div>

      {error && <div className="alert error">{error}</div>}
      {status?.error && (
        <div className="alert error" style={{ whiteSpace: "pre-wrap" }}>
          {status.error}
        </div>
      )}

      {running && (
        <>
          <iframe
            key={reloadKey}
            title={`Preview of ${run.run_id}`}
            src={url}
            style={{
              width: "100%",
              height: "70vh",
              minHeight: 420,
              border: "1px solid var(--border, #2a2f3a)",
              borderRadius: 6,
              marginTop: 12,
              background: "#fff",
            }}
          />
          <div className="row" style={{ marginTop: 10 }}>
            <a className="ghost" href={url} target="_blank" rel="noreferrer">
              Open in a new tab
            </a>
            <button
              className="ghost"
              onClick={onReload}
              title="Re-fetch the page inside the iframe"
            >
              Refresh
            </button>
            <span className="hint">
              port {status?.port} on loopback · idle{" "}
              {status ? Math.round((status.idle_seconds ?? 0) / 60) : 0}m
            </span>
          </div>
        </>
      )}

      {!running && (
        <p className="empty">
          Start the preview to load the generated app here. It runs as a real
          subprocess on this host with the platform&apos;s API keys stripped from
          its environment — that is a mitigation, not isolation.
        </p>
      )}

      {status?.log_tail && (
        <>
          <h3 style={{ marginTop: 16 }}>Server log</h3>
          <pre className="code">{status.log_tail}</pre>
        </>
      )}
    </div>
  );
}