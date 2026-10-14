/* eslint-disable react-hooks/set-state-in-effect */
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api, setUnauthorizedHandler, subscribeToRun } from './api';
import type { Health, PipelineEvent, RunDetail, RunSummary } from './types';
import { statusTone } from './types';
import { ConfirmModal } from './components/ConfirmModal';
import { FileViewer } from './components/FileViewer';
import { LoginPage } from './components/LoginPage';
import { PipelineTimeline } from './components/PipelineTimeline';
import { RequirementForm } from './components/RequirementForm';
import { RunList } from './components/RunList';
import { Alert } from './components/ui/Alert';
import { Badge } from './components/ui/Badge';
import { Button } from './components/ui/Button';
import { Card, EmptyState } from './components/ui/Card';
import { ErrorBoundary } from './components/ui/ErrorBoundary';
import { Stat, StatGrid } from './components/ui/Stat';
import { Tabs } from './components/ui/Tabs';
import type { TabItem } from './components/ui/Tabs';
import { ToastList } from './components/ui/Toast';
import type { Toast } from './components/ui/Toast';

// Split out of the main bundle. Together these three were most of the
// 175 KB the browser downloaded before first paint, and the majority of
// sessions never open any of them: the eval table is a diagnostics view,
// and the UI and Playground panels only render on their own tabs.
// React.lazy defers the download to the first render of the panel; see
// `showEvals` for the eval table, which additionally has to not be
// rendered at all.
const EvalPanel = lazy(() =>
  import('./components/EvalPanel').then(m => ({ default: m.EvalPanel }))
);
const UiPanel = lazy(() => import('./components/UiPanel').then(m => ({ default: m.UiPanel })));
const PlaygroundPanel = lazy(() =>
  import('./components/PlaygroundPanel').then(m => ({ default: m.PlaygroundPanel }))
);

function PanelFallback({ label }: { label: string }) {
  return <p className="empty">Loading {label}…</p>;
}

type Tab =
  | 'pipeline'
  | 'stories'
  | 'plan'
  | 'spec'
  | 'code'
  | 'tests'
  | 'report'
  | 'review'
  | 'ui'
  | 'playground';

const TABS: ReadonlyArray<TabItem<Tab>> = [
  { id: 'pipeline', label: 'Pipeline' },
  { id: 'stories', label: 'Stories' },
  { id: 'plan', label: 'Plan' },
  { id: 'spec', label: 'OpenAPI' },
  { id: 'code', label: 'Code' },
  { id: 'tests', label: 'Tests' },
  { id: 'report', label: 'Test report' },
  { id: 'review', label: 'Review' },
  { id: 'ui', label: 'UI' },
  { id: 'playground', label: 'Playground' },
];

const POLL_MS = 1500;

/** Toast ids only need to be unique within a session. */
let toastSeq = 0;

export default function App() {
  // "checking" until we know whether a session exists, so we never flash the
  // app to an unauthenticated user.
  const [authState, setAuthState] = useState<'checking' | 'in' | 'out'>('checking');
  const [authEnabled, setAuthEnabled] = useState(true);
  const [username, setUsername] = useState<string | null>(null);
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);

  const [health, setHealth] = useState<Health | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [liveEvents, setLiveEvents] = useState<PipelineEvent[]>([]);
  const [tab, setTab] = useState<Tab>('pipeline');
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // Bumped by the Playground's Refresh button to force a new iframe document.
  const [previewReload, setPreviewReload] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [evals, setEvals] = useState<Awaited<ReturnType<typeof api.evals>> | null>(null);
  // The eval table is collapsed until asked for, which is what actually defers
  // its chunk: React.lazy only helps if the component is not rendered at all,
  // and this card sits below the fold on every screen.
  const [showEvals, setShowEvals] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);

  const unsubscribe = useRef<(() => void) | null>(null);

  const pushToast = useCallback((tone: Toast['tone'], message: string) => {
    const id = ++toastSeq;
    setToasts(prev => [...prev.slice(-3), { id, tone, message }]);
  }, []);

  const dismissToast = useCallback((id: number) => {
    setToasts(prev => prev.filter(t => t.id !== id));
  }, []);

  const refreshRuns = useCallback(async () => {
    try {
      setRuns(await api.listRuns());
    } catch {
      /* transient */
    }
  }, []);

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await api.health());
    } catch {
      /* transient */
    }
  }, []);

  // Establish the session once on mount.
  useEffect(() => {
    api
      .session()
      .then(s => {
        setAuthEnabled(s.auth_enabled);
        setUsername(s.username);
        setAuthState(s.authenticated ? 'in' : 'out');
      })
      .catch(() => setAuthState('out'));
  }, []);

  // Any 401 from anywhere drops us back to the login screen.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setAuthState('out');
      setSelected(null);
      setDetail(null);
      setRuns([]);
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  const handleLogin = async (user: string, pass: string) => {
    setLoginBusy(true);
    setLoginError(null);
    try {
      const s = await api.login(user, pass);
      setAuthEnabled(s.auth_enabled);
      setUsername(s.username);
      setAuthState('in');
    } catch (e) {
      setLoginError(e instanceof Error ? e.message : 'Sign-in failed');
    } finally {
      setLoginBusy(false);
    }
  };

  const handleLogout = async () => {
    try {
      await api.logout();
    } catch {
      /* the cookie is cleared either way */
    }
    setAuthState('out');
    setSelected(null);
    setDetail(null);
    setRuns([]);
  };

  // Load dashboard data once authenticated.
  useEffect(() => {
    if (authState !== 'in') return;
    refreshHealth();
    refreshRuns();
  }, [authState, refreshHealth, refreshRuns]);

  // The eval results are fetched only when the panel is opened, not on login:
  // it is a static table that changes between deployments, so there is nothing
  // about it that a session can invalidate.
  useEffect(() => {
    if (authState !== 'in' || !showEvals || evals) return;
    api
      .evals()
      .then(setEvals)
      .catch(() => undefined);
  }, [authState, showEvals, evals]);

  const loadDetail = useCallback(async (id: string) => {
    try {
      setDetail(await api.getRun(id));
    } catch {
      /* transient */
    }
  }, []);

  // Poll the active run while it is still moving; stop as soon as it settles.
  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setLiveEvents([]);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async () => {
      await loadDetail(selected);
      if (cancelled) return;
      const current = await api
        .getRun(selected)
        .then(d => d.status)
        .catch(() => 'unknown');
      if (current === 'queued' || current === 'running' || current === 'stopping') {
        timer = setTimeout(tick, POLL_MS);
      } else {
        refreshRuns();
      }
    };
    void tick();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [selected, loadDetail, refreshRuns]);

  // Live SSE feed for immediate feedback between polls.
  useEffect(() => {
    unsubscribe.current?.();
    unsubscribe.current = null;
    if (!selected) return;
    unsubscribe.current = subscribeToRun(
      selected,
      evt => setLiveEvents(prev => [...prev, evt as PipelineEvent]),
      () => {
        refreshRuns();
        void loadDetail(selected);
      },
      () => setError('Lost connection to the event stream; falling back to polling.')
    );
    return () => {
      unsubscribe.current?.();
      unsubscribe.current = null;
    };
  }, [selected, refreshRuns, loadDetail]);

  const startRun = async (
    requirement: string,
    opts: { skipTester: boolean; skipReviewer: boolean; skipReasoner: boolean; ui: boolean }
  ) => {
    setBusy(true);
    setError(null);
    try {
      const run = await api.createRun({
        requirement,
        skip_tester: opts.skipTester,
        skip_reviewer: opts.skipReviewer,
        skip_reasoner: opts.skipReasoner,
        ui: opts.ui,
      });
      setLiveEvents([]);
      await refreshRuns();
      setSelected(run.run_id);
      // Starting a run is the one action with no other feedback: the
      // list updates silently. Without this, a screen reader hears
      // nothing at all, and a sighted user may not notice the new row.
      pushToast('success', `Started run ${run.run_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to start run');
    } finally {
      setBusy(false);
    }
  };

  const deleteRun = async (id: string) => {
    try {
      await api.deleteRun(id);
      if (selected === id) setSelected(null);
      await refreshRuns();
      pushToast('success', `Deleted run ${id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to delete run');
    }
  };

  const [stopping, setStopping] = useState(false);
  const [restartTarget, setRestartTarget] = useState<string | null>(null);
  const [restartBusy, setRestartBusy] = useState(false);
  const [resuming, setResuming] = useState(false);
  const stopRun = async (id: string) => {
    setStopping(true);
    setError(null);
    try {
      await api.stopRun(id);
      await loadDetail(id);
      await refreshRuns();
      pushToast('info', `Stop requested for ${id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to stop run');
    } finally {
      setStopping(false);
    }
  };

  const confirmRestart = async () => {
    if (!restartTarget) return;
    setRestartBusy(true);
    setError(null);
    try {
      const next = await api.restartRun(restartTarget);
      setRestartTarget(null);
      setLiveEvents([]);
      await refreshRuns();
      setSelected(next.run_id);
      pushToast('success', `Restarted as ${next.run_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to restart run');
    } finally {
      setRestartBusy(false);
    }
  };

  const resumeRun = async (id: string) => {
    setResuming(true);
    setError(null);
    try {
      const next = await api.resumeRun(id);
      setLiveEvents([]);
      await refreshRuns();
      setSelected(next.run_id);
      pushToast('success', `Resumed as ${next.run_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to resume run');
    } finally {
      setResuming(false);
    }
  };

  const events = useMemo<PipelineEvent[]>(() => {
    const fromPoll = detail?.events ?? [];
    const seen = new Set(fromPoll.map(e => e.seq));
    const extra = liveEvents.filter(e => typeof e.seq === 'number' && !seen.has(e.seq));
    return [...fromPoll, ...extra].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
  }, [detail, liveEvents]);

  const running = detail?.status === 'running' || detail?.status === 'queued';
  const stoppingNow = detail?.status === 'stopping' || stopping;
  const stoppable = detail?.status === 'running' || detail?.status === 'queued';
  // Restart is offered on every settled run: it deletes all data and starts
  // over, which is meaningful for failed and accepted runs alike. Resume
  // continues a non-accepted terminal run from its saved artifacts.
  const restartable = !!detail && !stoppable && detail.status !== 'stopping';
  const resumable =
    !!detail &&
    ['cancelled', 'unresolved', 'unresolved_review', 'failed', 'blocked', 'domain_missed'].includes(
      detail.status
    );
  const tone = statusTone(detail?.status ?? 'idle');

  const llmLabel = health
    ? health.llm.skip_ollama
      ? 'template mode'
      : health.llm.ollama_reachable
        ? `ollama: ${health.llm.ollama_models.length} model(s)`
        : 'no LLM backend'
    : 'connecting…';

  if (authState === 'checking') {
    return (
      <div className="app">
        <div className="empty boot-check">Checking session…</div>
      </div>
    );
  }

  if (authState === 'out' && authEnabled) {
    return <LoginPage onLogin={handleLogin} error={loginError} busy={loginBusy} />;
  }

  return (
    <div className="app">
      <a href="#main-content" className="skip-link">
        Skip to main content
      </a>
      <header className="topbar">
        <h1>Multi-Agent Software Team</h1>
        <span className="sub">plain English → tested, reviewed REST API</span>
        <Button
          className="sidebar-toggle"
          aria-label="Toggle run list"
          aria-expanded={sidebarOpen}
          onClick={() => setSidebarOpen(!sidebarOpen)}
          aria-controls="sidebar"
        >
          <span aria-hidden="true">☰</span>
        </Button>
        <span className="sub">plain English → tested, reviewed REST API</span>
        <div className="spacer" />
        <Badge tone="idle" title={llmLabel}>
          {llmLabel}
        </Badge>
        {authEnabled && username && (
          <>
            <Badge tone="idle" title={`signed in as ${username}`}>
              {username}
            </Badge>
            <Button variant="ghost" onClick={() => void handleLogout()}>
              Sign out
            </Button>
          </>
        )}
        <span className="sub">v{health?.version ?? '—'}</span>
      </header>

      <div className="layout">
        <RunList
          runs={runs}
          selected={selected}
          onSelect={setSelected}
          onDelete={deleteRun}
          onRefresh={refreshRuns}
          open={sidebarOpen}
          onClose={() => setSidebarOpen(false)}
        />

        <main className="main" id="main-content">
          <RequirementForm
            onSubmit={startRun}
            busy={busy}
            error={error}
            onRetry={() => setError(null)}
            onDismissError={() => setError(null)}
          />

          {!detail ? (
            <Card>
              <h2>No run selected</h2>
              <EmptyState
                action={
                  <Button
                    variant="ghost"
                    onClick={() => {
                      document.getElementById('req')?.focus();
                    }}
                  >
                    Write a requirement
                  </Button>
                }
              >
                Submit a requirement above, or pick a previous run from the list.
              </EmptyState>
            </Card>
          ) : (
            <>
              <Card label={`Run ${detail.run_id}`}>
                <div className="run-head">
                  <div className="run-title">
                    <h2>
                      Run <code>{detail.run_id}</code>
                    </h2>
                    {/* A run's status changes on its own, so it is a
                        live region: settling at `accepted` is the
                        moment a user is waiting for. */}
                    <span role="status" className="run-status">
                      <Badge tone={tone} pulse={running || stoppingNow}>
                        {detail.status}
                      </Badge>
                    </span>
                  </div>
                  <div className="row run-actions">
                    {stoppable && (
                      <Button
                        variant="ghost"
                        disabled={stopping}
                        loading={stopping}
                        loadingLabel="Stopping…"
                        onClick={() => void stopRun(detail.run_id)}
                        title="Request cancellation; the run settles at cancelled"
                      >
                        Stop
                      </Button>
                    )}
                    {stoppingNow && !stoppable && <span className="hint">Stopping…</span>}
                    {restartable && (
                      <Button
                        variant="ghost"
                        onClick={() => setRestartTarget(detail.run_id)}
                        title="Delete all of this run's data and start over"
                      >
                        Restart
                      </Button>
                    )}
                    {resumable && (
                      <Button
                        variant="ghost"
                        disabled={resuming}
                        loading={resuming}
                        loadingLabel="Resuming…"
                        onClick={() => void resumeRun(detail.run_id)}
                        title="Continue this run from its saved artifacts (nothing is deleted)"
                      >
                        Resume
                      </Button>
                    )}
                  </div>
                </div>
                {detail.resumed_from && (
                  <div className="hint run-meta">
                    Resumed from <code>{detail.resumed_from}</code>
                  </div>
                )}
                {detail.error && (
                  <Alert tone="error" className="run-meta">
                    {detail.error}
                  </Alert>
                )}

                <StatGrid className="run-stats">
                  <Stat value={detail.tests_passed} label="Tests passed" />
                  <Stat value={detail.tests_failed} label="Failed" />
                  <Stat value={detail.attempts_dev} label="Dev retries" />
                  <Stat value={detail.attempts_test} label="Test retries" />
                  <Stat value={detail.review_rounds} label="Review rounds" />
                  <Stat value={detail.wall_time ?? '—'} label="Wall (s)" />
                </StatGrid>

                {detail.entrypoint && (
                  <div className="hint run-meta">
                    Entrypoint{' '}
                    <code>
                      {detail.entrypoint}
                      {detail.files ? ` · ${detail.files.join(', ')}` : ''}
                    </code>
                  </div>
                )}
                {detail.coverage && detail.coverage.conclusive && (
                  <Alert
                    tone={detail.coverage.missing.length ? 'error' : 'note'}
                    className="run-meta"
                  >
                    Domain check:{' '}
                    {detail.coverage.missing.length === 0
                      ? `all requirement terms present (${detail.coverage.covered.length})`
                      : `missing ${detail.coverage.missing.join(', ')}`}
                  </Alert>
                )}
                {detail.ui_report && (
                  <Alert tone={detail.ui_report.ok ? 'note' : 'error'} className="run-meta">
                    UI gate:{' '}
                    {detail.ui_report.ok
                      ? 'all static checks passed'
                      : `failed ${detail.ui_report.checks
                          .filter(c => !c.ok)
                          .map(c => c.name)
                          .join(', ')} (advisory)`}
                  </Alert>
                )}
              </Card>

              <Tabs
                label="Run detail"
                value={tab}
                onChange={setTab}
                items={[...TABS].map(t => ({
                  ...t,
                  badge:
                    t.id === 'review' && detail.review?.comments?.length
                      ? ` (${detail.review.comments.length})`
                      : t.id === 'ui' && Object.keys(detail.ui_code ?? {}).length
                        ? ` (${Object.keys(detail.ui_code ?? {}).length})`
                        : undefined,
                }))}
              >
                <ErrorBoundary label="This tab">
                  {renderTab(tab, {
                    detail,
                    events,
                    running,
                    previewReload,
                    setPreviewReload,
                  })}
                </ErrorBoundary>
              </Tabs>
            </>
          )}

          <Card label="Evaluation">
            <h2>
              <button
                type="button"
                className="linklike"
                aria-expanded={showEvals}
                onClick={() => setShowEvals(v => !v)}
              >
                {showEvals ? 'Hide' : 'Show'} evaluation
              </button>
            </h2>
            {showEvals && (
              <Suspense fallback={<PanelFallback label="evaluation" />}>
                <EvalPanel data={evals} />
              </Suspense>
            )}
          </Card>
        </main>
      </div>

      <ToastList toasts={toasts} onDismiss={dismissToast} />

      {restartTarget && (
        <ConfirmModal
          title={`Restart run ${restartTarget}?`}
          body={
            <>
              <p>
                This will stop the pipeline if it is still running,{' '}
                <strong>permanently delete all of this run&apos;s data</strong> (stories, spec,
                code, tests, reports), and start a fresh run with the same requirement.
              </p>
              <p className="hint">This cannot be undone.</p>
            </>
          }
          confirmLabel="Delete data and restart"
          busy={restartBusy}
          onConfirm={() => void confirmRestart()}
          onCancel={() => {
            if (!restartBusy) setRestartTarget(null);
          }}
        />
      )}
    </div>
  );
}

interface TabContext {
  detail: RunDetail;
  events: PipelineEvent[];
  running: boolean;
  previewReload: number;
  setPreviewReload: (_fn: (_n: number) => number) => void;
}

/**
 * The active tab's panel.
 *
 * Kept as a function rather than a map so the tab bodies can read
 * `detail` directly and so an unknown tab id cannot render anything.
 */
function renderTab(
  tab: Tab,
  { detail, events, running, previewReload, setPreviewReload }: TabContext
) {
  switch (tab) {
    case 'pipeline':
      return <PipelineTimeline events={events} running={running} />;

    case 'stories':
      return detail.stories?.stories?.length ? (
        <Card label="Stories">
          {detail.stories.clarifying_question && (
            <Alert tone="note">Clarifying question: {detail.stories.clarifying_question}</Alert>
          )}
          {detail.stories.stories.map(s => (
            <article key={s.id} className="story">
              <div className="id">{s.id}</div>
              <h3 className="title">{s.title}</h3>
              <ul>
                {s.acceptance.map((a, i) => (
                  <li key={i}>{a}</li>
                ))}
              </ul>
            </article>
          ))}
        </Card>
      ) : (
        <EmptyState>
          No stories yet. The PM node writes them first, so they appear as soon as the run starts.
        </EmptyState>
      );

    case 'plan':
      return detail.plan ? (
        <Card className="plan" label="Plan">
          {!detail.plan.structured && (
            <Alert tone="note">
              The model answered in prose instead of the JSON block, so this is its own wording
              rather than a parsed plan.
            </Alert>
          )}
          <h3>Approach</h3>
          <p className="approach">{detail.plan.approach}</p>
          {(
            [
              ['Decisions', detail.plan.decisions],
              ['Risks', detail.plan.risks],
              ['Edge cases', detail.plan.edge_cases],
              ['Open questions', detail.plan.open_questions],
            ] as Array<[string, string[]]>
          ).map(([title, items]) =>
            items.length ? (
              <section key={title} className="section">
                <div className="hint caps">
                  {title} ({items.length})
                </div>
                <ul>
                  {items.map((it, i) => (
                    <li key={i}>{it}</li>
                  ))}
                </ul>
              </section>
            ) : null
          )}
          <p className="hint footer">
            Advisory: it guides the Designer and Developer. The Tester and Reviewer never see it — a
            run is graded on its contract and its tests.
          </p>
        </Card>
      ) : (
        <EmptyState>
          No plan yet. The reasoner node has not produced one, or it was ablated for this run.
        </EmptyState>
      );

    case 'spec':
      return detail.spec ? (
        <pre className="code">{detail.spec}</pre>
      ) : (
        <EmptyState>No OpenAPI spec yet.</EmptyState>
      );

    case 'code':
      return <FileViewer files={detail.code} emptyHint="No code generated." />;

    case 'tests':
      return <FileViewer files={detail.tests} emptyHint="No tests generated." />;

    case 'report':
      return detail.report ? (
        <Card label="Test report">
          <StatGrid>
            <Stat value={detail.report.passed} label="Passed" />
            <Stat value={detail.report.failed} label="Failed" />
          </StatGrid>
          {detail.report.raw && <pre className="code">{detail.report.raw}</pre>}
          {detail.memory.length > 0 && (
            <>
              <h3>Reflections</h3>
              {detail.memory.map((m, i) => (
                <p key={i} className="hint">
                  {m}
                </p>
              ))}
            </>
          )}
        </Card>
      ) : (
        <EmptyState>No test report yet.</EmptyState>
      );

    case 'review':
      return detail.review?.comments?.length ? (
        <Card label="Review">
          {detail.review.comments.map((c, i) => (
            <div key={i} className="comment">
              <span className={`sev ${c.severity}`}>{c.severity}</span>
              <div>
                <div className="msg">{c.message}</div>
                {c.location && <div className="loc">{c.location}</div>}
              </div>
            </div>
          ))}
        </Card>
      ) : (
        <EmptyState>No review comments yet.</EmptyState>
      );

    case 'ui':
      // Both lazy panels need a Suspense boundary above them. Selecting a tab is
      // a synchronous state change, and a lazy component that suspends in
      // response to one takes the whole tree down with it — React throws
      // "A component suspended while responding to synchronous input" unless
      // the update is wrapped, or a boundary is already in place.
      return (
        <Suspense fallback={<PanelFallback label="UI panel" />}>
          <UiPanel detail={detail} />
        </Suspense>
      );

    case 'playground':
      return (
        <Suspense fallback={<PanelFallback label="playground" />}>
          <PlaygroundPanel
            run={detail}
            reloadKey={previewReload}
            onReload={() => setPreviewReload(n => n + 1)}
          />
        </Suspense>
      );
  }
}
