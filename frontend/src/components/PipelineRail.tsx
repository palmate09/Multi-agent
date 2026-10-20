import { buildStages, STATE_WORD } from '../lib/pipeline';
import type { StageState } from '../lib/pipeline';
import type { PipelineEvent } from '../types';
import { Icon } from './ui/Icon';

/** The round marker for a stage: tick, spinner, cross, or an empty ring. */
export function StageMarker({ state }: { state: StageState }) {
  return (
    <span className={`stage-marker ${state}`} aria-hidden="true">
      {state === 'done' && <Icon name="check" size={12} />}
      {state === 'active' && <Icon name="loader" size={12} />}
      {state === 'failed' && <Icon name="close" size={12} />}
    </span>
  );
}

interface Props {
  events: PipelineEvent[];
  running: boolean;
}

/**
 * Every pipeline stage in one line, with where the run is right now.
 *
 * It sits in the run header so the answer to "how far along is it, and where
 * did it stop?" is visible whichever tab is open. Stages the pipeline planned
 * but has not reached are shown as empty rings, so a failed run shows exactly
 * how far it got. Renders nothing for a run with no events.
 */
export function PipelineRail({ events, running }: Props) {
  const stages = buildStages(events, running);
  if (stages.length === 0) return null;

  return (
    <ol className="rail" aria-label="Pipeline stages">
      {stages.map(s => (
        <li key={s.node} className={`rail-stage ${s.state}`}>
          <StageMarker state={s.state} />
          <span className="rail-label">
            {s.label}
            <span className="visually-hidden">: {STATE_WORD[s.state]}</span>
          </span>
        </li>
      ))}
    </ol>
  );
}
