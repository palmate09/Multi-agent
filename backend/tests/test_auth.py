"""Tests for authentication: hashing, sessions, CSRF, rate limiting, middleware."""

import time

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import get_settings

GOOD_PASSWORD = "correct-horse-battery-staple"
REQUIREMENT = (
    "Build a REST API for managing tasks with SQLite persistence, "
    "CRUD /tasks plus GET /health, 422 on empty title."
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Give every test an isolated auth config and runs dir."""
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    for key in (
        "AUTH_USERNAME",
        "AUTH_SECRET",
        "AUTH_PASSWORD_HASH",
        "AUTH_PASSWORD",
        "AUTH_API_KEY",
        "AUTH_COOKIE_SECURE",
        "AUTH_SESSION_HOURS",
        "AUTH_CSRF",
    ):
        monkeypatch.delenv(key, raising=False)
    auth.reset_auth_config()
    auth.login_limiter.reset()
    get_settings.cache_clear()
    yield
    auth.reset_auth_config()
    get_settings.cache_clear()


def _enable_auth(monkeypatch, password=GOOD_PASSWORD, secret="s" * 48, **extra):
    monkeypatch.setenv("AUTH_USERNAME", "admin")
    monkeypatch.setenv("AUTH_SECRET", secret)
    monkeypatch.setenv("AUTH_PASSWORD_HASH", auth.hash_password(password))
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    auth.reset_auth_config()


def _client():
    from app.main import create_app

    return TestClient(create_app())


# ---------------------------------------------------------------- hashing ---
def test_password_hash_roundtrip():
    stored = auth.hash_password(GOOD_PASSWORD)
    assert stored.startswith("scrypt.")
    assert auth.verify_password(GOOD_PASSWORD, stored)
    assert not auth.verify_password("wrong", stored)


def test_password_hash_survives_compose_interpolation():
    """Regression: a '$' in the hash would be eaten by Docker Compose.

    Compose treats `$` as a variable reference, so a $-delimited hash silently
    becomes something else in .env and every login fails on a deployed host.
    """
    stored = auth.hash_password(GOOD_PASSWORD)
    assert "$" not in stored, "hash must not contain '$' (docker compose interpolation)"
    # Simulate compose's substitution on a value passed through .env.
    assert stored == stored.replace("$", "")


def test_password_hash_is_salted():
    assert auth.hash_password(GOOD_PASSWORD) != auth.hash_password(GOOD_PASSWORD)


def test_short_password_rejected():
    with pytest.raises(ValueError):
        auth.hash_password("short")


@pytest.mark.parametrize("stored", ["", "garbage", "scrypt.1.2.3", "bcrypt.1.2.3.4.5"])
def test_verify_password_never_raises_on_bad_input(stored):
    assert auth.verify_password("anything", stored) is False


def test_api_key_shape():
    key = auth.new_api_key()
    assert key.startswith("mat_")
    assert len(key) > 30
    assert key != auth.new_api_key()


def test_plaintext_env_password_is_hashed_on_load(monkeypatch):
    monkeypatch.setenv("AUTH_PASSWORD", GOOD_PASSWORD)
    monkeypatch.setenv("AUTH_SECRET", "s" * 48)
    auth.reset_auth_config()
    cfg = auth.get_auth_config()
    assert cfg.enabled
    assert cfg.password_hash.startswith("scrypt.")
    assert GOOD_PASSWORD not in cfg.password_hash


# --------------------------------------------------------------- sessions ---
def test_session_roundtrip(monkeypatch):
    _enable_auth(monkeypatch)
    token, csrf = auth.issue_session("admin")
    assert auth.read_session(token)["u"] == "admin"
    assert len(csrf) > 16


def test_tampered_session_is_rejected(monkeypatch):
    _enable_auth(monkeypatch)
    token, _ = auth.issue_session("admin")
    assert auth.read_session(token[:-3] + "abc") is None


def test_session_signed_with_other_secret_is_rejected(monkeypatch):
    _enable_auth(monkeypatch, secret="a" * 48)
    token, _ = auth.issue_session("admin")
    _enable_auth(monkeypatch, secret="b" * 48)
    assert auth.read_session(token) is None


def test_expired_session_is_rejected(monkeypatch):
    _enable_auth(monkeypatch, AUTH_SESSION_HOURS="0")
    token, _ = auth.issue_session("admin")
    time.sleep(1.1)
    assert auth.read_session(token) is None


def test_missing_secret_fails_closed(monkeypatch):
    """No AUTH_SECRET must not produce working unsigned sessions."""
    monkeypatch.setenv("AUTH_USERNAME", "admin")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", auth.hash_password(GOOD_PASSWORD))
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    auth.reset_auth_config()
    cfg = auth.get_auth_config()
    assert cfg.enabled
    assert cfg.secret, "an ephemeral secret should be derived"
    # And it changes per process, so restarts invalidate old cookies.
    first = cfg.secret
    auth.reset_auth_config()
    assert auth.get_auth_config().secret != first


# ------------------------------------------------------------------- API ----
def test_auth_disabled_by_default(monkeypatch):
    with _client() as c:
        assert c.get("/api/runs").status_code == 200


def test_login_succeeds_and_sets_cookies(monkeypatch):
    _enable_auth(monkeypatch)
    with _client() as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PASSWORD})
        assert r.status_code == 200
        body = r.json()
        assert body["authenticated"] and body["username"] == "admin"
        assert "mat_session" in r.cookies or "mat_session" in c.cookies


def test_login_rejects_bad_password(monkeypatch):
    _enable_auth(monkeypatch)
    with _client() as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": "nope"})
        assert r.status_code == 401
        # Must not distinguish wrong user from wrong password.
        assert r.json()["detail"] == "invalid username or password"


def test_login_rejects_unknown_user(monkeypatch):
    _enable_auth(monkeypatch)
    with _client() as c:
        r = c.post("/api/auth/login", json={"username": "attacker", "password": GOOD_PASSWORD})
        assert r.status_code == 401
        assert r.json()["detail"] == "invalid username or password"


def test_protected_endpoints_require_auth(monkeypatch):
    _enable_auth(monkeypatch)
    with _client() as c:
        assert c.get("/api/runs").status_code == 401
        assert c.get("/api/evals/results").status_code == 401
        assert c.get("/api/docs").status_code == 401
        assert c.post("/api/runs", json={"requirement": REQUIREMENT}).status_code == 401


def test_health_stays_public(monkeypatch):
    """The container healthcheck and uptime probes must not need a session."""
    _enable_auth(monkeypatch)
    with _client() as c:
        assert c.get("/health").status_code == 200


def test_session_endpoint_reports_state(monkeypatch):
    _enable_auth(monkeypatch)
    with _client() as c:
        assert c.get("/api/auth/me").json()["authenticated"] is False
        c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PASSWORD})
        me = c.get("/api/auth/me").json()
        assert me["authenticated"] is True
        assert me["username"] == "admin"
        assert me["csrf_token"]


def test_authenticated_user_can_read_runs(monkeypatch):
    _enable_auth(monkeypatch)
    with _client() as c:
        c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PASSWORD})
        assert c.get("/api/runs").status_code == 200


def test_logout_clears_session(monkeypatch):
    _enable_auth(monkeypatch)
    with _client() as c:
        c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PASSWORD})
        assert c.get("/api/runs").status_code == 200

        out = c.post("/api/auth/logout")
        # Regression: this used to return a bare Response with status_code
        # None, which crashed uvicorn and surfaced as a 502.
        assert out.status_code == 200, out.text
        assert out.json()["authenticated"] is False

        c.cookies.clear()
        assert c.get("/api/runs").status_code == 401


# ------------------------------------------------------------------ CSRF ----
def _csrf(c) -> str:
    return c.get("/api/auth/me").json()["csrf_token"]


def test_write_without_csrf_header_is_rejected(monkeypatch):
    _enable_auth(monkeypatch)
    with _client() as c:
        c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PASSWORD})
        c.get("/api/auth/me")  # populate the csrf cookie
        resp = c.post("/api/runs", json={"requirement": REQUIREMENT})
        assert resp.status_code == 403
        assert "CSRF" in resp.json()["detail"]


def test_write_with_mismatched_csrf_is_rejected(monkeypatch):
    _enable_auth(monkeypatch)
    with _client() as c:
        c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PASSWORD})
        resp = c.post(
            "/api/runs",
            json={"requirement": REQUIREMENT},
            headers={"X-CSRF-Token": "not-the-right-token"},
        )
        assert resp.status_code == 403


def test_write_with_correct_csrf_succeeds(monkeypatch):
    _enable_auth(monkeypatch)
    with _client() as c:
        c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PASSWORD})
        token = _csrf(c)
        resp = c.post(
            "/api/runs", json={"requirement": REQUIREMENT}, headers={"X-CSRF-Token": token}
        )
        assert resp.status_code == 202


def test_reads_do_not_require_csrf(monkeypatch):
    _enable_auth(monkeypatch)
    with _client() as c:
        c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PASSWORD})
        assert c.get("/api/runs").status_code == 200


def test_csrf_can_be_disabled(monkeypatch):
    _enable_auth(monkeypatch, AUTH_CSRF="0")
    with _client() as c:
        c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PASSWORD})
        assert c.post("/api/runs", json={"requirement": REQUIREMENT}).status_code == 202


# --------------------------------------------------------------- API key ----
def test_api_key_grants_access(monkeypatch):
    _enable_auth(monkeypatch, AUTH_API_KEY="mat_test_key_value")
    with _client() as c:
        resp = c.get("/api/runs", headers={"X-API-Key": "mat_test_key_value"})
        assert resp.status_code == 200


def test_bad_api_key_rejected(monkeypatch):
    _enable_auth(monkeypatch, AUTH_API_KEY="mat_test_key_value")
    with _client() as c:
        assert c.get("/api/runs", headers={"X-API-Key": "mat_wrong"}).status_code == 401


def test_api_key_skips_csrf(monkeypatch):
    _enable_auth(monkeypatch, AUTH_API_KEY="mat_test_key_value")
    with _client() as c:
        resp = c.post(
            "/api/runs",
            json={"requirement": REQUIREMENT},
            headers={"X-API-Key": "mat_test_key_value"},
        )
        assert resp.status_code == 202


# ---------------------------------------------------------- rate limiting ----
def test_repeated_failures_get_blocked(monkeypatch):
    _enable_auth(monkeypatch)
    with _client() as c:
        codes = [
            c.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code
            for _ in range(7)
        ]
        assert codes[:5] == [401] * 5
        assert 429 in codes[5:], f"expected throttling, got {codes}"


def test_rate_limit_resets_after_success(monkeypatch):
    _enable_auth(monkeypatch)
    with _client() as c:
        for _ in range(3):
            c.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PASSWORD})
        # Budget should be restored, not still partially consumed.
        assert (
            c.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code
            == 401
        )


def test_limiter_unit_behaviour():
    limiter = auth.RateLimiter(max_attempts=3, window=60, block=30)
    key = "k"
    for _ in range(3):
        assert limiter.check(key) == 0
        limiter.record_failure(key)
    assert limiter.check(key) == 30
    limiter.record_success(key)
    assert limiter.check(key) == 0
