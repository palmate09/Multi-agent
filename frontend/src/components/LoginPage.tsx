import { useState } from "react";

interface Props {
  onLogin: (username: string, password: string) => Promise<void>;
  error: string | null;
  busy: boolean;
}

export function LoginPage({ onLogin, error, busy }: Props) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");

  const canSubmit = username.trim().length > 0 && password.length > 0 && !busy;

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "grid",
        placeItems: "center",
        padding: 20,
      }}
    >
      <form
        className="card"
        style={{ width: "100%", maxWidth: 380, margin: 0 }}
        onSubmit={(e) => {
          e.preventDefault();
          if (canSubmit) void onLogin(username.trim(), password);
        }}
      >
        <h2 style={{ marginBottom: 14 }}>Multi-Agent Software Team</h2>

        <div className="field">
          <label htmlFor="user">Username</label>
          <input
            id="user"
            type="text"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </div>

        <div className="field">
          <label htmlFor="pass">Password</label>
          <input
            id="pass"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>

        {error && <div className="alert error">{error}</div>}

        <button
          type="submit"
          className="primary"
          disabled={!canSubmit}
          style={{ width: "100%", marginTop: 4 }}
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <p className="hint" style={{ marginTop: 12 }}>
          Sessions last 12 hours. Generate credentials with
          <code> python backend/scripts/gen_auth.py</code>.
        </p>
      </form>
    </div>
  );
}