import type { RunDetail } from '../types';
import { Alert } from './ui/Alert';
import { Card } from './ui/Card';
import { EmptyState } from './ui/Card';
import { FileViewer } from './FileViewer';

interface Props {
  detail: RunDetail;
}

/**
 * The generated interface: its contract, its files, and the gate's verdict.
 *
 * For a full-stack run these files are part of the application itself — the
 * app serves them at "/" — so the panel shows the same thing the person will
 * see in the Playground.
 *
 * The check rows are styled after the domain-coverage alert on
 * purpose — both are advisory verdicts about generated code, and a
 * reader should not have to learn two different visual languages for
 * "the backend is fine, something else is not".
 */
export function UiPanel({ detail }: Props) {
  const spec = detail.ui;
  const report = detail.ui_report;
  const files = detail.ui_code ?? {};

  if (!spec && !report && Object.keys(files).length === 0) {
    return (
      <EmptyState
        action={
          <>
            Tick <strong>Also build a web app</strong> on a new run
          </>
        }
      >
        No interface generated. A plain request already gets one; tick{' '}
        <strong>Also build a web app</strong> to add it to a request that names only a backend.
      </EmptyState>
    );
  }

  return (
    <div className="panel-stack">
      {spec && (
        <Card label="UI contract">
          <h3>Contract</h3>
          <div className="contract-grid">
            <div className="section">
              <div className="list-title">Pages ({spec.pages.length})</div>
              <ul>
                {spec.pages.map(p => (
                  <li key={p}>
                    <code>{p}</code>
                  </li>
                ))}
              </ul>
            </div>
            {spec.components.length > 0 && (
              <div className="section">
                <div className="list-title">Components ({spec.components.length})</div>
                <ul>
                  {spec.components.map(c => (
                    <li key={c}>{c}</li>
                  ))}
                </ul>
              </div>
            )}
            <div className="section">
              <div className="list-title">Data bindings ({spec.data_bindings.length})</div>
              <ul>
                {spec.data_bindings.map(b => (
                  <li key={b}>
                    <code>{b}</code>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </Card>
      )}

      {report && (
        <Card label="UI gate">
          <h3>Gate</h3>
          <Alert tone={report.ok ? 'success' : 'warning'}>
            {report.ok
              ? 'All static UI checks passed.'
              : `${report.checks.filter(c => !c.ok).length} of ${
                  report.checks.length
                } checks failed. Advisory — this did not affect the run's status.`}
          </Alert>
          <div className="scroll-x">
            <table>
              <caption className="visually-hidden">
                Static UI gate checks with their results and detail
              </caption>
              <thead>
                <tr>
                  <th scope="col">Check</th>
                  <th scope="col">Result</th>
                  <th scope="col">Detail</th>
                </tr>
              </thead>
              <tbody>
                {report.checks.map(c => (
                  <tr key={c.name}>
                    <td>
                      <code>{c.name}</code>
                    </td>
                    <td className={c.ok ? 'pass' : 'fail'}>{c.ok ? 'pass' : 'fail'}</td>
                    <td className="hint">{c.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      <Card label="UI files">
        <h3>Files</h3>
        <FileViewer files={files} emptyHint="No UI files generated." />
      </Card>
    </div>
  );
}
