export type Severity = "blocker" | "major" | "minor";

export type PipelineNode =
  | "pipeline"
  | "pm"
  | "designer"
  | "developer"
  | "tester"
  | "runner"
  | "triage"
  | "reviewer"
  | "final";

export interface PipelineEvent {
  seq: number;
  node: PipelineNode;
  phase: string;
  ts: string;
  [key: string]: unknown;
}

export interface RunSummary {
  run_id: string;
  requirement: string;
  status: string;
  created_at: string;
  updated_at: string;
  tests_passed: number;
  tests_failed: number;
  attempts_dev: number;
  attempts_test: number;
  review_rounds: number;
  wall_time: number | null;
  error?: string | null;
}

export interface UserStory {
  id: string;
  title: string;
  acceptance: string[];
}

export interface Stories {
  stories: UserStory[];
  clarifying_question?: string | null;
}

export interface ReviewComment {
  severity: Severity;
  message: string;
  location: string;
}

export interface RunDetail extends RunSummary {
  events: PipelineEvent[];
  artifacts: string[];
  stories: Stories | null;
  spec: string | null;
  code: Record<string, string>;
  tests: Record<string, string>;
  review: { comments: ReviewComment[] } | null;
  report: { passed: number; failed: number; raw?: string } | null;
  memory: string[];
}

export interface SessionInfo {
  authenticated: boolean;
  username: string | null;
  auth_enabled: boolean;
  csrf_token: string | null;
}

export interface LlmStatus {
  ollama_reachable: boolean;
  ollama_models: string[];
  skip_ollama: boolean;
  hosted_keys_present: string[];
}

export interface Health {
  status: "ok" | "degraded";
  version: string;
  environment: string;
  llm: LlmStatus;
  active_runs: number;
  max_concurrent_runs: number;
}

export interface EvalResults {
  runs: Array<{
    run: string;
    status: string;
    test_pass_rate: number;
    spec_coverage: number;
    attempts_to_green: number;
    wall_time: number;
  }>;
  baseline: { passed: number; failed: number; wall_time: number } | null;
  total: number;
  accepted: number;
  pass_rate: number | null;
}

export const NODE_LABELS: Record<PipelineNode, string> = {
  pipeline: "Pipeline",
  pm: "PM",
  designer: "Designer",
  developer: "Developer",
  tester: "Tester",
  runner: "Sandbox",
  triage: "Triage",
  reviewer: "Reviewer",
  final: "Final",
};

export const NODE_ORDER: PipelineNode[] = [
  "pm",
  "designer",
  "developer",
  "tester",
  "runner",
  "triage",
  "reviewer",
  "final",
];

export function statusTone(status: string): "good" | "warn" | "bad" | "idle" {
  if (status.startsWith("accepted")) return "good";
  if (status === "failed" || status === "unresolved" || status === "unresolved_review")
    return "bad";
  if (status === "running" || status === "queued" || status === "tests_green")
    return "warn";
  return "idle";
}