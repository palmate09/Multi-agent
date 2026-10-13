import type { RunDetail } from "../types";
import { FileViewer } from "./FileViewer";

interface Props {
  detail: RunDetail;
}

/**
 * The generated UI: its contract, its files, and the gate's verdict.
 *
 * The check rows are styled after the domain-coverage alert on purpose — both
 * are advisory verdicts about generated code, and a reader should not have to
 * learn two different visual languages for "the backend is fine, something
 * else is not".
 */
export function UiPanel({ detail }: Props) {
  const spec = detail.ui;
  const report = detail.ui_report;
  const files = detail.ui_code ?? {};

  if (!spec && !report && Object.keys(files).length === 0) {
    return (
      <p className="empty">
        No UI generated. Tick <strong>Generate a web UI</strong> on a new run to
        get a static page bound to the generated API.
      </p>
    );
  }

  return (
    <div className="card">
      {spec && (
        <>
          <h3>Contract</h3>
          <div className="section">
            <div className="hint caps">Pages ({spec.pages.length})</div>
            <ul>
              {spec.pages.map((p) => (
                <li key={p}>
                  <code>{p}</code>
                </li>
              ))}
            </ul>
          </div>
          {spec.components.length > 0 && (
            <div className="section">
              <div className="hint caps">Components ({spec.components.length})</div>
              <ul>
                {spec.components.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
            </div>
          )}
          <div className="section">
            <div className="hint caps">Data bindings ({spec.data_bindings.length})</div>
            <ul>
              {spec.data_bindings.map((b) => (
                <li key={b}>
                  <code>{b}</code>
                </li>
              ))}
            </ul>
          </div>
        </>
      )}

      {report && (
        <>
          <h3 style={{ marginTop: 16 }}>Gate</h3>
          <div className={`alert ${report.ok ? "note" : "error"}`}>
            {report.ok
              ? "All static UI checks passed."
              : `${report.checks.filter((c) => !c.ok).length} of ${
                  report.checks.length
                } checks failed. Advisory — this did not affect the run's status.`}
          </div>
          <table>
            <thead>
              <tr>
                <th>Check</th>
                <th>Result</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {report.checks.map((c) => (
                <tr key={c.name}>
                  <td>
                    <code>{c.name}</code>
                  </td>
                  <td className={c.ok ? "pass" : "fail"}>{c.ok ? "pass" : "fail"}</td>
                  <td className="hint">{c.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <h3 style={{ marginTop: 16 }}>Files</h3>
      <FileViewer files={files} emptyHint="No UI files generated." />
    </div>
  );
}