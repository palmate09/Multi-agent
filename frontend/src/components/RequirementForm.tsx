import { useState } from "react";

const EXAMPLES = [
  "Build a REST API for managing tasks with SQLite persistence. Endpoints: GET /health, CRUD /tasks with title validation (422 on empty), 404 on unknown id.",
  "Build a task manager REST API with CRUD /tasks plus GET /tasks?done=true filter. Same validation (422/404) and /health.",
  "Build a task manager REST API with CRUD /tasks plus a notes field on tasks. Same validation (422/404) and /health.",
];

interface Props {
  onSubmit: (requirement: string, opts: { skipTester: boolean; skipReviewer: boolean }) => void;
  busy: boolean;
  error: string | null;
}

export function RequirementForm({ onSubmit, busy, error }: Props) {
  const [text, setText] = useState(EXAMPLES[0]);
  const [skipTester, setSkipTester] = useState(false);
  const [skipReviewer, setSkipReviewer] = useState(false);

  const valid = text.trim().length >= 10;

  return (
    <div className="card">
      <h2>New run</h2>
      <div className="field">
        <label htmlFor="req">Requirement</label>
        <textarea
          id="req"
          rows={4}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Describe the API you want in plain English…"
        />
      </div>
      <div className="field row" style={{ gap: 16 }}>
        <label className="check">
          <input
            type="checkbox"
            checked={skipTester}
            onChange={(e) => setSkipTester(e.target.checked)}
          />
          Ablate tester
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={skipReviewer}
            onChange={(e) => setSkipReviewer(e.target.checked)}
          />
          Ablate reviewer
        </label>
      </div>
      {error && <div className="alert error">{error}</div>}
      <div className="row">
        <button
          className="primary"
          disabled={!valid || busy}
          onClick={() => onSubmit(text.trim(), { skipTester, skipReviewer })}
        >
          {busy ? "Starting…" : "Run pipeline"}
        </button>
        <span className="hint">
          {text.trim().length < 10
            ? "At least 10 characters required."
            : `${text.trim().length} characters`}
        </span>
      </div>
      <div style={{ marginTop: 10 }}>
        <span className="hint">Examples: </span>
        {EXAMPLES.map((ex, i) => (
          <button
            key={i}
            className="ghost"
            style={{ fontSize: 12 }}
            onClick={() => setText(ex)}
          >
            #{i + 1}
          </button>
        ))}
      </div>
    </div>
  );
}