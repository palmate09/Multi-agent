import { AGENT_STEPS } from '../lib/agents';

/**
 * What happens to a requirement, shown on the home screen where it replaces an
 * empty "no run selected" panel. The six steps really are a sequence, which is
 * why they are numbered.
 */
export function TeamOverview() {
  return (
    <section className="overview" aria-labelledby="overview-title">
      <h2 id="overview-title">How a run works</h2>
      <ol className="overview-steps">
        {AGENT_STEPS.map(step => (
          <li key={step.name} className="overview-step">
            <div className="overview-name">{step.name}</div>
            <p>{step.does}</p>
          </li>
        ))}
      </ol>
      <p className="hint">
        Tick <strong>Generate a web UI</strong> under Pipeline options and a UI designer and builder
        add a static page bound to the API.
      </p>
    </section>
  );
}
