import type { EvalResults } from '../types';
import { Alert } from './ui/Alert';
import { EmptyState } from './ui/Card';
import { SkeletonGroup } from './ui/Skeleton';
import { Stat, StatGrid } from './ui/Stat';

/**
 * The evaluation table — a regression harness, not evidence.
 *
 * The numbers come from template mode, where the Developer emits a
 * known-good bundle and the Tester emits a matching suite, so
 * `attempts = 0` means the fix loop never fired. That caveat is
 * printed under the table in the same tone as the data it qualifies,
 * because a harness read as a benchmark is how a false green gets
 * trusted.
 */
export function EvalPanel({ data }: { data: EvalResults | null }) {
  // `data` is null until the eval results are fetched, which now
  // happens when this panel is first opened rather than on login.
  if (!data) return <SkeletonGroup label="Loading evaluation results" />;

  if (data.runs.length === 0) {
    return (
      <EmptyState action={<code>python cli.py --eval</code>}>
        No evaluation results yet. Populate them with
        <code> python cli.py --eval</code>, which writes
        <code> evals/results.json</code>.
      </EmptyState>
    );
  }

  return (
    <div>
      <StatGrid>
        <Stat
          value={data.pass_rate === null ? '—' : `${Math.round(data.pass_rate * 100)}%`}
          label="Accept rate"
        />
        <Stat value={`${data.accepted}/${data.total}`} label="Accepted" />
        {data.baseline && (
          <Stat
            value={`${data.baseline.passed}/${data.baseline.passed + data.baseline.failed}`}
            label="Baseline (single agent)"
          />
        )}
      </StatGrid>

      {/* The table is the one thing in the app that is wider than
          its column, so it gets a scroll container of its own: a
          phone should scroll the table, not the page. */}
      <div className="scroll-x">
        <table>
          <caption className="visually-hidden">
            Evaluation runs with pass rate, spec coverage, UI gate, preview boot, attempts and wall
            time
          </caption>
          <thead>
            <tr>
              <th scope="col">Run</th>
              <th scope="col">Status</th>
              <th scope="col" className="num">
                Pass rate
              </th>
              <th scope="col" className="num">
                Spec coverage
              </th>
              <th scope="col" className="num">
                UI gate
              </th>
              <th scope="col" className="num">
                Preview
              </th>
              <th scope="col" className="num">
                Attempts
              </th>
              <th scope="col" className="num">
                Wall (s)
              </th>
            </tr>
          </thead>
          <tbody>
            {data.runs.map(r => (
              <tr key={r.run}>
                <td>
                  <code>{r.run}</code>
                </td>
                <td>{r.status}</td>
                <td className="num">{r.test_pass_rate?.toFixed(2) ?? '—'}</td>
                <td className="num">{r.spec_coverage?.toFixed(2) ?? '—'}</td>
                <td className="num">
                  {r.ui_pass_rate === null || r.ui_pass_rate === undefined
                    ? '—'
                    : `${Math.round(r.ui_pass_rate * 100)}%`}
                </td>
                <td className="num">
                  {r.preview_booted === null || r.preview_booted === undefined
                    ? '—'
                    : r.preview_booted
                      ? 'booted'
                      : 'no'}
                </td>
                <td className="num">{r.attempts_to_green}</td>
                <td className="num">{r.wall_time}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <Alert tone="note">
        These numbers come from template mode, where the Developer emits a known-good bundle and the
        Tester emits a matching suite. <strong>attempts = 0</strong> means the fix loop and triage
        never fired. Use them as a regression harness, not as evidence that the multi-agent loop
        helps.
      </Alert>
    </div>
  );
}
