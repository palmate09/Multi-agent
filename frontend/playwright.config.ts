import { defineConfig, devices } from '@playwright/test';
import { E2E_PASSWORD_HASH } from './e2e/credentials';

/**
 * End-to-end tests for the operator console.
 *
 * These run against a REAL FastAPI server with no LLM configured, which is the
 * key decision (see FRONTEND_IMPROVEMENT_PLAN.md §7.1):
 *
 *  - `cli.py --stub` cannot back Playwright: it calls `graph.workflow.run_stub()`,
 *    prints JSON and exits without binding a port.
 *  - `AGENT_TEAM_TESTING=1` does NOT stub generation. It only short-circuits the
 *    Ollama probe (`backend/agents/llm.py`). So a run settles at `blocked` with
 *    the per-provider errors in `detail.error` — which is a first-class terminal
 *    state with its own UI, and is exactly what the tests assert.
 *  - Auth is driven from the environment: `AUTH_PASSWORD` + `AUTH_SECRET` hash on
 *    load (`backend/app/auth.py`), so no backend change is needed.
 *
 * A seeded `accepted` run is written to E2E_RUNS_DIR so the run-detail console
 * (stories, plan, OpenAPI, code, tests, report, review) can be rendered without a
 * model. See e2e/fixtures/seed_run.py for the artifact shape.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['github'], ['list']] : [['list']],
  timeout: 45_000,
  expect: { timeout: 10_000 },

  use: {
    baseURL: 'http://127.0.0.1:5173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    actionTimeout: 10_000,
  },

  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],

  webServer: [
    {
      // The real API. No LLM keys, so runs terminate `blocked` deterministically.
      command:
        '../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8123 --app-dir ../backend',
      cwd: import.meta.dirname,
      url: 'http://127.0.0.1:8123/health',
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: {
        // The hash, not the plaintext. AuthConfig calls load_dotenv() itself
        // and does not honour PYTHON_DOTENV_DISABLED for pydantic's own .env
        // reading, so a developer's root .env can otherwise supply its own
        // AUTH_PASSWORD_HASH and silently win over AUTH_PASSWORD.
        AUTH_USERNAME: 'admin',
        AUTH_PASSWORD_HASH: E2E_PASSWORD_HASH,
        AUTH_SECRET: 'e2e-secret-not-for-production',
        AUTH_COOKIE_SECURE: '0',
        RUNS_DIR: `${import.meta.dirname}/e2e/fixtures/runs`,
        SANDBOX_MODE: 'subprocess',
        SKIP_OLLAMA: '1',
        ENVIRONMENT: 'development',
      },
    },
    {
      command: 'npm run dev -- --port 5173 --strictPort',
      url: 'http://127.0.0.1:5173',
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: { VITE_BACKEND_URL: 'http://127.0.0.1:8123' },
    },
  ],
});
