import { useState } from 'react';

import { AGENT_STEPS } from '../lib/agents';
import { Alert } from './ui/Alert';
import { Button } from './ui/Button';
import { Card } from './ui/Card';
import { Input } from './ui/Field';
import { BrandMark } from './ui/Icon';
import { ThemeSwitch } from './ui/ThemeSwitch';

interface Props {
  onLogin: (_username: string, _password: string) => Promise<void>;
  error: string | null;
  busy: boolean;
}

/**
 * Sign-in.
 *
 * No "remember me", no second factor, no puzzle: this is one shared operator
 * account behind a single password (WCAG 2.2 SC 3.3.8 Accessible
 * Authentication). What it does have is a live region for the failure and the
 * error wired to the fields, so being wrong is not a silent dead end.
 *
 * The entered values survive a failed attempt. Re-typing a username and
 * password because the password was wrong is redundant entry (SC 3.3.7), and
 * it is the single most common thing to get right here.
 *
 * The left panel says what the product does. Someone arriving at a sign-in
 * screen cold has no other place to learn that.
 */
export function LoginPage({ onLogin, error, busy }: Props) {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [reveal, setReveal] = useState(false);

  const canSubmit = username.trim().length > 0 && password.length > 0 && !busy;

  return (
    <div className="auth">
      <div className="auth-theme">
        <ThemeSwitch />
      </div>

      <div className="auth-shell">
        <aside className="auth-aside">
          <div className="auth-brand">
            <BrandMark size={36} />
            {/* The route's only page heading. */}
            <h1>Multi-Agent Software Team</h1>
          </div>
          <p className="auth-pitch">
            Describe an app in plain English. A team of agents plans, builds, tests and reviews it —
            a tested REST API, and a working web UI when you ask for one.
          </p>
          <ol className="auth-steps" aria-label="The agents in a run">
            {AGENT_STEPS.map(step => (
              <li key={step.name}>
                <span className="auth-step-name">{step.name}</span>
                <span className="auth-step-does">{step.does}</span>
              </li>
            ))}
          </ol>
        </aside>

        <main className="auth-main">
          <Card className="auth-card">
            <form
              onSubmit={e => {
                e.preventDefault();
                if (canSubmit) void onLogin(username.trim(), password);
              }}
            >
              <h2 className="auth-title">Sign in</h2>
              <p className="auth-sub">Start and watch pipeline runs.</p>

              <Input
                label="Username"
                autoComplete="username"
                name="username"
                value={username}
                onChange={e => setUsername(e.target.value)}
                autoFocus
                disabled={busy}
                hint="The shared operator account."
                invalid={!!error}
                describedBy={error ? 'login-error' : undefined}
                required
              />

              <Input
                label="Password"
                type={reveal ? 'text' : 'password'}
                autoComplete="current-password"
                name="password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                disabled={busy}
                invalid={!!error}
                describedBy={error ? 'login-error' : undefined}
                required
                trailing={
                  <button type="button" className="field-toggle" onClick={() => setReveal(r => !r)}>
                    {reveal ? 'Hide' : 'Show'}
                  </button>
                }
              />

              {/* One message, rendered once. Both fields point at it, so a screen
                reader hears it in context instead of twice or not at all. */}
              {error && (
                <div id="login-error">
                  <Alert tone="error">{error}</Alert>
                </div>
              )}

              <Button
                type="submit"
                variant="primary"
                className="auth-submit"
                disabled={!canSubmit}
                loading={busy}
                loadingLabel="Signing in…"
              >
                Sign in
              </Button>

              <p className="hint auth-hint">
                Sessions last 12 hours. Generate credentials with{' '}
                <code>python backend/scripts/gen_auth.py</code>.
              </p>
            </form>
          </Card>
        </main>
      </div>
    </div>
  );
}
