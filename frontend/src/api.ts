import type {
  EvalResults,
  Health,
  RunDetail,
  RunSummary,
  SessionInfo,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "";

const CSRF_COOKIE = "mat_csrf";

/** Read the double-submit CSRF token the login endpoint set. */
export function csrfToken(): string | null {
  const match = document.cookie.match(
    new RegExp(`(?:^|; )${CSRF_COOKIE}=([^;]*)`)
  );
  return match ? decodeURIComponent(match[1]) : null;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

let onUnauthorized: (() => void) | null = null;

/** Register a callback fired when the API returns 401, so the UI can re-login. */
export function setUnauthorizedHandler(fn: (() => void) | null) {
  onUnauthorized = fn;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
    ...((init?.headers as Record<string, string>) ?? {}),
  };
  // State-changing calls must echo the CSRF token.
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const token = csrfToken();
    if (token) headers["X-CSRF-Token"] = token;
  }

  const resp = await fetch(`${BASE}${path}`, {
    ...init,
    headers,
    // Session cookie must ride along.
    credentials: "same-origin",
  });

  if (resp.status === 401) {
    onUnauthorized?.();
  }

  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`;
    try {
      const body = await resp.json();
      if (body?.detail) {
        detail =
          typeof body.detail === "string"
            ? body.detail
            : JSON.stringify(body.detail);
      }
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

export const api = {
  session: () => request<SessionInfo>("/api/auth/me"),

  login: (username: string, password: string) =>
    request<SessionInfo>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),

  logout: () => request<void>("/api/auth/logout", { method: "POST" }),

  health: () => request<Health>("/health"),

  listRuns: () => request<RunSummary[]>("/api/runs"),

  getRun: (runId: string) => request<RunDetail>(`/api/runs/${runId}`),

  createRun: (body: {
    requirement: string;
    skip_tester?: boolean;
    skip_reviewer?: boolean;
    skip_reasoner?: boolean;
  }) =>
    request<RunSummary>("/api/runs", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  deleteRun: (runId: string) =>
    request<void>(`/api/runs/${runId}`, { method: "DELETE" }),

  stopRun: (runId: string) =>
    request<{ run_id: string; stopped: boolean; status: string }>(
      `/api/runs/${runId}/stop`,
      { method: "POST", body: JSON.stringify({}) }
    ),

  restartRun: (runId: string) =>
    request<RunSummary>(`/api/runs/${runId}/restart`, {
      method: "POST",
      body: JSON.stringify({}),
    }),

  resumeRun: (runId: string) =>
    request<RunSummary>(`/api/runs/${runId}/resume`, {
      method: "POST",
      body: JSON.stringify({}),
    }),

  evals: () => request<EvalResults>("/api/evals/results"),
};

/**
 * Subscribe to a run's server-sent event stream.
 * Returns an unsubscribe function.
 */
export function subscribeToRun(
  runId: string,
  onEvent: (event: unknown) => void,
  onEnd?: () => void,
  onError?: (err: unknown) => void
): () => void {
  const source = new EventSource(`${BASE}/api/runs/${runId}/events`, {
    withCredentials: true,
  });

  source.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data));
    } catch {
      /* ignore malformed frames */
    }
  };
  source.onerror = () => {
    // EventSource auto-reconnects on transient drops; only surface a hard end.
    if (source.readyState === EventSource.CLOSED) {
      onError?.(new Error("stream closed"));
      onEnd?.();
    }
  };
  source.addEventListener("end", () => {
    source.close();
    onEnd?.();
  });

  return () => source.close();
}