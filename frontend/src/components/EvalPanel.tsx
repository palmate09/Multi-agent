import type { EvalResults } from "../types";

export function EvalPanel({ data }: { data: EvalResults | null }) {
  if (!data) return <p className="empty">Loading evaluation results…</p>;
  if (data.runs.length === 0)
    return (
      <p className="empty">
        No evaluation results yet. Run <code>python cli.py --eval</code> to populate
        <code> evals/results.json</code>.
      </p>
    );

  return (
    <div>
      <div className="stats">
        <div className="stat">
          <div className="v">
            {data.pass_rate === null ? "—" : `${Math.round(data.pass_rate * 100)}%`}
          </div>
          <div className="k">Accept rate</div>
        </div>
        <div className="stat">
          <div className="v">
            {data.accepted}/{data.total}
          </div>
          <div className="k">Accepted</div>
        </div>
        {data.baseline && (
          <div className="stat">
            <div className="v">
              {data.baseline.passed}/{data.baseline.passed + data.baseline.failed}
            </div>
            <div className="k">Baseline (single agent)</div>
          </div>
        )}
      </div>

      <table>
        <thead>
          <tr>
            <th>Run</th>
            <th>Status</th>
            <th className="num">Pass rate</th>
            <th className="num">Spec coverage</th>
            <th className="num">Attempts</th>
            <th className="num">Wall (s)</th>
          </tr>
        </thead>
        <tbody>
          {data.runs.map((r) => (
            <tr key={r.run}>
              <td>
                <code>{r.run}</code>
              </td>
              <td>{r.status}</td>
              <td className="num">{r.test_pass_rate?.toFixed(2) ?? "—"}</td>
              <td className="num">{r.spec_coverage?.toFixed(2) ?? "—"}</td>
              <td className="num">{r.attempts_to_green}</td>
              <td className="num">{r.wall_time}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="alert note" style={{ marginTop: 14 }}>
        These numbers come from template mode, where the Developer emits a known-good bundle
        and the Tester emits a matching suite. <strong>attempts = 0</strong> means the fix
        loop and triage never fired. Use them as a regression harness, not as evidence the
        multi-agent loop helps.
      </div>
    </div>
  );
}