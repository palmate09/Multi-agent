import type { EvalResults, Health, RunDetail, RunSummary } from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`;
    try {
      const body = await resp.json();
      if (body?.detail) {
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      }
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

export const api = {
  health: () => request<Health>("/health"),

  listRuns: () => request<RunSummary[]>("/api/runs"),

  getRun: (runId: string) => request<RunDetail>(`/api/runs/${runId}`),

  createRun: (body: {
    requirement: string;
    skip_tester?: boolean;
    skip_reviewer?: boolean;
  }) =>
    request<RunSummary>("/api/runs", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  deleteRun: (runId: string) =>
    request<void>(`/api/runs/${runId}`, { method: "DELETE" }),

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
  const source = new EventSource(`${BASE}/api/runs/${runId}/events`);

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