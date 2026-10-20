import type { PipelineEvent, PipelineNode } from '../types';
import { NODE_LABELS, NODE_ORDER } from '../types';

/**
 * Turns a run's flat event stream into one entry per pipeline stage.
 *
 * Two views need the same answer to "where is this run?": the stage rail in
 * the run header (always visible) and the detailed timeline (on the Pipeline
 * tab). Deriving it in one place is what keeps them from ever disagreeing.
 */

export type StageState = 'done' | 'active' | 'failed' | 'stopped' | 'pending';

/** The word for each state. The marker's glyph and colour repeat it; the word
 *  is what assistive tech reads, so state is never colour-only. */
export const STATE_WORD: Record<StageState, string> = {
  done: 'done',
  active: 'running',
  failed: 'failed',
  stopped: 'stopped',
  pending: 'waiting',
};

export interface Stage {
  node: PipelineNode;
  label: string;
  state: StageState;
  /** Timestamp of the stage's latest event, or null if it has not started. */
  ts: string | null;
  /** Seconds the stage actually worked, from its start→done pairs. */
  seconds: number | null;
  /** One short line per meaningful event; empty when there is nothing to add. */
  details: string[];
}

/** Human-readable one-liner for an event, shown next to the stage name. */
export function describeEvent(event: PipelineEvent): string {
  const d = event as Record<string, unknown>;
  const bits: string[] = [];

  switch (event.phase) {
    case 'start':
      if (event.node === 'pipeline') {
        bits.push(`nodes: ${(d.nodes as string[])?.join(' → ') ?? ''}`);
      } else if (typeof d.retry === 'number') {
        bits.push(`retry #${d.retry} (${String(d.reason ?? 'retry')})`);
      } else {
        bits.push('started');
      }
      break;
    case 'done':
      if (event.node === 'reasoner') {
        // The plan node: either it wrote a plan, or it was ablated on purpose.
        if (d.ablated === true) {
          bits.push('ablated, no plan');
        } else {
          if (typeof d.risks === 'number') bits.push(`${d.risks} risks`);
          if (typeof d.edge_cases === 'number') bits.push(`${d.edge_cases} edge cases`);
          if (typeof d.open_questions === 'number') {
            bits.push(`${d.open_questions} open questions`);
          }
          if (d.structured === false) bits.push('prose, not parsed');
        }
        break;
      }
      if (typeof d.stories === 'number') bits.push(`${d.stories} stories`);
      if (typeof d.endpoints === 'number') bits.push(`${d.endpoints} endpoints`);
      if (Array.isArray(d.files)) bits.push(`${d.files.length} files`);
      if (typeof d.passed === 'number') {
        bits.push(`${d.passed} passed, ${d.failed} failed`);
      }
      if (typeof d.verdict === 'string') bits.push(`verdict: ${d.verdict}`);
      if (typeof d.blockers === 'number') bits.push(`${d.blockers} blockers`);
      if (typeof d.status === 'string') bits.push(`status: ${d.status}`);
      if (typeof d.reflection === 'string' && d.reflection) {
        bits.push(String(d.reflection));
      }
      if (Array.isArray(d.failures) && d.failures.length) {
        bits.push(`${(d.failures as string[]).length} failing`);
      }
      break;
    case 'progress':
      if (typeof d.detail === 'string') bits.push(String(d.detail));
      break;
    case 'error':
      bits.push(String(d.error ?? 'error'));
      break;
    default:
      break;
  }
  return bits.filter(Boolean).join(' · ');
}

/** Stages the pipeline announced it would run, from its own start event. */
function plannedNodes(events: PipelineEvent[]): PipelineNode[] {
  const start = events.find(e => e.node === 'pipeline' && e.phase === 'start');
  const nodes = (start as Record<string, unknown> | undefined)?.nodes;
  return Array.isArray(nodes) ? (nodes.filter(n => typeof n === 'string') as PipelineNode[]) : [];
}

/** Seconds a stage was working: the sum of its start→done pairs. Summing the
 *  pairs rather than taking first→last keeps a retried stage from being
 *  charged for the time other stages ran in between. */
function workedSeconds(events: PipelineEvent[]): number | null {
  let open: number | null = null;
  let total = 0;
  let counted = false;
  for (const e of events) {
    const at = Date.parse(e.ts);
    if (Number.isNaN(at)) continue;
    if (e.phase === 'start') {
      open = at;
    } else if (e.phase === 'done' && open !== null) {
      total += Math.max(0, (at - open) / 1000);
      open = null;
      counted = true;
    }
  }
  return counted ? total : null;
}

function stateOf(events: PipelineEvent[], running: boolean, accepted: boolean): StageState {
  const last = events[events.length - 1];
  if (last.phase === 'done') return 'done';
  if (last.phase === 'error') return 'failed';
  // An accepted run is the pipeline's own word that every stage finished. A
  // stage can still end on a trailing retry "start" or "progress" in older
  // event logs (the terminal event was missing), and the rail used to render
  // that completed work as "stopped". Errors above still win, and a stage that
  // never reported done (for example a UI node that failed) stays stopped.
  if (accepted && events.some(e => e.phase === 'done')) return 'done';
  return running ? 'active' : 'stopped';
}

/**
 * One entry per stage, in pipeline order.
 *
 * `pipeline` (the run itself) and `final` (its verdict) are not stages: the
 * status badge already says how the run ended, so the rail and timeline show
 * only the agents that did work. Stages the pipeline announced but has not
 * reached yet come back as `pending`.
 */
export function buildStages(events: PipelineEvent[], running: boolean): Stage[] {
  const byNode = new Map<PipelineNode, PipelineEvent[]>();
  for (const e of events) {
    if (e.node === 'pipeline' || e.node === 'final') continue;
    const list = byNode.get(e.node) ?? [];
    list.push(e);
    byNode.set(e.node, list);
  }

  const accepted = !running && finishedStatus(events)?.status === 'accepted';
  const wanted = new Set<PipelineNode>([...plannedNodes(events), ...byNode.keys()]);
  wanted.delete('pipeline');
  wanted.delete('final');

  // Known stages in pipeline order; anything the backend adds later goes last
  // rather than being dropped.
  const known = NODE_ORDER.filter(n => wanted.has(n));
  const unknown = [...wanted].filter(n => !NODE_ORDER.includes(n));

  return [...known, ...unknown].map(node => {
    const own = byNode.get(node) ?? [];
    if (own.length === 0) {
      return {
        node,
        label: NODE_LABELS[node] ?? node,
        state: 'pending' as const,
        ts: null,
        seconds: null,
        details: [],
      };
    }

    const lines = own.map(describeEvent).filter(Boolean);
    // A bare "started" is noise once the stage has said anything else.
    const meaningful = lines.filter(l => l !== 'started');
    const state = stateOf(own, running, accepted);

    return {
      node,
      label: NODE_LABELS[node] ?? node,
      state,
      ts: own[own.length - 1].ts,
      seconds: workedSeconds(own),
      details: meaningful,
    };
  });
}

/** The run's closing event, if it has one: its `status` is the verdict. */
export function finishedStatus(events: PipelineEvent[]): { ts: string; status: string } | null {
  const end =
    events.find(e => e.node === 'pipeline' && e.phase === 'end') ??
    events.find(e => e.node === 'final' && e.phase === 'done');
  if (!end) return null;
  const status = (end as Record<string, unknown>).status;
  return { ts: end.ts, status: typeof status === 'string' ? status : 'unknown' };
}
