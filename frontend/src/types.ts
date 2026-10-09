export type Severity = "blocker" | "major" | "minor";

export type PipelineNode =
  | "pipeline"
  | "pm"
  | "reasoner"
  | "designer"
  | "developer"
  | "coverage"
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
  /** Module the generated app was imported from, discovered from the code. */
  entrypoint?: string | null;
  /** Files the Developer actually emitted; the layout follows the design. */
  files?: string[];
  error?: string | null;
  /** Set when this run continues a previous one (resume); the old run id. */
  resumed_from?: string | null;
}

/** Verdict from the domain coverage gate. */
export interface Coverage {
  covered: string[];
  missing: string[];
  /** False when the requirement was too vague to judge; the gate is advisory. */
  conclusive: boolean;
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

/**
 * What the Reasoner worked out before the spec existed. Advisory: nothing is
 * graded against it, it is shown so a human can see what the run understood.
 */
export interface Plan {
  approach: string;
  decisions: string[];
  risks: string[];
  edge_cases: string[];
  open_questions: string[];
  /** False when the model answered in prose and the JSON block never parsed. */
  structured: boolean;
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
  /** Plan from the Reasoner node; null when it was ablated or has not run. */
  plan?: Plan | null;
  spec: string | null;
  code: Record<string, string>;
  tests: Record<string, string>;
  review: { comments: ReviewComment[] } | null;
  report: { passed: number; failed: number; raw?: string } | null;
  coverage?: Coverage | null;
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
  reasoner: "Reasoner",
  designer: "Designer",
  developer: "Developer",
  coverage: "Domain check",
  tester: "Tester",
  runner: "Sandbox",
  triage: "Triage",
  reviewer: "Reviewer",
  final: "Final",
};

export const NODE_ORDER: PipelineNode[] = [
  "pm",
  "reasoner",
  "designer",
  "developer",
  "coverage",
  "tester",
  "runner",
  "triage",
  "reviewer",
  "final",
];

export function statusTone(status: string): "good" | "warn" | "bad" | "idle" {
  if (status.startsWith("accepted")) return "good";
  // blocked = an agent produced nothing; domain_missed = it answered a different
  // question. Both are failures the old design could not report.
  if (
    status === "failed" ||
    status === "unresolved" ||
    status === "unresolved_review" ||
    status === "blocked" ||
    status === "domain_missed"
  )
    return "bad";
  if (status === "running" || status === "queued" || status === "tests_green")
    return "warn";
  // stopping = stop requested, worker draining to cancelled. cancelled = user
  // stopped the run; neutral tone, it is neither success nor failure.
  if (status === "stopping") return "warn";
  if (status === "cancelled") return "idle";
  return "idle";
}