import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, setUnauthorizedHandler, subscribeToRun } from "./api";
import type { Health, PipelineEvent, RunDetail, RunSummary } from "./types";
import { statusTone } from "./types";
import { EvalPanel } from "./components/EvalPanel";
import { FileViewer } from "./components/FileViewer";
import { LoginPage } from "./components/LoginPage";
import { PipelineTimeline } from "./components/PipelineTimeline";
import { RequirementForm } from "./components/RequirementForm";
import { RunList } from "./components/RunList";

type Tab = "pipeline" | "stories" | "spec" | "code" | "tests" | "report" | "review";

const TABS: Array<{ id: Tab; label: string }> = [
  { id: "pipeline", label: "Pipeline" },
  { id: "stories", label: "Stories" },
  { id: "spec", label: "OpenAPI" },
  { id: "code", label: "Code" },
  { id: "tests", label: "Tests" },
  { id: "report", label: "Test report" },
  { id: "review", label: "Review" },
];

const POLL_MS = 1500;

export default function App() {
  // "checking" until we know whether a session exists, so we never flash the
  // app to an unauthenticated user.
  const [authState, setAuthState] = useState<"checking" | "in" | "out">("checking");
  const [authEnabled, setAuthEnabled] = useState(true);
  const [username, setUsername] = useState<string | null>(null);
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);

  const [health, setHealth] = useState<Health | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [liveEvents, setLiveEvents] = useState<PipelineEvent[]>([]);
  const [tab, setTab] = useState<Tab>("pipeline");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [evals, setEvals] = useState<Awaited<ReturnType<typeof api.evals>> | null>(null);

  const unsubscribe = useRef<(() => void) | null>(null);

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
      .then((s) => {
        setAuthEnabled(s.auth_enabled);
        setUsername(s.username);
        setAuthState(s.authenticated ? "in" : "out");
      })
      .catch(() => setAuthState("out"));
  }, []);

  // Any 401 from anywhere drops us back to the login screen.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setAuthState("out");
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
      setAuthState("in");
    } catch (e) {
      setLoginError(e instanceof Error ? e.message : "Sign-in failed");
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
    setAuthState("out");
    setSelected(null);
    setDetail(null);
    setRuns([]);
  };

  // Load dashboard data once authenticated.
  useEffect(() => {
    if (authState !== "in") return;
    refreshHealth();
    refreshRuns();
    api.evals().then(setEvals).catch(() => undefined);
  }, [authState, refreshHealth, refreshRuns]);

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
        .then((d) => d.status)
        .catch(() => "unknown");
      if (current === "queued" || current === "running" || current === "stopping") {
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
      (evt) => setLiveEvents((prev) => [...prev, evt as PipelineEvent]),
      () => {
        refreshRuns();
        void loadDetail(selected);
      },
      () => setError("Lost connection to the event stream; falling back to polling.")
    );
    return () => {
      unsubscribe.current?.();
      unsubscribe.current = null;
    };
  }, [selected, refreshRuns, loadDetail]);

  const startRun = async (
    requirement: string,
    opts: { skipTester: boolean; skipReviewer: boolean }
  ) => {
    setBusy(true);
    setError(null);
    try {
      const run = await api.createRun({
        requirement,
        skip_tester: opts.skipTester,
        skip_reviewer: opts.skipReviewer,
      });
      setLiveEvents([]);
      await refreshRuns();
      setSelected(run.run_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start run");
    } finally {
      setBusy(false);
    }
  };

  const deleteRun = async (id: string) => {
    try {
      await api.deleteRun(id);
      if (selected === id) setSelected(null);
      await refreshRuns();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete run");
    }
  };

  const [stopping, setStopping] = useState(false);
  const stopRun = async (id: string) => {
    setStopping(true);
    setError(null);
    try {
      await api.stopRun(id);
      await loadDetail(id);
      await refreshRuns();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to stop run");
    } finally {
      setStopping(false);
    }
  };

  const events = useMemo<PipelineEvent[]>(() => {
    const fromPoll = detail?.events ?? [];
    const seen = new Set(fromPoll.map((e) => e.seq));
    const extra = liveEvents.filter((e) => typeof e.seq === "number" && !seen.has(e.seq));
    return [...fromPoll, ...extra].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
  }, [detail, liveEvents]);

  const running = detail?.status === "running" || detail?.status === "queued";
  const stoppingNow = detail?.status === "stopping" || stopping;
  const stoppable = detail?.status === "running" || detail?.status === "queued";
  const tone = statusTone(detail?.status ?? "idle");

  const llmLabel = health
    ? health.llm.skip_ollama
      ? "template mode"
      : health.llm.ollama_reachable
        ? `ollama: ${health.llm.ollama_models.length} model(s)`
        : "no LLM backend"
    : "connecting…";

  if (authState === "checking") {
    return (
      <div className="app">
        <div className="empty" style={{ marginTop: 80 }}>
          Checking session…
        </div>
      </div>
    );
  }

  if (authState === "out" && authEnabled) {
    return <LoginPage onLogin={handleLogin} error={loginError} busy={loginBusy} />;
  }

  return (
    <div className="app">
      <header className="topbar">
        <h1>Multi-Agent Software Team</h1>
        <span className="sub">plain English → tested, reviewed REST API</span>
        <div className="spacer" />
        <span className="badge idle" title={llmLabel}>
          <span className="dot" />
          {llmLabel}
        </span>
        {authEnabled && username && (
          <>
            <span className="badge idle" title={`signed in as ${username}`}>
              <span className="dot" />
              {username}
            </span>
            <button className="ghost" onClick={() => void handleLogout()}>
              Sign out
            </button>
          </>
        )}
        <span className="sub">v{health?.version ?? "—"}</span>
      </header>

      <div className="layout">
        <RunList
          runs={runs}
          selected={selected}
          onSelect={setSelected}
          onDelete={deleteRun}
          onRefresh={refreshRuns}
        />

        <main className="main">
          <RequirementForm onSubmit={startRun} busy={busy} error={error} />

          {!detail ? (
            <div className="card">
              <h2>No run selected</h2>
              <p className="empty">
                Submit a requirement above, or pick a previous run from the list.
              </p>
            </div>
          ) : (
            <>
              <div className="card">
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <div>
                    <h2 style={{ marginBottom: 4 }}>
                      Run <code>{detail.run_id}</code>
                    </h2>
                    <span className={`badge ${tone}`}>
                      <span className={`dot${running || stoppingNow ? " pulse" : ""}`} />
                      {detail.status}
                    </span>
                    {stoppable && (
                      <button
                        className="ghost"
                        style={{ marginLeft: 8 }}
                        disabled={stopping}
                        onClick={() => void stopRun(detail.run_id)}
                        title="Request cancellation; the run settles at cancelled"
                      >
                        {stopping ? "Stopping…" : "Stop"}
                      </button>
                    )}
                    {stoppingNow && !stoppable && (
                      <span className="hint" style={{ marginLeft: 8 }}>
                        Stopping…
                      </span>
                    )}
                    {detail.error && (
                      <span className="hint" style={{ marginLeft: 8 }}>
                        {detail.error}
                      </span>
                    )}
                  </div>
                </div>
                <div className="stats" style={{ marginTop: 12, marginBottom: 0 }}>
                  <div className="stat">
                    <div className="v">{detail.tests_passed}</div>
                    <div className="k">Tests passed</div>
                  </div>
                  <div className="stat">
                    <div className="v">{detail.tests_failed}</div>
                    <div className="k">Failed</div>
                  </div>
                  <div className="stat">
                    <div className="v">{detail.attempts_dev}</div>
                    <div className="k">Dev retries</div>
                  </div>
                  <div className="stat">
                    <div className="v">{detail.attempts_test}</div>
                    <div className="k">Test retries</div>
                  </div>
                  <div className="stat">
                    <div className="v">{detail.review_rounds}</div>
                    <div className="k">Review rounds</div>
                  </div>
                  <div className="stat">
                    <div className="v">{detail.wall_time ?? "—"}</div>
                    <div className="k">Wall (s)</div>
                  </div>
                </div>
                {detail.entrypoint && (
                  <div className="hint" style={{ marginTop: 10 }}>
                    Entrypoint{" "}
                    <code>
                      {detail.entrypoint}
                      {detail.files ? ` · ${detail.files.join(", ")}` : ""}
                    </code>
                  </div>
                )}
                {detail.coverage && detail.coverage.conclusive && (
                  <div className={`alert ${detail.coverage.missing.length ? "error" : "note"}`} style={{ marginTop: 10 }}>
                    Domain check:{" "}
                    {detail.coverage.missing.length === 0
                      ? `all requirement terms present (${detail.coverage.covered.length})`
                      : `missing ${detail.coverage.missing.join(", ")}`}
                  </div>
                )}
              </div>

              <div className="tabs">
                {TABS.map((t) => (
                  <button
                    key={t.id}
                    className={tab === t.id ? "active" : ""}
                    onClick={() => setTab(t.id)}
                  >
                    {t.label}
                    {t.id === "review" && detail.review?.comments?.length
                      ? ` (${detail.review.comments.length})`
                      : ""}
                  </button>
                ))}
              </div>

              {tab === "pipeline" && <PipelineTimeline events={events} running={running} />}

              {tab === "stories" &&
                (detail.stories?.stories?.length ? (
                  <div className="card">
                    {detail.stories.clarifying_question && (
                      <div className="alert note">
                        Clarifying question: {detail.stories.clarifying_question}
                      </div>
                    )}
                    {detail.stories.stories.map((s) => (
                      <div key={s.id} className="story">
                        <div className="id">{s.id}</div>
                        <div className="title">{s.title}</div>
                        <ul>
                          {s.acceptance.map((a, i) => (
                            <li key={i}>{a}</li>
                          ))}
                        </ul>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="empty">No stories yet.</p>
                ))}

              {tab === "spec" &&
                (detail.spec ? (
                  <pre className="code">{detail.spec}</pre>
                ) : (
                  <p className="empty">No OpenAPI spec yet.</p>
                ))}

              {tab === "code" && <FileViewer files={detail.code} emptyHint="No code generated." />}

              {tab === "tests" && (
                <FileViewer files={detail.tests} emptyHint="No tests generated." />
              )}

              {tab === "report" &&
                (detail.report ? (
                  <div className="card">
                    <div className="stats">
                      <div className="stat">
                        <div className="v">{detail.report.passed}</div>
                        <div className="k">Passed</div>
                      </div>
                      <div className="stat">
                        <div className="v">{detail.report.failed}</div>
                        <div className="k">Failed</div>
                      </div>
                    </div>
                    {detail.report.raw && <pre className="code">{detail.report.raw}</pre>}
                    {detail.memory.length > 0 && (
                      <>
                        <h3 style={{ marginTop: 16 }}>Reflections</h3>
                        {detail.memory.map((m, i) => (
                          <p key={i} className="hint" style={{ color: "var(--text-dim)" }}>
                            {m}
                          </p>
                        ))}
                      </>
                    )}
                  </div>
                ) : (
                  <p className="empty">No test report yet.</p>
                ))}

              {tab === "review" &&
                (detail.review?.comments?.length ? (
                  <div className="card">
                    {detail.review.comments.map((c, i) => (
                      <div key={i} className="comment">
                        <span className={`sev ${c.severity}`}>{c.severity}</span>
                        <div>
                          <div className="msg">{c.message}</div>
                          {c.location && <div className="loc">{c.location}</div>}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="empty">No review comments yet.</p>
                ))}
            </>
          )}

          <div className="card">
            <h2>Evaluation</h2>
            <EvalPanel data={evals} />
          </div>
        </main>
      </div>
    </div>
  );
}