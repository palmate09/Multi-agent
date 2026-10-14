import { useState } from 'react';

import { Alert } from './ui/Alert';
import { Button } from './ui/Button';
import { Card } from './ui/Card';
import { Input } from './ui/Field';

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
 */
export function LoginPage({ onLogin, error, busy }: Props) {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');

  const canSubmit = username.trim().length > 0 && password.length > 0 && !busy;

  return (
    <div className="auth">
      <main className="auth-main">
        <Card className="auth-card">
          <form
            onSubmit={e => {
              e.preventDefault();
              if (canSubmit) void onLogin(username.trim(), password);
            }}
          >
            {/* The route's only page heading. It was an <h2> before, which left
              the document with no <h1> at all on the sign-in screen. */}
            <h1 className="auth-title">Multi-Agent Software Team</h1>
            <p className="auth-sub">Sign in to start and watch pipeline runs.</p>

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
              type="password"
              autoComplete="current-password"
              name="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              disabled={busy}
              invalid={!!error}
              describedBy={error ? 'login-error' : undefined}
              required
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
              disabled={!canSubmit}
              loading={busy}
              loadingLabel="Signing in…"
            >
              Sign in
            </Button>

            <p className="hint auth-hint">
              Sessions last 12 hours. Generate credentials with
              <code> python backend/scripts/gen_auth.py</code>.
            </p>
          </form>
        </Card>
      </main>
    </div>
  );
}
