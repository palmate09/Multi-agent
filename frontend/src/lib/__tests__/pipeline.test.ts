import { describe, it, expect } from 'vitest';
import { buildStages, finishedStatus } from '../pipeline';
import type { PipelineEvent } from '../../types';

const ev = (seq: number, node: string, phase: string, at: number, extra = {}): PipelineEvent =>
  ({
    seq,
    node,
    phase,
    ts: new Date(Date.UTC(2026, 9, 17, 8, 10, at)).toISOString(),
    ...extra,
  }) as PipelineEvent;

describe('buildStages', () => {
  it('returns nothing for a run with no events', () => {
    expect(buildStages([], false)).toEqual([]);
  });

  it('puts stages in pipeline order and leaves out the run itself and its verdict', () => {
    const stages = buildStages(
      [
        ev(1, 'pipeline', 'start', 0, { nodes: ['pm', 'designer', 'developer'] }),
        ev(2, 'developer', 'start', 5),
        ev(3, 'pm', 'done', 6, { stories: 3 }),
        ev(4, 'final', 'done', 9, { status: 'accepted' }),
      ],
      false
    );
    expect(stages.map(s => s.node)).toEqual(['pm', 'designer', 'developer']);
  });

  it('marks announced-but-unreached stages as pending', () => {
    const stages = buildStages(
      [
        ev(1, 'pipeline', 'start', 0, { nodes: ['pm', 'designer'] }),
        ev(2, 'pm', 'done', 4, { stories: 2 }),
      ],
      true
    );
    expect(stages.map(s => s.state)).toEqual(['done', 'pending']);
  });

  it('shows the latest stage as active only while the run is moving', () => {
    const events = [
      ev(1, 'developer', 'start', 0),
      ev(2, 'developer', 'progress', 3, { detail: 'writing' }),
    ];
    expect(buildStages(events, true)[0].state).toBe('active');
    // The same events with the run settled must not claim it is still working.
    expect(buildStages(events, false)[0].state).toBe('stopped');
    expect(buildStages(events, true)[0].details).toEqual(['writing']);
  });

  it('marks a stage done when an accepted run left it on a retry event', () => {
    // Historical logs can end a completed stage on its last retry "start"
    // (or a lint progress line). Acceptance says the work finished.
    const events = [
      ev(1, 'developer', 'start', 0),
      ev(2, 'developer', 'done', 5, { files: ['main.py'] }),
      ev(3, 'developer', 'progress', 6, { detail: 'lint repair pass complete' }),
      ev(4, 'developer', 'start', 10, { reason: 'boot_failure', retry: 1 }),
      ev(5, 'pipeline', 'end', 12, { status: 'accepted' }),
    ];
    expect(buildStages(events, false)[0].state).toBe('done');
  });

  it('does not promote a stage that never finished, even on an accepted run', () => {
    const events = [
      ev(1, 'ui_designer', 'start', 0),
      ev(2, 'pipeline', 'end', 12, { status: 'accepted' }),
    ];
    expect(buildStages(events, false)[0].state).toBe('stopped');
  });

  it('keeps retried stages stopped when the run did not succeed', () => {
    const events = [
      ev(1, 'developer', 'start', 0),
      ev(2, 'developer', 'done', 5),
      ev(3, 'developer', 'start', 10, { reason: 'code_bug', retry: 2 }),
      ev(4, 'pipeline', 'end', 12, { status: 'cancelled' }),
    ];
    expect(buildStages(events, false)[0].state).toBe('stopped');
  });

  it('reports an error phase as failed', () => {
    const stages = buildStages(
      [ev(1, 'tester', 'start', 0), ev(2, 'tester', 'error', 4, { error: 'boom' })],
      true
    );
    expect(stages[0].state).toBe('failed');
    expect(stages[0].details).toEqual(['boom']);
  });

  it('drops a bare "started" once the stage has said something else', () => {
    const [pm] = buildStages(
      [ev(1, 'pm', 'start', 0), ev(2, 'pm', 'done', 6, { stories: 4 })],
      false
    );
    expect(pm.details).toEqual(['4 stories']);
  });

  it('times a stage from its start→done pairs, not first→last', () => {
    // developer works 0–10s, the tester runs 10–40s, then developer retries 40–45s.
    // First→last would charge developer 45s; the pairs say 15s.
    const [developer] = buildStages(
      [
        ev(1, 'developer', 'start', 0),
        ev(2, 'developer', 'done', 10),
        ev(3, 'tester', 'start', 10),
        ev(4, 'tester', 'done', 40),
        ev(5, 'developer', 'start', 40, { retry: 2, reason: 'tests failing' }),
        ev(6, 'developer', 'done', 45),
      ],
      false
    ).filter(s => s.node === 'developer');
    expect(developer.seconds).toBe(15);
    expect(developer.details).toContain('retry #2 (tests failing)');
  });

  it('gives no duration when a stage never reports a start', () => {
    const [coverage] = buildStages([ev(1, 'coverage', 'done', 5)], false);
    expect(coverage.seconds).toBeNull();
  });

  it('keeps a stage the frontend has not heard of instead of dropping it', () => {
    const stages = buildStages([ev(1, 'security_scan', 'done', 3)], false);
    expect(stages[0]).toMatchObject({ node: 'security_scan', label: 'security_scan' });
  });
});

describe('finishedStatus', () => {
  it('reads the verdict from the closing event', () => {
    const events = [ev(1, 'pm', 'done', 1), ev(2, 'pipeline', 'end', 9, { status: 'unresolved' })];
    expect(finishedStatus(events)).toEqual({ ts: events[1].ts, status: 'unresolved' });
  });

  it('is null while the run is still going', () => {
    expect(finishedStatus([ev(1, 'pm', 'start', 0)])).toBeNull();
  });
});
