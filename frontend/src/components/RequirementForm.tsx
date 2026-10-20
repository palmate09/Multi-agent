import { useState } from 'react';

import { Alert } from './ui/Alert';
import { Button } from './ui/Button';
import { Card } from './ui/Card';
import { Checkbox, Textarea } from './ui/Field';
import { Icon } from './ui/Icon';

const EXAMPLES = [
  {
    label: 'Tasks with SQLite',
    text: 'Build a REST API for managing tasks with SQLite persistence. Endpoints: GET /health, CRUD /tasks with title validation (422 on empty), 404 on unknown id.',
  },
  {
    label: 'Habit tracker',
    text: 'A habit tracker where I can add daily habits, tick them off, and see a streak count for each one.',
  },
  {
    label: 'Recipe collection',
    text: 'A recipe collection where I can search meals by ingredient, save favourites, and add my own recipes with notes.',
  },
  {
    label: 'Reading list',
    text: 'A reading list that keeps track of what I am reading, what I finished, and what I want to read next.',
  },
  {
    label: 'Task manager with a web UI',
    text: 'Build a task manager REST API with CRUD /tasks and SQLite. Include a web UI page that lists tasks, creates tasks and marks them done.',
  },
];

/** The backend rejects anything shorter; matching it here means the user is
 *  told before a round trip, not after one. */
const MIN_CHARS = 10;

const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.userAgent);

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
  const [text, setText] = useState('');
  const [skipTester, setSkipTester] = useState(false);
  const [skipReviewer, setSkipReviewer] = useState(false);
  const [skipReasoner, setSkipReasoner] = useState(false);
  const [ui, setUi] = useState(false);
  const [showOptions, setShowOptions] = useState(false);
  // Only after a submit: complaining about a half-typed requirement while
  // someone is still writing it is noise, not validation.
  const [touched, setTouched] = useState(false);

  const trimmed = text.trim();
  const valid = trimmed.length >= MIN_CHARS;
  const validation =
    touched && !valid ? `Describe the app in at least ${MIN_CHARS} characters.` : null;
  const changed = [skipTester, skipReviewer, skipReasoner, ui].filter(Boolean).length;

  const submit = () => {
    setTouched(true);
    if (valid && !busy) onSubmit(trimmed, { skipTester, skipReviewer, skipReasoner, ui });
  };

  return (
    <Card label="New run" className="composer">
      <div className="composer-head">
        <h2>New run</h2>
        <p className="composer-lead">
          Describe the app you want in plain English. The team will plan it, build the backend and the
          interface together, and test both.
        </p>
      </div>

      <Textarea
        id="req"
        label="Requirement"
        rows={5}
        value={text}
        onChange={e => setText(e.target.value)}
        onBlur={() => setTouched(true)}
        onKeyDown={e => {
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            submit();
          }
        }}
        placeholder="e.g. A reading list that keeps track of what I am reading, what I finished, and what I want to read next."
        error={validation}
        hint={validation ? undefined : `${trimmed.length} characters`}
        required
      />

      <div className="examples" role="group" aria-label="Example requirements">
        <span className="hint">Start from an example:</span>
        {EXAMPLES.map(ex => (
          <button
            key={ex.label}
            type="button"
            className="chip chip-example"
            disabled={busy}
            title={ex.text}
            onClick={() => {
              setText(ex.text);
              setTouched(false);
            }}
          >
            {ex.label}
          </button>
        ))}
      </div>

      {/* The four switches change how the pipeline runs, and most runs leave
          them alone, so they live behind a disclosure that says how many are
          not at their defaults. */}
      <button
        type="button"
        className="disclosure"
        aria-expanded={showOptions}
        aria-controls="pipeline-options"
        onClick={() => setShowOptions(v => !v)}
      >
        <Icon name="sliders" size={15} />
        <span>Pipeline options</span>
        <span className="disclosure-summary">
          {changed === 0 ? 'All agents on' : `${changed} changed`}
        </span>
        <Icon name={showOptions ? 'chevron-down' : 'chevron-right'} size={15} />
      </button>

      {/* One control with four settings, so it is a fieldset. */}
      <fieldset className="ablate" id="pipeline-options" hidden={!showOptions}>
        <legend className="visually-hidden">Pipeline options</legend>
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
            label="Also build a web app"
            hint="A plain ask already gets one. Tick this when the request names only a backend and you want the interface anyway."
            checked={ui}
            disabled={busy}
            onChange={e => setUi(e.target.checked)}
          />
        </div>
      </fieldset>

      {error && (
        <Alert tone="error">
          {error}
          <div className="retry-row">
            {onRetry && (
              <Button size="sm" onClick={onRetry}>
                Try again
              </Button>
            )}
            {onDismissError && (
              <Button size="sm" variant="ghost" onClick={onDismissError}>
                Dismiss
              </Button>
            )}
          </div>
        </Alert>
      )}

      <div className="composer-actions">
        <Button
          variant="primary"
          disabled={!valid || busy}
          loading={busy}
          loadingLabel="Starting…"
          onClick={submit}
        >
          <Icon name="play" size={14} />
          Run pipeline
        </Button>
        <span className="hint">
          <kbd>{IS_MAC ? '⌘' : 'Ctrl'}</kbd> <kbd>Enter</kbd> to run
        </span>
      </div>
    </Card>
  );
}
