import type { RunSummary } from "../types";
import { statusTone } from "../types";

interface Props {
  runs: RunSummary[];
  selected: string | null;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onRefresh: () => void;
}

function shortTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleDateString([], { month: "short", day: "numeric" }) +
      " " + d.toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

export function RunList({ runs, selected, onSelect, onDelete, onRefresh }: Props) {
  return (
    <div className="sidebar">
      <div className="side-head">
        <span className="side-title">Runs</span>
        <button className="ghost" onClick={onRefresh} title="Refresh list">
          ↻
        </button>
      </div>
      {runs.length === 0 && <p className="empty">No runs yet.</p>}
      {runs.map((run) => {
        const tone = statusTone(run.status);
        return (
          <div
            key={run.run_id}
            className={`run-item${selected === run.run_id ? " active" : ""}`}
            onClick={() => onSelect(run.run_id)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelect(run.run_id);
              }
            }}
          >
            <div className="req">{run.requirement}</div>
            <div className="meta">
              <span className={`badge ${tone}`}>
                <span className={`dot${run.status === "running" ? " pulse" : ""}`} />
                {run.status}
              </span>
              <span>{shortTime(run.created_at)}</span>
              <button
                className="ghost"
                title="Delete run"
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete(run.run_id);
                }}
              >
                ✕
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}