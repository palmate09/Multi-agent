import { useState } from 'react';

import { Alert } from './ui/Alert';
import { Button } from './ui/Button';
import { Card } from './ui/Card';
import { Checkbox, Textarea } from './ui/Field';

const EXAMPLES = [
  'Build a REST API for managing tasks with SQLite persistence. Endpoints: GET /health, CRUD /tasks with title validation (422 on empty), 404 on unknown id.',
  'Build a task manager REST API with CRUD /tasks plus GET /tasks?done=true filter. Same validation (422/404) and /health.',
  'Build a task manager REST API with CRUD /tasks plus a notes field on tasks. Same validation (422/404) and /health.',
];

/** The backend rejects anything shorter; matching it here means the user is
 *  told before a round trip, not after one. */
const MIN_CHARS = 10;

interface Props {
  onSubmit: (
    _requirement: string,
    _opts: { skipTester: boolean; skipReviewer: boolean; skipReasoner: boolean; ui: boolean }
  ) => void;
  busy: boolean;
  error: string | null;
  /** Shown on the error alert, so a failed start can be repeated. */
  onRetry?: () => void;
  onDismissError?: () => void;
}

export function RequirementForm({ onSubmit, busy, error, onRetry, onDismissError }: Props) {
  const [text, setText] = useState(EXAMPLES[0]);
  const [skipTester, setSkipTester] = useState(false);
  const [skipReviewer, setSkipReviewer] = useState(false);
  const [skipReasoner, setSkipReasoner] = useState(false);
  const [ui, setUi] = useState(false);
  // Only after a submit: complaining about a half-typed requirement while
  // someone is still writing it is noise, not validation.
  const [touched, setTouched] = useState(false);

  const trimmed = text.trim();
  const valid = trimmed.length >= MIN_CHARS;
  const validation =
    touched && !valid ? `Describe the API in at least ${MIN_CHARS} characters.` : null;

  return (
    <Card label="New run">
      <h2>New run</h2>

      <Textarea
        label="Requirement"
        rows={4}
        value={text}
        onChange={e => setText(e.target.value)}
        onBlur={() => setTouched(true)}
        placeholder="Describe the API you want in plain English…"
        error={validation}
        hint={
          validation ? undefined : `${trimmed.length} characters · describe the endpoints you want`
        }
        required
      />

      {/* One control with four settings, so it is a fieldset. "Ablate" is
          jargon; each checkbox says what it does to the run. */}
      <fieldset className="ablate">
        <legend>Pipeline options</legend>
        <div className="check-grid">
          <Checkbox
            label="Skip the reasoner"
            hint="No plan tab; design starts from the stories alone."
            checked={skipReasoner}
            disabled={busy}
            onChange={e => setSkipReasoner(e.target.checked)}
          />
          <Checkbox
            label="Skip the tester"
            hint="No generated tests, so nothing is graded green."
            checked={skipTester}
            disabled={busy}
            onChange={e => setSkipTester(e.target.checked)}
          />
          <Checkbox
            label="Skip the reviewer"
            hint="No ruff/bandit pass; runs end accepted_no_review."
            checked={skipReviewer}
            disabled={busy}
            onChange={e => setSkipReviewer(e.target.checked)}
          />
          <Checkbox
            label="Generate a web UI"
            hint="Adds a static page bound to the generated API."
            checked={ui}
            disabled={busy}
            onChange={e => setUi(e.target.checked)}
          />
        </div>
      </fieldset>

      {error && (
        <Alert tone="error">
          {error}
          {onRetry && (
            <div className="retry">
              <Button size="sm" onClick={onRetry}>
                Try again
              </Button>
            </div>
          )}
          {onDismissError && (
            <div className="retry">
              <Button size="sm" variant="ghost" onClick={onDismissError}>
                Dismiss
              </Button>
            </div>
          )}
        </Alert>
      )}

      <div className="row">
        <Button
          variant="primary"
          disabled={!valid || busy}
          loading={busy}
          loadingLabel="Starting…"
          onClick={() => {
            setTouched(true);
            if (valid && !busy) onSubmit(trimmed, { skipTester, skipReviewer, skipReasoner, ui });
          }}
        >
          Run pipeline
        </Button>
        <span className="hint">
          {valid ? `${trimmed.length} characters` : `At least ${MIN_CHARS} characters required.`}
        </span>
      </div>

      <div className="row examples" role="group" aria-label="Example requirements">
        <span className="hint">Examples:</span>
        {EXAMPLES.map((ex, i) => (
          <Button
            key={i}
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => {
              setText(ex);
              setTouched(false);
            }}
          >
            Example {i + 1}
          </Button>
        ))}
      </div>
    </Card>
  );
}
