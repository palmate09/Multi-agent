/**
 * The hash of the E2E password, computed once with the backend's own
 * `hash_password` and committed so the suite needs no setup step.
 *
 * It is a hash of a throwaway fixture password for a local test server. It
 * grants access to nothing.
 *
 *   PYTHON_DOTENV_DISABLED=1 ../.venv/bin/python \
 *     -c "import sys;sys.path.insert(0,'backend');from app.auth import hash_password;print(hash_password('e2e-password'))"
 */
export const E2E_PASSWORD_HASH =
  'scrypt.16384.8.1.gnQa13rMm0wjt+2vte3FTQ==.IIbdFkKsJjOr7tn49S7AXFABXe2koEkxGgLP0wM7BOA=';
