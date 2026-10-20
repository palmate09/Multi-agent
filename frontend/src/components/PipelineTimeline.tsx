import { formatClock, formatDuration } from '../lib/format';
import { buildStages, finishedStatus, STATE_WORD } from '../lib/pipeline';
import type { PipelineEvent } from '../types';
import { statusTone } from '../types';
import { StageMarker } from './PipelineRail';
import { EmptyState } from './ui/Card';

interface Props {
  events: PipelineEvent[];
  running: boolean;
}

/**
 * The detailed pipeline log: one row per stage with what it produced, how long
 * it worked, and when it last reported. The same stage model as the header's
 * rail, so the two cannot disagree.
 */
export function PipelineTimeline({ events, running }: Props) {
  if (events.length === 0) {
    return (
      <EmptyState>No pipeline activity yet. Each agent step appears here as it happens.</EmptyState>
    );
  }

  // While the run is moving, show what is still to come; once it has settled,
  // show only what actually happened.
  const stages = buildStages(events, running).filter(s => running || s.state !== 'pending');
  const finished = finishedStatus(events);
  const finishedState = finished
    ? statusTone(finished.status) === 'bad'
      ? 'failed'
      : 'done'
    : null;

  return (
    <div
      className="timeline"
      role="log"
      aria-live="polite"
      aria-relevant="additions"
      aria-label="Pipeline progress"
    >
      {stages.map(s => (
        <div key={s.node} className={`tl-node ${s.state}`}>
          <div className="tl-icon">
            <StageMarker state={s.state} />
          </div>
          <div className="tl-name">
            {s.label}
            <span className="visually-hidden"> ({STATE_WORD[s.state]})</span>
            {s.ts && <div className="tl-time">{formatClock(s.ts)}</div>}
          </div>
          <div className="tl-detail">
            {s.details.map((line, i) => (
              <div key={i} className={line.startsWith('retry #') ? 'tl-retry' : undefined}>
                {line}
              </div>
            ))}
            {s.details.length === 0 && s.state === 'active' && (
              <div className="tl-quiet">Working…</div>
            )}
            {s.state === 'pending' && <div className="tl-quiet">Waiting</div>}
          </div>
          <div className="tl-duration">{s.seconds !== null ? formatDuration(s.seconds) : ''}</div>
        </div>
      ))}

      {finished && finishedState && (
        <div className={`tl-node finish ${finishedState}`}>
          <div className="tl-icon">
            <StageMarker state={finishedState} />
          </div>
          <div className="tl-name">
            Finished
            <div className="tl-time">{formatClock(finished.ts)}</div>
          </div>
          <div className="tl-detail">status: {finished.status}</div>
          <div className="tl-duration" />
        </div>
      )}
    </div>
  );
}
