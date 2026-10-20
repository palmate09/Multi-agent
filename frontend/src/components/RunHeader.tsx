import type { PipelineEvent, RunDetail } from '../types';
import { statusTone } from '../types';
import { PipelineRail } from './PipelineRail';
import { Alert } from './ui/Alert';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { Card } from './ui/Card';
import { Icon } from './ui/Icon';
import { Stat, StatGrid } from './ui/Stat';

interface Props {
  detail: RunDetail;
  events: PipelineEvent[];
  /** The run is queued or running. */
  running: boolean;
  stoppable: boolean;
  stopping: boolean;
  /** A stop was requested and the worker is still draining. */
  stoppingNow: boolean;
  restartable: boolean;
  resumable: boolean;
  resuming: boolean;
  onStop: () => void;
  onRestart: () => void;
  onResume: () => void;
}

/** What the run produced, in the words a person would use. */
const KIND_LABEL: Record<string, string> = {
  fullstack: 'Full-stack app',
  api: 'HTTP service',
  program: 'Program',
};

/**
 * Everything about a run that is true whichever tab is open: what was asked
 * for, how it is going, the verdict, and what can be done about it.
 *
 * The requirement is shown in full. A run used to be identified only by its id,
 * which meant the one thing a person recognises it by was visible only in the
 * sidebar.
 */
export function RunHeader({
  detail,
  events,
  running,
  stoppable,
  stopping,
  stoppingNow,
  restartable,
  resumable,
  resuming,
  onStop,
  onRestart,
  onResume,
}: Props) {
  const tone = statusTone(detail.status);
  const failing = detail.tests_failed > 0;

  return (
    <Card label={`Run ${detail.run_id}`} className="run-card">
      <div className="run-head">
        <div className="run-title">
          <h2>
            Run <code>{detail.run_id}</code>
          </h2>
          {/* A run's status changes on its own, so it is a live region:
              settling at `accepted` is the moment a user is waiting for. */}
          <span role="status" className="run-status">
            <Badge tone={tone} pulse={running || stoppingNow}>
              {detail.status}
            </Badge>
          </span>
          {detail.kind && (
            <Badge tone="idle" title="What this run produced">
              {KIND_LABEL[detail.kind] ?? detail.kind}
            </Badge>
          )}
        </div>
        <div className="row run-actions">
          {stoppable && (
            <Button
              disabled={stopping}
              loading={stopping}
              loadingLabel="Stopping…"
              onClick={onStop}
              title="Request cancellation; the run settles at cancelled"
            >
              <Icon name="stop" size={14} />
              Stop
            </Button>
          )}
          {stoppingNow && !stoppable && <span className="hint">Stopping…</span>}
          {resumable && (
            <Button
              variant="primary"
              disabled={resuming}
              loading={resuming}
              loadingLabel="Resuming…"
              onClick={onResume}
              title="Continue this run from its saved artifacts (nothing is deleted)"
            >
              <Icon name="play" size={14} />
              Resume
            </Button>
          )}
          {restartable && (
            <Button onClick={onRestart} title="Delete all of this run's data and start over">
              <Icon name="restart" size={14} />
              Restart
            </Button>
          )}
        </div>
      </div>

      <p className="run-requirement">{detail.requirement}</p>

      {detail.resumed_from && (
        <div className="hint run-meta">
          Resumed from <code>{detail.resumed_from}</code>
        </div>
      )}
      {detail.error && (
        <Alert tone="error" className="run-meta">
          {detail.error}
        </Alert>
      )}

      <PipelineRail events={events} running={running} />

      <StatGrid className="run-stats">
        <Stat
          value={detail.tests_passed}
          label="Tests passed"
          tone={detail.tests_passed > 0 && !failing ? 'good' : undefined}
        />
        <Stat value={detail.tests_failed} label="Failed" tone={failing ? 'bad' : undefined} />
        <Stat value={detail.attempts_dev} label="Dev retries" />
        <Stat value={detail.attempts_test} label="Test retries" />
        <Stat value={detail.review_rounds} label="Review rounds" />
        <Stat value={detail.wall_time ?? '—'} label="Wall (s)" />
      </StatGrid>

      {detail.entrypoint && (
        <div className="hint run-meta">
          Entrypoint{' '}
          <code>
            {detail.entrypoint}
            {detail.files ? ` · ${detail.files.join(', ')}` : ''}
          </code>
        </div>
      )}
      {((detail.coverage && detail.coverage.conclusive) || detail.ui_report) && (
        <div className="run-notes">
          {detail.coverage && detail.coverage.conclusive && (
            <Alert tone={detail.coverage.missing.length ? 'warning' : 'success'}>
              Domain check:{' '}
              {detail.coverage.missing.length === 0
                ? `all requirement terms present (${detail.coverage.covered.length})`
                : `covered ${detail.coverage.covered.length} of ${
                    detail.coverage.covered.length + detail.coverage.missing.length
                  } requirement terms; not found: ${detail.coverage.missing.join(', ')}`}
            </Alert>
          )}
          {detail.ui_report && (
            <Alert tone={detail.ui_report.ok ? 'success' : 'warning'}>
              UI gate:{' '}
              {detail.ui_report.ok
                ? 'all static checks passed'
                : `failed ${detail.ui_report.checks
                    .filter(c => !c.ok)
                    .map(c => c.name)
                    .join(', ')} (advisory)`}
            </Alert>
          )}
        </div>
      )}
    </Card>
  );
}
