import { Component } from 'react';
import type { ErrorInfo, ReactNode } from 'react';

interface Props {
  children: ReactNode;
  /** Shown in place of the console's own chrome, so a crash inside a panel
   *  does not take the sidebar and the way out with it. */
  label?: string;
}

interface State {
  error: Error | null;
}

/**
 * Contains a render error to the subtree that caused it.
 *
 * Without this, any throw during render unmounts the whole tree and the user
 * is left staring at a blank page with no way back except a reload — which
 * loses the selected run and everything typed into the requirement box. This
 * is the PERF-05 finding from FRONTEND_IMPROVEMENT_PLAN.md §1.6, and it is not
 * hypothetical: a lazy panel that suspends on a synchronous tab switch did
 * exactly this before the Suspense boundaries were restored.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Left to the operator's logs on purpose: this console has no telemetry,
    // and inventing a send endpoint is out of scope for a frontend change.
    console.error(`[${this.props.label ?? 'app'}] render error:`, error, info.componentStack);
  }

  private reset = () => this.setState({ error: null });

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="card error-boundary" role="alert">
        <h2>{this.props.label ?? 'This panel'} stopped working</h2>
        <p className="error-boundary-msg">{error.message}</p>
        <button type="button" className="btn primary" onClick={this.reset}>
          Try again
        </button>
      </div>
    );
  }
}
