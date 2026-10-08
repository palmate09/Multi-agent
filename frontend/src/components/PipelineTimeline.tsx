import type { PipelineEvent, PipelineNode } from "../types";
import { NODE_LABELS, NODE_ORDER } from "../types";

/** Human-readable one-liner for an event, shown next to the node name. */
function describe(event: PipelineEvent): string {
  const d = event as Record<string, unknown>;
  const bits: string[] = [];

  switch (event.phase) {
    case "start":
      if (event.node === "pipeline") {
        bits.push(`nodes: ${(d.nodes as string[])?.join(" → ") ?? ""}`);
      } else if (typeof d.retry === "number") {
        bits.push(`retry #${d.retry} (${String(d.reason ?? "retry")})`);
      } else {
        bits.push("started");
      }
      break;
    case "done":
      if (typeof d.stories === "number") bits.push(`${d.stories} stories`);
      if (typeof d.endpoints === "number") bits.push(`${d.endpoints} endpoints`);
      if (Array.isArray(d.files)) bits.push(`${d.files.length} files`);
      if (typeof d.passed === "number") {
        bits.push(`${d.passed} passed, ${d.failed} failed`);
      }
      if (typeof d.verdict === "string") bits.push(`verdict: ${d.verdict}`);
      if (typeof d.blockers === "number") bits.push(`${d.blockers} blockers`);
      if (typeof d.status === "string") bits.push(`status: ${d.status}`);
      if (typeof d.reflection === "string" && d.reflection) {
        bits.push(String(d.reflection));
      }
      if (Array.isArray(d.failures) && d.failures.length) {
        bits.push(`${(d.failures as string[]).length} failing`);
      }
      break;
    case "progress":
      if (typeof d.detail === "string") bits.push(String(d.detail));
      break;
    case "error":
      bits.push(String(d.error ?? "error"));
      break;
    default:
      break;
  }
  return bits.filter(Boolean).join(" · ");
}

function shortTime(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString([], {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts;
  }
}

interface Props {
  events: PipelineEvent[];
  running: boolean;
}

export function PipelineTimeline({ events, running }: Props) {
  const byNode = new Map<PipelineNode, PipelineEvent[]>();
  for (const e of events) {
    if (e.node === "pipeline" && e.phase === "end") continue;
    const list = byNode.get(e.node) ?? [];
    list.push(e);
    byNode.set(e.node, list);
  }

  const ordered = NODE_ORDER.filter((n) => byNode.has(n));
  // Nodes with events we don't know about (future pipeline changes).
  const extras = [...byNode.keys()].filter((n) => !NODE_ORDER.includes(n));

  if (events.length === 0) {
    return <p className="empty">No pipeline activity yet. Start a run to see live agent steps.</p>;
  }

  const finalEvent = events.find((e) => e.phase === "end");

  return (
    <div className="timeline">
      {[...ordered, ...extras].map((node) => {
        const nodeEvents = byNode.get(node) ?? [];
        const last = nodeEvents[nodeEvents.length - 1];
        const state = last.phase === "done" ? "done" : "active";
        const isActive = state === "active" && running;
        return (
          <div key={node} className={`tl-node ${state}`}>
            <div className="tl-icon">{isActive ? "" : state === "done" ? "✓" : "•"}</div>
            <div className="tl-name">
              {NODE_LABELS[node]}
              <div className="kv hint" style={{ marginTop: 0 }}>
                {shortTime(last.ts)}
              </div>
            </div>
            <div className="tl-detail">
              {nodeEvents.map((e, i) => (
                <div key={e.seq ?? i}>
                  {i > 0 && <span className="kv"> · </span>}
                  {describe(e)}
                </div>
              ))}
            </div>
          </div>
        );
      })}
      {finalEvent && (
        <div className="tl-node done">
          <div className="tl-icon">✓</div>
          <div className="tl-name">Finished</div>
          <div className="tl-detail">
            status: {String((finalEvent as Record<string, unknown>).status ?? "unknown")}
          </div>
        </div>
      )}
    </div>
  );
}